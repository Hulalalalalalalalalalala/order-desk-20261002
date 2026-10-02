import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk

class ShipBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("U", "Unmanaged", 50)

    def test_batch_deducts_only_actual_reservations_example(self):
        self.app.restock("T", 10)
        self.app.place("A", [{"sku": "T", "quantity": 3}])
        self.app.place("B", [{"sku": "T", "quantity": 2}])
        self.app.place("C", [{"sku": "T", "quantity": 1}])
        result = self.app.ship_batch([
            {"order_id": " A ", "carrier": " DHL ", "tracking_no": " t1 ", "extra": 1},
            {"order_id": "B", "carrier": "UPS", "tracking_no": "t2"},
        ])
        self.assertEqual([o["order_id"] for o in result], ["A", "B"])
        for order_id, tracking in (("A", "t1"), ("B", "t2")):
            order = self.app.get(order_id)
            self.assertEqual(order["status"], "shipped")
            self.assertEqual(order["shipment"]["tracking_no"], tracking)
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 5, "reserved": 1, "available": 4})
        self.assertEqual(self.app.get("C")["status"], "placed")
        # reopen consistency
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.stock("T"),
                         {"sku": "T", "on_hand": 5, "reserved": 1, "available": 4})

    def test_result_matches_ship_snapshot(self):
        self.app.restock("T", 5)
        order = self.app.place("O1", [{"sku": "T", "quantity": 2}, {"sku": "T", "quantity": 1}])
        shipped = self.app.ship_batch([{"order_id": "O1", "carrier": "DHL", "tracking_no": "X"}])
        self.assertEqual(len(shipped), 1)
        self.assertEqual(set(shipped[0]), {"order_id", "status", "lines", "total_cents", "shipment"})
        self.assertEqual(shipped[0]["lines"], order["lines"])
        self.assertEqual(shipped[0]["total_cents"], order["total_cents"])

    def test_unmanaged_and_later_managed_and_legacy_no_stock_change(self):
        self.app.place("O1", [{"sku": "U", "quantity": 1000}])
        # order placed before product is managed
        self.app.add_product("C", "Coffee", 200)
        self.app.place("O2", [{"sku": "C", "quantity": 4}])
        self.app.restock("C", 9)
        shipped = self.app.ship_batch([
            {"order_id": "O1", "carrier": "DHL", "tracking_no": "1"},
            {"order_id": "O2", "carrier": "DHL", "tracking_no": "2"},
        ])
        self.assertEqual([o["order_id"] for o in shipped], ["O1", "O2"])
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("U", raw.get("inventory", {}))
        self.assertEqual(self.app.stock("C"),
                         {"sku": "C", "on_hand": 9, "reserved": 0, "available": 9})

    def test_paused_product_still_ships_and_duplicate_tracking_ok(self):
        self.app.restock("T", 5)
        self.app.place("A", [{"sku": "T", "quantity": 1}])
        self.app.place("B", [{"sku": "T", "quantity": 1}])
        self.app.set_product_enabled("T", False)
        result = self.app.ship_batch([
            {"order_id": "A", "carrier": "DHL", "tracking_no": "SAME"},
            {"order_id": "B", "carrier": "UPS", "tracking_no": "SAME"},
        ])
        self.assertEqual(len(result), 2)
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 3, "reserved": 0, "available": 3})

    def test_history_one_event_per_order_sequences_continue(self):
        self.app.restock("T", 5)
        self.app.place("A", [{"sku": "T", "quantity": 1}])
        self.app.amend("A", [{"sku": "T", "quantity": 1}])
        self.app.place("B", [{"sku": "T", "quantity": 1}])
        self.app.ship_batch([
            {"order_id": "A", "carrier": "DHL", "tracking_no": "1"},
            {"order_id": "B", "carrier": "DHL", "tracking_no": "2"},
        ])
        hist_a = self.app.history("A")
        self.assertEqual([e["action"] for e in hist_a["events"]], ["place", "amend", "ship"])
        self.assertEqual([e["sequence"] for e in hist_a["events"]], [1, 2, 3])
        self.assertTrue(hist_a["complete"])
        hist_b = self.app.history("B")
        self.assertEqual([e["action"] for e in hist_b["events"]], ["place", "ship"])
        self.assertTrue(hist_b["complete"])
        self.assertEqual(hist_b["events"][-1]["result"]["status"], "shipped")

    def test_legacy_order_without_history_starts_at_one_complete_false(self):
        # Fabricate a legacy placed order with no history document.
        raw = json.loads(self.app.path.read_text(encoding="utf-8")) if self.app.path.exists() else {}
        raw.setdefault("orders", {})["OLD"] = {
            "order_id": "OLD", "status": "placed",
            "lines": [{"sku": "U", "quantity": 1, "unit_price_cents": 50, "subtotal_cents": 50}],
            "total_cents": 50,
        }
        self.app._write(raw)
        self.app.ship_batch([{"order_id": "OLD", "carrier": "DHL", "tracking_no": "9"}])
        hist = OrderDesk(self.root).history("OLD")
        self.assertFalse(hist["complete"])
        self.assertEqual(len(hist["events"]), 1)
        self.assertEqual(hist["events"][0]["sequence"], 1)
        self.assertEqual(hist["events"][0]["action"], "ship")

    def test_all_invalid_inputs_rejected_without_change(self):
        self.app.restock("T", 5)
        self.app.place("A", [{"sku": "T", "quantity": 1}])
        self.app.place("B", [{"sku": "T", "quantity": 1}])
        self.app.cancel("B")
        self.app.place("C", [{"sku": "T", "quantity": 1}])
        self.app.ship("C", "DHL", "3")
        before = self.app.path.read_bytes()
        invalid = [
            [],
            None,
            {},
            "x",
            123,
            ["not-an-object"],
            [{"carrier": "DHL", "tracking_no": "1"}],
            [{"order_id": "A", "carrier": "DHL"}],
            [{"order_id": "A", "carrier": "  ", "tracking_no": "1"}],
            [{"order_id": "A", "carrier": "DHL", "tracking_no": ""}],
            [{"order_id": 3, "carrier": "DHL", "tracking_no": "1"}],
            [{"order_id": "A", "carrier": 4, "tracking_no": "1"}],
            [{"order_id": " A ", "carrier": "DHL", "tracking_no": "1"},
             {"order_id": "A", "carrier": "DHL", "tracking_no": "2"}],
            [{"order_id": "missing", "carrier": "DHL", "tracking_no": "1"}],
            [{"order_id": "B", "carrier": "DHL", "tracking_no": "1"}],
            [{"order_id": "C", "carrier": "DHL", "tracking_no": "1"}],
            [{"order_id": "A", "carrier": "DHL", "tracking_no": "1"},
             {"order_id": "missing", "carrier": "DHL", "tracking_no": "2"}],
        ]
        for payload in invalid:
            with self.assertRaises(ValueError):
                self.app.ship_batch(payload)
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.get("A")["status"], "placed")
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 4, "reserved": 1, "available": 3})

    def test_failure_does_not_create_directory(self):
        empty_root = self.root / "does-not-exist"
        app = OrderDesk(empty_root)
        with self.assertRaises(ValueError):
            app.ship_batch([])
        self.assertFalse(empty_root.exists())

    def test_batch_shipped_orders_accept_returns(self):
        self.app.restock("T", 5)
        self.app.place("A", [{"sku": "T", "quantity": 1}])
        self.app.ship_batch([{"order_id": "A", "carrier": "DHL", "tracking_no": "1"}])
        record = self.app.record_return("A", "R1", [{"sku": "T", "quantity": 1}])
        self.assertEqual(record["return_id"], "R1")

    def test_cli_success_failure_and_outer_array_independence(self):
        self.app.restock("T", 10)
        self.app.place("A", [{"sku": "T", "quantity": 3}])
        self.app.place("B", [{"sku": "T", "quantity": 2}])
        self.app.place("C", [{"sku": "T", "quantity": 1}])
        payload = self.root / "batch.json"
        payload.write_text(json.dumps({"shipments": [
            {"order_id": "B", "carrier": "UPS", "tracking_no": "B-1"},
            {"order_id": "A", "carrier": "DHL", "tracking_no": "A-1"},
        ]}), encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                             "ship-batch", str(payload)], text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        out = json.loads(ok.stdout)
        self.assertEqual([o["order_id"] for o in out], ["A", "B"])
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 5, "reserved": 1, "available": 4})
        # Failing batch: stderr JSON with error, exit 2, no change.
        bad = self.root / "bad.json"
        bad.write_text(json.dumps({"shipments": [
            {"order_id": "C", "carrier": "DHL", "tracking_no": "C-1"},
            {"order_id": "missing", "carrier": "DHL", "tracking_no": "X"},
        ]}), encoding="utf-8")
        failed = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                                 "ship-batch", str(bad)], text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2, failed.stdout)
        self.assertIn("error", json.loads(failed.stderr))
        self.assertEqual(self.app.get("C")["status"], "placed")
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 5, "reserved": 1, "available": 4})
        # Outer JSON array: each shipment request is independent; earlier
        # success is not rolled back when a later request fails.
        outer = self.root / "outer.json"
        outer.write_text(json.dumps([
            {"shipments": [{"order_id": "C", "carrier": "DHL", "tracking_no": "C-1"}]},
            {"shipments": [{"order_id": "missing", "carrier": "DHL", "tracking_no": "X"}]},
        ]), encoding="utf-8")
        stopped = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                                  "ship-batch", str(outer)], text=True, capture_output=True)
        self.assertEqual(stopped.returncode, 2, stopped.stdout)
        self.assertEqual(OrderDesk(self.root).get("C")["status"], "shipped")
        self.assertEqual(OrderDesk(self.root).stock("T"),
                         {"sku": "T", "on_hand": 4, "reserved": 0, "available": 4})

if __name__ == "__main__":
    unittest.main()
