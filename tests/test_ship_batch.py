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
        self.app.add_product("C", "Coffee", 200)
        self.app.add_product("U", "Unrestocked", 50)

    def payload(self, rows):
        path = self.root / "input.json"
        path.write_text(json.dumps({"shipments": rows}), encoding="utf-8")
        return path

    def test_batch_ships_all_orders_sorted_with_snapshots(self):
        self.app.restock("T", 10)
        o2 = self.app.place("O2", [{"sku": "T", "quantity": 2}, {"sku": "T", "quantity": 1}])
        o1 = self.app.place("O1", [{"sku": "C", "quantity": 1}])
        result = self.app.ship_batch([
            {"order_id": " O2 ", "carrier": "  DHL  ", "tracking_no": " X-2 ", "extra": "ignored"},
            {"order_id": "O1", "carrier": "UPS", "tracking_no": "X-2"},
        ])
        self.assertEqual([order["order_id"] for order in result], ["O1", "O2"])
        for order in result:
            self.assertEqual(order["status"], "shipped")
            self.assertEqual(set(order), {"order_id", "status", "lines", "total_cents", "shipment"})
        by_id = {order["order_id"]: order for order in result}
        self.assertEqual(by_id["O2"]["shipment"], {"carrier": "DHL", "tracking_no": "X-2"})
        self.assertEqual(by_id["O1"]["shipment"], {"carrier": "UPS", "tracking_no": "X-2"})
        # Lines, amounts and duplicate-sku arrangement are preserved.
        self.assertEqual(by_id["O2"]["lines"], o2["lines"])
        self.assertEqual(by_id["O2"]["total_cents"], o2["total_cents"])
        self.assertEqual(by_id["O1"]["lines"], o1["lines"])
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.list_orders(), result)

    def test_batch_deducts_only_each_orders_reservation(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.place("O2", [{"sku": "T", "quantity": 2}])
        self.app.place("O3", [{"sku": "T", "quantity": 1}])
        result = self.app.ship_batch([
            {"order_id": "O2", "carrier": "DHL", "tracking_no": "2"},
            {"order_id": "O1", "carrier": "DHL", "tracking_no": "1"},
        ])
        self.assertEqual([order["order_id"] for order in result], ["O1", "O2"])
        # 10 on hand; O1 reserved 3 and O2 reserved 2 leave on_hand 5; O3's
        # reservation of 1 survives, so reserved 1 and available 4.
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 5, "reserved": 1, "available": 4})
        self.assertEqual(self.app.get("O3")["status"], "placed")
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["reservations"], {"O3": {"T": 1}})

    def test_unmanaged_later_managed_and_legacy_orders_ship_without_stock_change(self):
        self.app.place("U1", [{"sku": "U", "quantity": 1000}])
        # Product managed only after the order was placed: no real reservation.
        self.app.place("C1", [{"sku": "C", "quantity": 4}])
        self.app.restock("C", 9)
        # Legacy order: shipped-capable record without a history document.
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        data["orders"]["OLD"] = {"order_id": "OLD", "status": "placed",
                                 "lines": [{"sku": "T", "quantity": 2}], "total_cents": 200}
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        result = self.app.ship_batch([
            {"order_id": "U1", "carrier": "DHL", "tracking_no": "1"},
            {"order_id": "C1", "carrier": "DHL", "tracking_no": "2"},
            {"order_id": "OLD", "carrier": "DHL", "tracking_no": "3"},
        ])
        self.assertEqual([order["order_id"] for order in result], ["C1", "OLD", "U1"])
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("U", raw.get("inventory", {}))
        self.assertEqual(self.app.stock("C"),
                         {"sku": "C", "on_hand": 9, "reserved": 0, "available": 9})
        # The legacy order gets a fresh history starting at 1, complete false.
        history = self.app.history("OLD")
        self.assertFalse(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]], [(1, "ship")])
        self.assertEqual(history["events"][0]["result"]["shipment"],
                         {"carrier": "DHL", "tracking_no": "3"})

    def test_paused_product_still_ships_in_batch(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.set_product_enabled("T", False)
        result = self.app.ship_batch([{"order_id": "O1", "carrier": "DHL", "tracking_no": "1"}])
        self.assertEqual(result[0]["status"], "shipped")
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 3, "reserved": 0, "available": 3})

    def test_history_sequences_continue_and_old_events_remain(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.app.amend("O1", [{"sku": "T", "quantity": 2}])
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        self.app.ship_batch([
            {"order_id": "O2", "carrier": "DHL", "tracking_no": "2"},
            {"order_id": "O1", "carrier": "DHL", "tracking_no": "1"},
        ])
        h1 = self.app.history("O1")
        self.assertTrue(h1["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in h1["events"]],
                         [(1, "place"), (2, "amend"), (3, "ship")])
        self.assertEqual(h1["events"][2]["result"], self.app.get("O1"))
        h2 = self.app.history("O2")
        self.assertEqual([(e["sequence"], e["action"]) for e in h2["events"]],
                         [(1, "place"), (2, "ship")])

    def test_invalid_batches_are_rejected_entirely(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        self.app.place("dup", [{"sku": "T", "quantity": 1}])
        self.app.cancel("dup")
        self.app.ship("O2", "DHL", "2")
        before = self.app.path.read_bytes()
        good = {"order_id": "O1", "carrier": "DHL", "tracking_no": "1"}
        invalid_lists = [
            [],
            None,
            {},
            "x",
            [good, "not-an-object"],
            [{"carrier": "DHL", "tracking_no": "1"}],
            [{"order_id": "O1", "tracking_no": "1"}],
            [{"order_id": "O1", "carrier": "DHL"}],
            [{"order_id": "  ", "carrier": "DHL", "tracking_no": "1"}],
            [{"order_id": "O1", "carrier": None, "tracking_no": "1"}],
            [{"order_id": "O1", "carrier": "DHL", "tracking_no": ""}],
            [{"order_id": 3, "carrier": "DHL", "tracking_no": "1"}],
            [{"order_id": True, "carrier": "DHL", "tracking_no": "1"}],
            [{"order_id": " O1 ", "carrier": "DHL", "tracking_no": "1"},
             {"order_id": "O1", "carrier": "UPS", "tracking_no": "9"}],
            [{"order_id": "missing", "carrier": "DHL", "tracking_no": "1"}],
            [dict(good, order_id="dup")],
            [dict(good, order_id="O2")],
            # A bad later entry must roll back nothing even though the first is fine.
            [good, {"order_id": "missing", "carrier": "DHL", "tracking_no": "x"}],
        ]
        for shipments in invalid_lists:
            with self.subTest(shipments=shipments):
                with self.assertRaises(ValueError):
                    self.app.ship_batch(shipments)
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.get("O1")["status"], "placed")
        self.assertNotIn("shipment", self.app.get("O1"))
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 9, "reserved": 1, "available": 8})
        raw = json.loads(before)
        self.assertEqual(raw["history"]["O1"]["events"][-1]["action"], "place")

    def test_case_sensitive_ids_are_not_duplicates(self):
        self.app.restock("T", 5)
        self.app.place("a", [{"sku": "T", "quantity": 1}])
        self.app.place("A", [{"sku": "T", "quantity": 1}])
        result = self.app.ship_batch([
            {"order_id": "a", "carrier": "DHL", "tracking_no": "1"},
            {"order_id": " A ", "carrier": "DHL", "tracking_no": "1"},
        ])
        self.assertEqual([order["order_id"] for order in result], ["A", "a"])

    def test_failure_does_not_create_directory(self):
        missing_root = self.root / "does-not-exist"
        app = OrderDesk(missing_root)
        with self.assertRaises(ValueError):
            app.ship_batch([])
        with self.assertRaises(ValueError):
            app.ship_batch([{"order_id": "X", "carrier": "DHL", "tracking_no": "1"}])
        self.assertFalse(missing_root.exists())

    def test_batch_shipped_orders_follow_normal_return_rules(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.ship_batch([{"order_id": "O1", "carrier": "DHL", "tracking_no": "1"}])
        record = self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.assertEqual(record["lines"], [{"sku": "T", "quantity": 1}])
        receipt = self.app.receive_return("R1")
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 4, "reserved": 0, "available": 4})

    def test_cli_success_failure_and_outer_array_independence(self):
        self.app.restock("T", 10)
        self.app.place("A", [{"sku": "T", "quantity": 3}])
        self.app.place("B", [{"sku": "T", "quantity": 2}])
        self.app.place("C", [{"sku": "T", "quantity": 1}])
        ok_payload = self.payload([
            {"order_id": "B", "carrier": "UPS", "tracking_no": "B-1"},
            {"order_id": "A", "carrier": "DHL", "tracking_no": "A-1"},
        ])
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "ship-batch", str(ok_payload)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        shipped = json.loads(ok.stdout)
        self.assertEqual([order["order_id"] for order in shipped], ["A", "B"])
        # A single bad shipment in the request rejects the whole batch.
        bad_payload = self.payload([
            {"order_id": "C", "carrier": "DHL", "tracking_no": "C-1"},
            {"order_id": "missing", "carrier": "DHL", "tracking_no": "x"},
        ])
        bad = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "ship-batch", str(bad_payload)],
            text=True, capture_output=True)
        self.assertEqual(bad.returncode, 2, bad.stdout)
        self.assertIn("error", json.loads(bad.stderr))
        self.assertEqual(OrderDesk(self.root).get("C")["status"], "placed")
        # The outer JSON array still runs requests independently: the first
        # request succeeds and is not rolled back when the second fails.
        outer = self.root / "outer.json"
        outer.write_text(json.dumps([
            {"shipments": [{"order_id": "C", "carrier": "DHL", "tracking_no": "C-1"}]},
            {"shipments": [{"order_id": "missing", "carrier": "DHL", "tracking_no": "x"}]},
        ]), encoding="utf-8")
        stopped = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "ship-batch", str(outer)],
            text=True, capture_output=True)
        self.assertEqual(stopped.returncode, 2, stopped.stdout)
        self.assertIn("error", json.loads(stopped.stderr))
        # A and B (5 units) were shipped earlier; C's 1 unit now ships too.
        self.assertEqual(OrderDesk(self.root).get("C")["status"], "shipped")
        self.assertEqual(OrderDesk(self.root).stock("T"),
                         {"sku": "T", "on_hand": 4, "reserved": 0, "available": 4})

    def test_cli_rejects_non_object_input(self):
        path = self.root / "array.json"
        path.write_text(json.dumps([{"order_id": "A"}]), encoding="utf-8")
        failed = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "ship-batch", str(path)],
            text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2)
        self.assertIn("error", json.loads(failed.stderr))


if __name__ == "__main__":
    unittest.main()
