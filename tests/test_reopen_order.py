import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk


class ReopenOrderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)
        self.app.add_product("U", "Unmanaged", 0)

    def _cancelled(self, order_id, lines, stock=None):
        for sku, quantity in (stock or {"T": 10, "C": 10}).items():
            self.app.restock(sku, quantity)
        self.app.place(order_id, lines)
        self.app.cancel(order_id)

    def test_reopen_returns_placed_order_and_recreates_reservations(self):
        self.app.restock("T", 10)
        order = self.app.place("O1", [
            {"sku": "T", "quantity": 2},
            {"sku": "T", "quantity": 1},
            {"sku": "U", "quantity": 9},
        ])
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 10, "reserved": 3, "available": 7})
        cancelled = self.app.cancel("O1")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 10, "reserved": 0, "available": 10})

        result = self.app.reopen_order("O1")
        self.assertEqual(result, self.app.get("O1"))
        self.assertEqual(result["status"], "placed")
        # Deal content is preserved exactly, including duplicate rows and order.
        self.assertEqual(result["lines"], order["lines"])
        self.assertEqual(result["total_cents"], 300)
        self.assertEqual(set(result), {"order_id", "status", "lines", "total_cents"})
        # Full demand is reserved again from current availability.
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 10, "reserved": 3, "available": 7})
        self.assertEqual(OrderDesk(self.root).stock("T"), self.app.stock("T"))

    def test_reopen_is_rejected_unless_cancelled(self):
        self.app.restock("T", 5)
        self.app.place("P", [{"sku": "T", "quantity": 1}])
        for order_id in ("P",):
            with self.assertRaises(ValueError):
                self.app.reopen_order(order_id)
        self.app.ship("P", "DHL", "1")
        with self.assertRaises(ValueError):
            self.app.reopen_order("P")
        # The placed-then-shipped order never gained a reopen event.
        self.assertEqual([e["action"] for e in self.app.history("P")["events"]],
                         ["place", "ship"])

    def test_reopen_input_validation(self):
        self._cancelled("O1", [{"sku": "T", "quantity": 1}])
        snapshot = json.loads((self.root / "data.json").read_text())
        for bad in (None, 123, True, "", "   "):
            with self.assertRaises(ValueError):
                self.app.reopen_order(bad)
        with self.assertRaises(ValueError):
            self.app.reopen_order("missing")
        # Every rejection leaves the file content untouched.
        self.assertEqual(json.loads((self.root / "data.json").read_text()), snapshot)

    def test_order_id_is_trimmed_and_case_sensitive(self):
        self._cancelled("O1", [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.reopen_order("  O1  ")["order_id"], "O1")
        self.app.cancel("O1")
        with self.assertRaises(ValueError):
            self.app.reopen_order("o1")

    def test_double_reopen_is_rejected(self):
        self._cancelled("O1", [{"sku": "T", "quantity": 1}])
        self.app.reopen_order("O1")
        with self.assertRaises(ValueError):
            self.app.reopen_order("O1")

    def test_reopen_preserves_deal_price_after_catalog_reprice(self):
        self._cancelled("O1", [{"sku": "T", "quantity": 2}, {"sku": "U", "quantity": 1}])
        self.app.reprice_products([
            {"sku": "T", "expected_price_cents": 100, "price_cents": 150},
            {"sku": "U", "expected_price_cents": 0, "price_cents": 7},
        ])
        result = self.app.reopen_order("O1")
        self.assertEqual(result["lines"][0]["unit_price_cents"], 100)
        self.assertEqual(result["lines"][0]["subtotal_cents"], 200)
        self.assertEqual(result["lines"][1]["unit_price_cents"], 0)
        self.assertEqual(result["total_cents"], 200)

    def test_missing_or_paused_product_blocks_reopen(self):
        self._cancelled("O1", [{"sku": "T", "quantity": 1}, {"sku": "U", "quantity": 1}])
        # A product removed from the catalog cannot be recreated here; instead
        # pause it: the whole reopen must be rejected.
        self.app.set_product_enabled("T", False)
        with self.assertRaises(ValueError):
            self.app.reopen_order("O1")
        order = self.app.get("O1")
        self.assertEqual(order["status"], "cancelled")
        self.assertEqual(self.app.stock("T")["reserved"], 0)
        self.app.set_product_enabled("T", True)
        self.assertEqual(self.app.reopen_order("O1")["status"], "placed")

    def test_legacy_product_without_enabled_sells(self):
        self._cancelled("O1", [{"sku": "T", "quantity": 1}])
        data = json.loads((self.root / "data.json").read_text())
        self.assertNotIn("enabled", data["products"]["T"])
        self.assertEqual(self.app.reopen_order("O1")["status"], "placed")

    def test_insufficient_stock_fails_without_partial_changes(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 4}])
        self.app.cancel("O1")
        # Another order now consumes the freed availability.
        self.app.place("O2", [{"sku": "T", "quantity": 3}])
        # Only 2 left, reopening needs 4: reject, and O2's reservation stands.
        with self.assertRaises(ValueError):
            self.app.reopen_order("O1")
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 5, "reserved": 3, "available": 2})
        self.assertEqual(self.app.get("O1")["status"], "cancelled")
        self.assertNotIn("O1", json.loads((self.root / "data.json").read_text()).get("reservations", {}))

    def test_reopen_never_borrows_other_orders_reservations(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 5}])
        self.app.cancel("O1")
        self.app.count_stock("CNT1", [{"sku": "T", "on_hand": 5}])
        self.app.place("O2", [{"sku": "T", "quantity": 5}])
        # Nothing available: O1 must not take O2's holdings.
        with self.assertRaises(ValueError):
            self.app.reopen_order("O1")
        self.assertEqual(self.app.stock("T")["reserved"], 5)

    def test_product_managed_after_original_place_gets_reserved_on_reopen(self):
        # Placed while U is unmanaged: no reservation.
        self.app.place("O1", [{"sku": "U", "quantity": 4}])
        self.app.cancel("O1")
        self.app.restock("U", 10)
        result = self.app.reopen_order("O1")
        self.assertEqual(result["status"], "placed")
        self.assertEqual(self.app.stock("U"),
                         {"sku": "U", "on_hand": 10, "reserved": 4, "available": 6})

    def test_product_managed_after_place_but_short_stock(self):
        self.app.place("O1", [{"sku": "U", "quantity": 4}])
        self.app.cancel("O1")
        self.app.restock("U", 3)
        with self.assertRaises(ValueError):
            self.app.reopen_order("O1")
        self.assertEqual(self.app.get("O1")["status"], "cancelled")
        self.assertEqual(self.app.stock("U")["reserved"], 0)

    def test_unmanaged_product_has_no_reservation(self):
        self._cancelled("O1", [{"sku": "U", "quantity": 100}])
        self.app.reopen_order("O1")
        self.assertEqual(self.app.stock("U"),
                         {"sku": "U", "on_hand": None, "reserved": 0, "available": None})
        self.assertNotIn("U", json.loads((self.root / "data.json").read_text()).get("inventory", {}))

    def test_history_records_reopen_event_with_full_snapshot(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.app.cancel("O1")
        result = self.app.reopen_order("O1")
        history = self.app.history("O1")
        self.assertTrue(history["complete"])
        actions = [e["action"] for e in history["events"]]
        self.assertEqual(actions, ["place", "cancel", "reopen-order"])
        event = history["events"][-1]
        self.assertEqual(event["sequence"], 3)
        self.assertEqual(event["result"], result)
        self.assertEqual(event["result"]["status"], "placed")

    def test_legacy_order_history_starts_at_one_incomplete(self):
        # An order that existed before history tracking: create one directly.
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "orders": {"O1": {"order_id": "O1", "status": "cancelled",
                              "lines": [{"sku": "T", "quantity": 1,
                                         "unit_price_cents": 100, "subtotal_cents": 100}],
                              "total_cents": 100}},
        }
        (self.root / "data.json").write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.app.reopen_order("O1")["status"], "placed")
        history = self.app.history("O1")
        self.assertFalse(history["complete"])
        self.assertEqual(len(history["events"]), 1)
        self.assertEqual(history["events"][0]["sequence"], 1)
        self.assertEqual(history["events"][0]["action"], "reopen-order")

    def test_stock_history_records_reopen_reservations(self):
        self.app.restock("T", 5)
        self.app.restock("C", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}, {"sku": "U", "quantity": 2}])
        self.app.cancel("O1")
        self.app.reopen_order("O1")
        events = self.app.stock_history("T")["events"]
        self.assertEqual([e["action"] for e in events], ["restock", "place", "cancel", "reopen-order"])
        event = events[-1]
        self.assertEqual(event["reference_id"], "O1")
        self.assertEqual(event["before"],
                         {"sku": "T", "on_hand": 5, "reserved": 0, "available": 5})
        self.assertEqual(event["after"],
                         {"sku": "T", "on_hand": 5, "reserved": 1, "available": 4})
        # Unmanaged products get no stock events.
        self.assertEqual(self.app.stock_history("U")["events"], [])

    def test_failed_reopen_consumes_no_sequences(self):
        self.app.restock("T", 2)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.cancel("O1")
        self.app.place("O2", [{"sku": "T", "quantity": 2}])
        t_before = self.app.stock_history("T")
        with self.assertRaises(ValueError):
            self.app.reopen_order("O1")
        self.assertEqual(self.app.stock_history("T")["events"], t_before["events"])
        self.assertEqual([e["action"] for e in self.app.history("O1")["events"]],
                         ["place", "cancel"])

    def test_failed_reopen_creates_no_directory(self):
        fresh = Path(self.temp.name) / "empty"
        app = OrderDesk(fresh)
        with self.assertRaises(ValueError):
            app.reopen_order("ghost")
        self.assertFalse(fresh.exists())

    def test_reopen_cancel_cycle_and_later_operations_use_reservations(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.cancel("O1")
        self.app.reopen_order("O1")
        self.app.cancel("O1")
        self.app.reopen_order("O1")
        self.assertEqual(self.app.stock("T")["reserved"], 2)
        # Amend shrinks against the reopened reservation.
        self.app.amend("O1", [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.stock("T")["reserved"], 1)
        shipped = self.app.ship("O1", "DHL", "X")
        self.assertEqual(shipped["status"], "shipped")
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 4, "reserved": 0, "available": 4})

    def test_other_orders_carts_and_stock_are_unchanged(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.cancel("O1")
        self.app.place("O2", [{"sku": "T", "quantity": 3}])
        self.app.save_cart("K1", [{"sku": "T", "quantity": 4}])
        self.app.reopen_order("O1")
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 10, "reserved": 5, "available": 5})
        self.assertEqual(self.app.get_cart("K1")["lines"], [{"sku": "T", "quantity": 4}])
        self.assertEqual(self.app.get("O2")["status"], "placed")

    def test_missing_collections_use_empty_semantics(self):
        # A cancelled order with no inventory/reservations collections at all.
        data = {
            "products": {"U": {"sku": "U", "name": "Unmanaged", "price_cents": 0}},
            "orders": {"O1": {"order_id": "O1", "status": "cancelled",
                              "lines": [{"sku": "U", "quantity": 3,
                                         "unit_price_cents": 0, "subtotal_cents": 0}],
                              "total_cents": 0}},
        }
        (self.root / "data.json").write_text(json.dumps(data), encoding="utf-8")
        result = OrderDesk(self.root).reopen_order("O1")
        self.assertEqual(result["status"], "placed")
        saved = json.loads((self.root / "data.json").read_text())
        self.assertNotIn("reservations", saved)

    def test_cli_reopen_success_failure_and_array(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        self.app.cancel("O1")
        self.app.cancel("O2")
        payload = self.root / "in.json"
        payload.write_text(json.dumps({"order_id": " O1 "}), encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                             "reopen-order", str(payload)], text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout)["status"], "placed")
        # A just-reopened order is rejected via the CLI with exit code 2.
        failed = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                                 "reopen-order", str(payload)], text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2, failed.stdout)
        self.assertIn("error", json.loads(failed.stderr))
        # Outer array: each item is independent; the first success survives the
        # failure of the last.
        payload.write_text(json.dumps([{"order_id": "O2"}, {"order_id": "nope"}]), encoding="utf-8")
        batch = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                                "reopen-order", str(payload)], text=True, capture_output=True)
        self.assertEqual(batch.returncode, 2, batch.stdout)
        self.assertEqual(self.app.get("O2")["status"], "placed")
        self.assertEqual(self.app.stock("T")["reserved"], 2)


if __name__ == "__main__":
    unittest.main()
