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
        self.app.add_product("U", "Unmanaged", 50)

    def _cancelled_order(self):
        # O holds 5 T and 3 C; P holds another 2 T. O is then cancelled.
        self.app.restock("T", 10)
        self.app.restock("C", 8)
        self.app.place("O", [{"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 3}])
        self.app.place("P", [{"sku": "T", "quantity": 2}])
        self.app.cancel("O")

    def test_reopen_restores_placed_status_and_full_deal(self):
        self._cancelled_order()
        # A catalog reprice after cancellation never reprices the deal.
        self.app.reprice_products([
            {"sku": "T", "expected_price_cents": 100, "price_cents": 150},
        ])
        result = self.app.reopen_order(" O ")
        self.assertEqual(result, {
            "order_id": "O",
            "status": "placed",
            "lines": [
                {"sku": "T", "quantity": 5, "unit_price_cents": 100,
                 "subtotal_cents": 500},
                {"sku": "C", "quantity": 3, "unit_price_cents": 200,
                 "subtotal_cents": 600},
            ],
            "total_cents": 1100,
        })
        self.assertEqual(self.app.get("O"), result)

    def test_duplicate_lines_keep_order_and_content(self):
        self.app.restock("T", 10)
        self.app.restock("C", 5)
        self.app.place("D", [
            {"sku": "T", "quantity": 2},
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 1},
        ])
        self.app.cancel("D")
        result = self.app.reopen_order("D")
        self.assertEqual([line["sku"] for line in result["lines"]], ["T", "C", "T"])
        self.assertEqual([line["quantity"] for line in result["lines"]], [2, 1, 1])
        self.assertEqual(result["total_cents"], 500)
        # The merged 3 T are reserved as one amount.
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(data["reservations"]["D"], {"T": 3, "C": 1})

    def test_reservations_use_current_availability_only(self):
        self._cancelled_order()
        # After the cancel, T has 8 available; another order takes 6 of it.
        self.app.place("Q", [{"sku": "T", "quantity": 6}])
        # O needs 5 T but only 2 remain: the reopen is rejected and nothing
        # is borrowed from P's or Q's reservations.
        with self.assertRaises(ValueError):
            self.app.reopen_order("O")
        self.assertEqual(self.app.get("O")["status"], "cancelled")
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 10, "reserved": 8, "available": 2})
        # Releasing 3 from Q leaves exactly 5: the reopen now fits.
        self.app.release_reservation("Q", [{"sku": "T", "quantity": 3}])
        self.app.reopen_order("O")
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 10, "reserved": 10, "available": 0})
        self.assertEqual(self.app.stock("C"),
                         {"sku": "C", "on_hand": 8, "reserved": 3, "available": 5})
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(data["reservations"]["O"], {"T": 5, "C": 3})
        self.assertEqual(data["reservations"]["P"], {"T": 2})

    def test_product_managed_after_cancel_is_reserved_on_reopen(self):
        # U is ordered while unmanaged; it becomes managed while O is
        # cancelled, so the reopen reserves its full quantity.
        self.app.restock("T", 10)
        self.app.place("O", [{"sku": "T", "quantity": 2}, {"sku": "U", "quantity": 4}])
        self.app.cancel("O")
        self.app.restock("U", 5)
        result = self.app.reopen_order("O")
        self.assertEqual(result["status"], "placed")
        self.assertEqual(self.app.stock("U"),
                         {"sku": "U", "on_hand": 5, "reserved": 4, "available": 1})
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(data["reservations"]["O"], {"T": 2, "U": 4})

    def test_unmanaged_product_stays_unmanaged_without_reservation(self):
        self.app.place("O", [{"sku": "U", "quantity": 4}])
        self.app.cancel("O")
        result = self.app.reopen_order("O")
        self.assertEqual(result["status"], "placed")
        self.assertEqual(self.app.stock("U"),
                         {"sku": "U", "on_hand": None, "reserved": 0, "available": None})
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("O", data.get("reservations", {}))
        self.assertNotIn("U", data.get("inventory", {}))

    def test_missing_enabled_flag_reads_as_enabled(self):
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "orders": {"OLD": {
                "order_id": "OLD", "status": "cancelled",
                "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100,
                           "subtotal_cents": 200}],
                "total_cents": 200,
            }},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        self.assertEqual(app.reopen_order("OLD")["status"], "placed")

    def test_unknown_missing_or_paused_product_rejects(self):
        self._cancelled_order()
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        # Paused product on the order.
        self.app.set_product_enabled("C", False)
        raw = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.reopen_order("O")
        self.assertEqual(self.app.path.read_bytes(), raw)
        self.app.set_product_enabled("C", True)
        # Product removed from the catalog.
        data["products"].pop("C")
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.app.reopen_order("O")
        self.assertEqual(self.app.get("O")["status"], "cancelled")

    def test_status_and_id_validation(self):
        self._cancelled_order()
        for bad in (1, None, True, "", "   ", "ghost", "o"):
            with self.assertRaises(ValueError, msg=bad):
                self.app.reopen_order(bad)
        # P is placed, not cancelled.
        with self.assertRaises(ValueError):
            self.app.reopen_order("P")
        self.app.ship("P", "DHL", "1")
        with self.assertRaises(ValueError):
            self.app.reopen_order("P")
        # A just-reopened order cannot be reopened again.
        self.app.reopen_order("O")
        with self.assertRaises(ValueError):
            self.app.reopen_order("O")

    def test_rejected_reopen_writes_nothing_and_creates_no_directory(self):
        self._cancelled_order()
        self.app.place("Q", [{"sku": "T", "quantity": 6}])
        raw = self.app.path.read_bytes()
        events_before = len(self.app.history("O")["events"])
        stock_events_before = len(self.app.stock_history("T")["events"])
        with self.assertRaises(ValueError):
            self.app.reopen_order("O")
        self.assertEqual(self.app.path.read_bytes(), raw)
        self.assertEqual(len(self.app.history("O")["events"]), events_before)
        self.assertEqual(len(self.app.stock_history("T")["events"]), stock_events_before)
        empty = self.root / "empty"
        with self.assertRaises(ValueError):
            OrderDesk(empty).reopen_order("ghost")
        self.assertFalse(empty.exists())

    def test_order_and_stock_events_are_recorded_together(self):
        self._cancelled_order()
        result = self.app.reopen_order("O")
        history = self.app.history("O")
        self.assertTrue(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                         [(1, "place"), (2, "cancel"), (3, "reopen-order")])
        self.assertEqual(set(history["events"][2]), {"sequence", "action", "result"})
        self.assertEqual(history["events"][2]["result"], result)
        events_t = self.app.stock_history("T")["events"]
        self.assertEqual([e["action"] for e in events_t],
                         ["restock", "place", "place", "cancel", "reopen-order"])
        event = events_t[-1]
        self.assertEqual(set(event), {"sequence", "action", "reference_id", "before", "after"})
        self.assertEqual(event["sequence"], 5)
        self.assertEqual(event["reference_id"], "O")
        self.assertEqual(event["before"],
                         {"sku": "T", "on_hand": 10, "reserved": 2, "available": 8})
        self.assertEqual(event["after"],
                         {"sku": "T", "on_hand": 10, "reserved": 7, "available": 3})
        events_c = self.app.stock_history("C")["events"]
        self.assertEqual([e["action"] for e in events_c],
                         ["restock", "place", "cancel", "reopen-order"])
        self.assertEqual(events_c[-1]["reference_id"], "O")
        # P is untouched: no event lands on its history.
        self.assertEqual([e["action"] for e in self.app.history("P")["events"]], ["place"])

    def test_legacy_order_starts_history_at_one_incomplete(self):
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "inventory": {"T": {"on_hand": 7, "reserved": 1}},
            "orders": {"OLD": {
                "order_id": "OLD", "status": "cancelled",
                "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100,
                           "subtotal_cents": 200}],
                "total_cents": 200,
            }},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        result = app.reopen_order("OLD")
        history = app.history("OLD")
        self.assertFalse(history["complete"])
        self.assertEqual(history["events"], [
            {"sequence": 1, "action": "reopen-order", "result": result},
        ])
        stock_history = app.stock_history("T")
        self.assertFalse(stock_history["complete"])
        self.assertEqual(stock_history["events"][0]["sequence"], 1)
        self.assertEqual(stock_history["events"][0]["action"], "reopen-order")
        self.assertEqual(stock_history["events"][0]["reference_id"], "OLD")
        self.assertEqual(app.stock("T"),
                         {"sku": "T", "on_hand": 7, "reserved": 3, "available": 4})
        stored = json.loads(app.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["reservations"]["OLD"], {"T": 2})

    def test_persists_across_reopen_and_follow_on_operations(self):
        self._cancelled_order()
        result = self.app.reopen_order("O")
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.get("O"), result)
        self.assertEqual(reopened.history("O")["events"][-1]["action"], "reopen-order")
        self.assertEqual(reopened.stock("T"),
                         {"sku": "T", "on_hand": 10, "reserved": 7, "available": 3})
        # The reopened order follows the normal placed-order rules.
        reopened.amend("O", [{"sku": "T", "quantity": 2}])
        self.assertEqual(reopened.stock("C")["reserved"], 0)
        reopened.cancel("O")
        self.assertEqual(reopened.get("O")["status"], "cancelled")
        self.assertEqual(reopened.stock("T")["reserved"], 2)
        # A re-cancelled order can be reopened again.
        reopened.reopen_order("O")
        self.assertEqual(reopened.get("O")["status"], "placed")
        self.assertEqual(reopened.stock("T"),
                         {"sku": "T", "on_hand": 10, "reserved": 4, "available": 6})
        reopened.ship("O", "DHL", "7")
        self.assertEqual(reopened.get("O")["status"], "shipped")
        self.assertEqual(reopened.stock("T"),
                         {"sku": "T", "on_hand": 8, "reserved": 2, "available": 6})

    def test_cli_success_failure_and_array_independence(self):
        self._cancelled_order()
        payload = self.root / "reopen.json"
        payload.write_text(json.dumps({"order_id": " O "}), encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                             "reopen-order", str(payload)],
                            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout)["status"], "placed")
        # A second reopen of the now-placed order exits 2 with an error.
        run = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                              "reopen-order", str(payload)],
                             text=True, capture_output=True)
        self.assertEqual(run.returncode, 2, run.stdout)
        self.assertTrue(json.loads(run.stderr)["error"])
        # Outer array: the first item succeeds; the failed second item does not
        # roll it back or consume another sequence.
        self.app.cancel("O")
        self.app.cancel("P")
        payload.write_text(json.dumps([{"order_id": "O"}, {"order_id": "ghost"}]),
                           encoding="utf-8")
        run = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                              "reopen-order", str(payload)],
                             text=True, capture_output=True)
        self.assertEqual(run.returncode, 2, run.stdout)
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.get("O")["status"], "placed")
        self.assertEqual(reopened.get("P")["status"], "cancelled")
        self.assertEqual([e["action"] for e in reopened.history("O")["events"]],
                         ["place", "cancel", "reopen-order", "cancel", "reopen-order"])

if __name__ == "__main__":
    unittest.main()
