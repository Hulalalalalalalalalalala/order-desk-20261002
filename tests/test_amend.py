import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk

class AmendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)

    def test_amend_replaces_lines_and_keeps_identity(self):
        self.app.restock("T", 10)
        self.app.restock("C", 10)
        placed = self.app.place("O1", [{"sku": "T", "quantity": 2}])
        amended = self.app.amend(" O1 ", [
            {"sku": " C ", "quantity": 1, "note": "ignored"},
            {"sku": "T", "quantity": 3},
            {"sku": "C", "quantity": 1},
        ])
        self.assertEqual(amended["order_id"], "O1")
        self.assertEqual(amended["status"], "placed")
        # Input order and duplicate SKUs are preserved; extra fields dropped.
        self.assertEqual(amended["lines"], [
            {"sku": "C", "quantity": 1, "unit_price_cents": 200, "subtotal_cents": 200},
            {"sku": "T", "quantity": 3, "unit_price_cents": 100, "subtotal_cents": 300},
            {"sku": "C", "quantity": 1, "unit_price_cents": 200, "subtotal_cents": 200},
        ])
        self.assertEqual(amended["total_cents"], 700)
        self.assertNotIn("shipment", amended)
        # get and list reflect the amended content; the old snapshot is gone.
        self.assertEqual(self.app.get("O1"), amended)
        self.assertEqual(self.app.list_orders(), [amended])
        self.assertNotEqual(self.app.get("O1")["lines"], placed["lines"])

    def test_amend_uses_catalog_price_at_submission(self):
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.get("O1")["lines"][0]["unit_price_cents"], 100)
        # Change the catalog by replacing the stored price, then amend.
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        data["products"]["T"]["price_cents"] = 150
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        amended = self.app.amend("O1", [{"sku": "T", "quantity": 2}])
        self.assertEqual(amended["lines"][0]["unit_price_cents"], 150)
        self.assertEqual(amended["total_cents"], 300)

    def test_amend_stock_check_counts_own_reservation(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        # Available is 1; own reservation of 3 makes 4 the ceiling.
        amended = self.app.amend("O1", [{"sku": "T", "quantity": 4}])
        self.assertEqual(amended["lines"][0]["quantity"], 4)
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 5, "reserved": 5, "available": 0})
        with self.assertRaises(ValueError):
            self.app.amend("O1", [{"sku": "T", "quantity": 5}])
        # Failed amend changed nothing.
        self.assertEqual(self.app.get("O1")["lines"][0]["quantity"], 4)
        self.assertEqual(self.app.stock("T")["reserved"], 5)

    def test_amend_releases_removed_and_reduced_reservations(self):
        self.app.restock("T", 5)
        self.app.restock("C", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 2}])
        self.app.amend("O1", [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 5, "reserved": 1, "available": 4})
        self.assertEqual(self.app.stock("C"), {"sku": "C", "on_hand": 5, "reserved": 0, "available": 5})

    def test_cancel_and_ship_after_amend_use_new_reservations(self):
        self.app.restock("T", 5)
        self.app.restock("C", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.amend("O1", [{"sku": "C", "quantity": 2}])
        self.app.cancel("O1")
        self.assertEqual(self.app.stock("T")["reserved"], 0)
        self.assertEqual(self.app.stock("C")["reserved"], 0)
        self.app.place("O2", [{"sku": "T", "quantity": 3}])
        self.app.amend("O2", [{"sku": "C", "quantity": 2}])
        self.app.ship("O2", "DHL", "1")
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 5, "reserved": 0, "available": 5})
        self.assertEqual(self.app.stock("C"), {"sku": "C", "on_hand": 3, "reserved": 0, "available": 3})

    def test_unmanaged_products_stay_unlimited_and_unreserved(self):
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        amended = self.app.amend("O1", [{"sku": "T", "quantity": 99}, {"sku": "C", "quantity": 50}])
        self.assertEqual(amended["total_cents"], 99 * 100 + 50 * 200)
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": None, "reserved": 0, "available": None})
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("reservations", data)

    def test_product_managed_after_place_is_checked_and_reserved(self):
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.restock("T", 3)
        amended = self.app.amend("O1", [{"sku": "T", "quantity": 3}])
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 3, "reserved": 3, "available": 0})
        with self.assertRaises(ValueError):
            self.app.amend("O1", [{"sku": "T", "quantity": 4}])
        self.assertEqual(self.app.get("O1"), amended)

    def test_legacy_order_without_reservation_gets_no_extra_allowance(self):
        self.app.restock("T", 5)
        self.app.place("O2", [{"sku": "T", "quantity": 2}])
        # Legacy order: placed before reservations existed, holds nothing.
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        data["orders"]["OLD"] = {"order_id": "OLD", "status": "placed",
                                 "lines": [{"sku": "T", "quantity": 3, "unit_price_cents": 100, "subtotal_cents": 300}],
                                 "total_cents": 300}
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        # Available is 3; the legacy order's nominal 3 units grant nothing.
        amended = self.app.amend("OLD", [{"sku": "T", "quantity": 3}])
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 5, "reserved": 5, "available": 0})
        with self.assertRaises(ValueError):
            self.app.amend("OLD", [{"sku": "T", "quantity": 4}])
        self.assertEqual(self.app.get("OLD"), amended)

    def test_amend_validation_errors(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.app.cancel("O1")
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        good = [{"sku": "T", "quantity": 1}]
        for order_id, lines in (
            ("missing", good),            # unknown order
            ("O1", good),                 # not placed
            (None, good), (123, good), ("   ", good),
            ("O2", None), ("O2", "x"), ("O2", []),
            ("O2", ["x"]), ("O2", [{"quantity": 1}]), ("O2", [{"sku": "T"}]),
            ("O2", [{"sku": "  ", "quantity": 1}]),
            ("O2", [{"sku": "T", "quantity": 0}]),
            ("O2", [{"sku": "T", "quantity": -1}]),
            ("O2", [{"sku": "T", "quantity": 1.5}]),
            ("O2", [{"sku": "T", "quantity": True}]),
            ("O2", [{"sku": "T", "quantity": "1"}]),
            ("O2", [{"sku": "X", "quantity": 1}]),
            ("O2", [{"sku": "T", "quantity": 99}]),
        ):
            with self.assertRaises(ValueError, msg=(order_id, lines)):
                self.app.amend(order_id, lines)

    def test_failed_amend_does_not_rewrite_or_consume_sequence(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        raw = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.amend("O1", [{"sku": "T", "quantity": 99}])
        self.assertEqual(self.app.path.read_bytes(), raw)
        self.assertEqual([e["action"] for e in self.app.history("O1")["events"]], ["place"])

    def test_failed_amend_creates_no_root(self):
        empty = self.root / "empty"
        app = OrderDesk(empty)
        with self.assertRaises(ValueError):
            app.amend("ghost", [{"sku": "T", "quantity": 1}])
        self.assertFalse(empty.exists())

    def test_amend_history_events(self):
        self.app.restock("T", 10)
        self.app.restock("C", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        first = self.app.amend("O1", [{"sku": "C", "quantity": 2}])
        # Identical resubmission still appends an event.
        second = self.app.amend("O1", [{"sku": "C", "quantity": 2}])
        history = self.app.history("O1")
        self.assertTrue(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                         [(1, "place"), (2, "amend"), (3, "amend")])
        self.assertEqual(history["events"][1]["result"], first)
        self.assertEqual(history["events"][2]["result"], second)
        # The place snapshot keeps the original lines.
        self.assertEqual(history["events"][0]["result"]["lines"],
                         [{"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100}])
        # Persisted in the same write; reopening agrees.
        reopened = OrderDesk(self.root).history("O1")
        self.assertEqual(reopened, history)
        self.assertEqual(OrderDesk(self.root).get("O1"), second)

    def test_legacy_order_amend_starts_history_at_one(self):
        data = {"products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
                "orders": {"OLD": {"order_id": "OLD", "status": "placed",
                                   "lines": [{"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100}],
                                   "total_cents": 100}}}
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        amended = app.amend("OLD", [{"sku": "T", "quantity": 2}])
        history = app.history("OLD")
        self.assertFalse(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]], [(1, "amend")])
        self.assertEqual(history["events"][0]["result"], amended)

    def test_quote_and_place_unaffected_by_amend(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.amend("O1", [{"sku": "T", "quantity": 1}])
        quote = self.app.quote([{"sku": "T", "quantity": 4}])
        self.assertTrue(quote["can_place"])
        self.app.place("O2", [{"sku": "T", "quantity": 4}])
        self.assertEqual(self.app.stock("T")["reserved"], 5)

    def test_cli_amend_success_and_failure(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        payload = self.root / "a.json"
        payload.write_text(json.dumps({"order_id": " O1 ", "lines": [{"sku": "T", "quantity": 2}]}),
                           encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "amend", str(payload)],
                            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual(result["total_cents"], 200)
        payload.write_text(json.dumps({"order_id": "O1", "lines": [{"sku": "T", "quantity": 99}]}),
                           encoding="utf-8")
        failed = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "amend", str(payload)],
                                text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(failed.stdout, "")
        self.assertIn("error", json.loads(failed.stderr))
        self.assertEqual(OrderDesk(self.root).get("O1")["total_cents"], 200)

    def test_cli_amend_array_stops_at_error_and_keeps_successes(self):
        self.app.restock("T", 10)
        self.app.place("A", [{"sku": "T", "quantity": 1}])
        self.app.place("B", [{"sku": "T", "quantity": 1}])
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([
            {"order_id": "A", "lines": [{"sku": "T", "quantity": 2}]},
            {"order_id": "missing", "lines": [{"sku": "T", "quantity": 1}]},
            {"order_id": "B", "lines": [{"sku": "T", "quantity": 3}]},
        ]), encoding="utf-8")
        run = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "amend", str(batch)],
                             text=True, capture_output=True)
        self.assertEqual(run.returncode, 2)
        self.assertEqual(run.stdout, "")
        app = OrderDesk(self.root)
        self.assertEqual(app.get("A")["lines"][0]["quantity"], 2)
        self.assertEqual(app.get("B")["lines"][0]["quantity"], 1)
        self.assertEqual([e["action"] for e in app.history("A")["events"]], ["place", "amend"])
        self.assertEqual([e["action"] for e in app.history("B")["events"]], ["place"])

if __name__ == "__main__":
    unittest.main()
