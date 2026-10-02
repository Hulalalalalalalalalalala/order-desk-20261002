import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk


class ReceiveReturnBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)
        self.app.add_product("U", "Unmanaged", 0)

    def _shipped(self, order_id, lines, stock=None):
        if stock is None:
            stock = {"T": 10, "C": 10}
        for sku, quantity in stock.items():
            self.app.restock(sku, quantity)
        self.app.place(order_id, lines)
        self.app.ship(order_id, "DHL", "TRK-" + order_id)

    def test_batch_receives_sorted_with_full_receipts(self):
        self._shipped("O1", [{"sku": "T", "quantity": 4}, {"sku": "C", "quantity": 2}])
        self._shipped("O2", [{"sku": "T", "quantity": 1}], stock={})
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])
        self.app.record_return("O2", "R1", [{"sku": "T", "quantity": 1}])
        result = self.app.receive_return_batch([" R2 ", "R1"])
        # Results come back sorted by return_id regardless of input order.
        self.assertEqual([receipt["return_id"] for receipt in result], ["R1", "R2"])
        by_id = {receipt["return_id"]: receipt for receipt in result}
        self.assertEqual(set(by_id["R1"]), {"order_id", "return_id", "lines"})
        self.assertEqual(by_id["R1"]["order_id"], "O2")
        self.assertEqual(by_id["R1"]["lines"], [{
            "sku": "T",
            "quantity": 1,
            "before": {"sku": "T", "on_hand": 7, "reserved": 0, "available": 7},
            "after": {"sku": "T", "on_hand": 8, "reserved": 0, "available": 8},
        }])
        # R2 was applied first (input order), so its T snapshot starts at 5.
        self.assertEqual([line["sku"] for line in by_id["R2"]["lines"]], ["C", "T"])
        self.assertEqual(by_id["R2"]["lines"][1]["before"],
                         {"sku": "T", "on_hand": 5, "reserved": 0, "available": 5})
        self.assertEqual(by_id["R2"]["lines"][1]["after"],
                         {"sku": "T", "on_hand": 7, "reserved": 0, "available": 7})
        # R1's before chains onto R2's after for the shared product.
        self.assertEqual(by_id["R1"]["lines"][0]["before"], by_id["R2"]["lines"][1]["after"])
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 8, "reserved": 0, "available": 8})
        self.assertEqual(self.app.stock("C")["on_hand"], 9)
        # Every receipt is queryable and survives a reopen.
        reopened = OrderDesk(self.root)
        for receipt in result:
            self.assertEqual(reopened.get_return_receipt(receipt["return_id"]), receipt)

    def test_shared_product_snapshots_chain_in_input_order(self):
        # On hand 5, reserved 2: receiving 2 then 1 ends at available 6.
        self.app.restock("T", 9)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.place("O2", [{"sku": "T", "quantity": 2}])
        self.app.place("O3", [{"sku": "T", "quantity": 2}])
        self.app.ship("O1", "DHL", "1")
        self.app.ship("O2", "DHL", "2")
        self.app.record_return("O1", "RA", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O2", "RB", [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 5, "reserved": 2, "available": 3})
        result = self.app.receive_return_batch(["RA", "RB"])
        by_id = {receipt["return_id"]: receipt for receipt in result}
        self.assertEqual(by_id["RA"]["lines"][0]["before"],
                         {"sku": "T", "on_hand": 5, "reserved": 2, "available": 3})
        self.assertEqual(by_id["RA"]["lines"][0]["after"],
                         {"sku": "T", "on_hand": 7, "reserved": 2, "available": 5})
        self.assertEqual(by_id["RB"]["lines"][0]["before"], by_id["RA"]["lines"][0]["after"])
        self.assertEqual(by_id["RB"]["lines"][0]["after"],
                         {"sku": "T", "on_hand": 8, "reserved": 2, "available": 6})
        # Reserved quantities and the surviving order's reservation are untouched.
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 8, "reserved": 2, "available": 6})
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["reservations"], {"O3": {"T": 2}})

    def test_invalid_batches_are_rejected_entirely(self):
        self._shipped("O1", [{"sku": "T", "quantity": 3}, {"sku": "U", "quantity": 1}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "RU", [{"sku": "U", "quantity": 1}])
        self.app.record_return("O1", "RC", [{"sku": "T", "quantity": 1}])
        self.app.cancel_return("RC")
        self.app.record_return("O1", "RD", [{"sku": "T", "quantity": 1}])
        self.app.receive_return("RD")
        before = self.app.path.read_bytes()
        invalid = [
            [],
            None,
            {},
            "R1",
            [None],
            [123],
            [True],
            [["R1"]],
            [{"x": 1}],
            ["   "],
            ["\t\n"],
            ["R1", " R1 "],
            ["R1", "R2", "R1"],
            ["missing"],
            ["RC"],
            ["RD"],
            ["RU"],
            # A bad later entry must roll back nothing even though the first is fine.
            ["R1", "missing"],
            ["R1", "R2", "RU"],
        ]
        for return_ids in invalid:
            with self.subTest(return_ids=return_ids):
                with self.assertRaises(ValueError):
                    self.app.receive_return_batch(return_ids)
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 8, "reserved": 0, "available": 8})
        # No receipts, no history events and no sequence consumption happened.
        events = self.app.history("O1")["events"]
        self.assertEqual([event["action"] for event in events],
                         ["place", "ship", "record-return", "record-return",
                          "record-return", "record-return", "cancel-return",
                          "record-return", "receive-return"])
        self.assertEqual([event["sequence"] for event in events], list(range(1, 10)))
        with self.assertRaises(ValueError):
            self.app.get_return_receipt("R1")
        with self.assertRaises(ValueError):
            self.app.get_return_receipt("R2")

    def test_failure_does_not_create_directory(self):
        missing_root = self.root / "does-not-exist"
        app = OrderDesk(missing_root)
        with self.assertRaises(ValueError):
            app.receive_return_batch([])
        with self.assertRaises(ValueError):
            app.receive_return_batch(["R1"])
        self.assertFalse(missing_root.exists())

    def test_case_sensitive_ids_are_not_duplicates(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "ra", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "RA", [{"sku": "T", "quantity": 1}])
        result = self.app.receive_return_batch(["ra", " RA "])
        self.assertEqual([receipt["return_id"] for receipt in result], ["RA", "ra"])
        self.assertEqual(self.app.stock("T")["on_hand"], 10)

    def test_paused_product_still_receives_in_batch(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        self.app.set_product_enabled("T", False)
        result = self.app.receive_return_batch(["R1"])
        self.assertEqual(result[0]["lines"][0]["after"]["on_hand"], 10)

    def test_product_managed_after_shipment_uses_current_stock(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}, {"sku": "U", "quantity": 4}])
        self.app.ship("O1", "DHL", "X")
        self.app.record_return("O1", "R1", [{"sku": "U", "quantity": 4}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        # U is unmanaged: the whole batch is rejected, R2 included.
        with self.assertRaises(ValueError):
            self.app.receive_return_batch(["R2", "R1"])
        self.assertEqual(self.app.stock("T")["on_hand"], 4)
        self.app.restock("U", 1)
        result = self.app.receive_return_batch(["R2", "R1"])
        by_id = {receipt["return_id"]: receipt for receipt in result}
        self.assertEqual(by_id["R1"]["lines"][0]["before"],
                         {"sku": "U", "on_hand": 1, "reserved": 0, "available": 1})
        self.assertEqual(by_id["R1"]["lines"][0]["after"],
                         {"sku": "U", "on_hand": 5, "reserved": 0, "available": 5})
        self.assertEqual(by_id["R2"]["lines"][0]["after"]["on_hand"], 5)

    def test_history_and_stock_events_follow_input_order(self):
        self._shipped("O1", [{"sku": "T", "quantity": 3}])
        self._shipped("O2", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O2", "R2", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R3", [{"sku": "T", "quantity": 1}])
        result = self.app.receive_return_batch(["R2", "R3", "R1"])
        by_id = {receipt["return_id"]: receipt for receipt in result}
        # O1's two events follow input order (R3 before R1) and continue the
        # order's own sequence.
        events = self.app.history("O1")["events"]
        self.assertEqual([(event["sequence"], event["action"]) for event in events],
                         [(1, "place"), (2, "ship"), (3, "record-return"),
                          (4, "record-return"), (5, "receive-return"), (6, "receive-return")])
        self.assertEqual(events[4]["result"], by_id["R3"])
        self.assertEqual(events[5]["result"], by_id["R1"])
        o2_events = self.app.history("O2")["events"]
        self.assertEqual([(event["sequence"], event["action"]) for event in o2_events],
                         [(1, "place"), (2, "ship"), (3, "record-return"), (4, "receive-return")])
        self.assertEqual(o2_events[3]["result"], by_id["R2"])
        # Stock events chain in input order with the return id as reference.
        stock_events = self.app.stock_history("T")["events"]
        receives = [event for event in stock_events if event["action"] == "receive-return"]
        self.assertEqual([event["reference_id"] for event in receives], ["R2", "R3", "R1"])
        self.assertEqual([event["sequence"] for event in receives],
                         [len(stock_events) - 2, len(stock_events) - 1, len(stock_events)])
        for earlier, later in zip(receives, receives[1:]):
            self.assertEqual(later["before"], earlier["after"])

    def test_legacy_data_without_receipts_treated_as_not_received(self):
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "inventory": {"T": {"on_hand": 3, "reserved": 0}},
            "orders": {"OLD": {
                "order_id": "OLD", "status": "shipped",
                "lines": [{"sku": "T", "quantity": 3, "unit_price_cents": 100, "subtotal_cents": 300}],
                "total_cents": 300, "shipment": {"carrier": "DHL", "tracking_no": "Z"},
            }},
            "returns": {"OLD": [
                {"order_id": "OLD", "return_id": "L1", "lines": [{"sku": "T", "quantity": 1}]},
                {"order_id": "OLD", "return_id": "L2", "lines": [{"sku": "T", "quantity": 2}]},
            ]},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        result = app.receive_return_batch(["L2", "L1"])
        self.assertEqual([receipt["return_id"] for receipt in result], ["L1", "L2"])
        self.assertEqual(app.stock("T")["on_hand"], 6)
        # The legacy order and product start fresh histories at 1, complete false.
        history = app.history("OLD")
        self.assertFalse(history["complete"])
        self.assertEqual([(event["sequence"], event["action"]) for event in history["events"]],
                         [(1, "receive-return"), (2, "receive-return")])
        stock_history = app.stock_history("T")
        self.assertFalse(stock_history["complete"])
        self.assertEqual([(event["sequence"], event["reference_id"]) for event in stock_history["events"]],
                         [(1, "L2"), (2, "L1")])

    def test_legacy_order_missing_or_not_shipped_rejects_whole_batch(self):
        self._shipped("O1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        data["orders"]["PLACED"] = {
            "order_id": "PLACED", "status": "placed",
            "lines": [{"sku": "T", "quantity": 1}], "total_cents": 100,
        }
        data["returns"]["PLACED"] = [
            {"order_id": "PLACED", "return_id": "RP", "lines": [{"sku": "T", "quantity": 1}]},
        ]
        data["returns"]["GONE"] = [
            {"order_id": "GONE", "return_id": "RG", "lines": [{"sku": "T", "quantity": 1}]},
        ]
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.receive_return_batch(["R1", "RP"])
        with self.assertRaises(ValueError):
            self.app.receive_return_batch(["R1", "RG"])
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.stock("T")["on_hand"], 9)

    def test_received_returns_show_on_worklist_and_reject_repeat_and_cancel(self):
        self._shipped("O1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        self.app.receive_return_batch(["R1", "R2"])
        stages = {entry["return_id"]: entry["stage"] for entry in self.app.return_worklist("all")}
        self.assertEqual(stages, {"R1": "received", "R2": "received"})
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.receive_return("R1")
        with self.assertRaises(ValueError):
            self.app.receive_return_batch(["R2"])
        with self.assertRaises(ValueError):
            self.app.cancel_return("R1")
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_order_and_return_records_remain_unchanged(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        record = self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        order_before = self.app.get("O1")
        returns_before = self.app.get_returns("O1")
        self.app.receive_return_batch(["R1"])
        self.assertEqual(self.app.get("O1"), order_before)
        self.assertEqual(self.app.get_returns("O1"), returns_before)
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["returns"]["O1"], [record])

    def test_receipt_snapshots_untouched_by_later_operations(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        snapshots = self.app.receive_return_batch(["R1", "R2"])
        self.app.restock("T", 20)
        self.app.count_stock("c1", [{"sku": "T", "on_hand": 30}])
        reopened = OrderDesk(self.root)
        for receipt in snapshots:
            self.assertEqual(reopened.get_return_receipt(receipt["return_id"]), receipt)

    def test_single_receive_and_other_entries_unchanged(self):
        self._shipped("O1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        single = self.app.receive_return("R1")
        self.assertEqual(single["return_id"], "R1")
        batch = self.app.receive_return_batch(["R2"])
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0]["return_id"], "R2")
        self.assertEqual(batch[0]["lines"][0]["before"], single["lines"][0]["after"])

    def test_cli_success_failure_and_outer_array_independence(self):
        self._shipped("O1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R3", [{"sku": "T", "quantity": 1}])
        ok_payload = self.root / "ok.json"
        ok_payload.write_text(json.dumps({"return_ids": ["R2", "R1"]}), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "receive-return-batch", str(ok_payload)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        receipts = json.loads(ok.stdout)
        self.assertEqual([receipt["return_id"] for receipt in receipts], ["R1", "R2"])
        # A single bad id in the request rejects the whole batch.
        bad_payload = self.root / "bad.json"
        bad_payload.write_text(json.dumps({"return_ids": ["R3", "missing"]}), encoding="utf-8")
        bad = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "receive-return-batch", str(bad_payload)],
            text=True, capture_output=True)
        self.assertEqual(bad.returncode, 2, bad.stdout)
        self.assertIn("error", json.loads(bad.stderr))
        with self.assertRaises(ValueError):
            OrderDesk(self.root).get_return_receipt("R3")
        # The outer JSON array still runs requests independently: the first
        # request succeeds and is not rolled back when the second fails.
        outer = self.root / "outer.json"
        outer.write_text(json.dumps([
            {"return_ids": ["R3"]},
            {"return_ids": ["missing"]},
        ]), encoding="utf-8")
        stopped = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "receive-return-batch", str(outer)],
            text=True, capture_output=True)
        self.assertEqual(stopped.returncode, 2, stopped.stdout)
        self.assertIn("error", json.loads(stopped.stderr))
        receipt = OrderDesk(self.root).get_return_receipt("R3")
        self.assertEqual(receipt["lines"][0]["after"]["on_hand"], 10)

    def test_cli_rejects_non_object_input(self):
        path = self.root / "array.json"
        path.write_text(json.dumps([{"return_id": "R1"}]), encoding="utf-8")
        failed = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "receive-return-batch", str(path)],
            text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2)
        self.assertIn("error", json.loads(failed.stderr))


if __name__ == "__main__":
    unittest.main()
