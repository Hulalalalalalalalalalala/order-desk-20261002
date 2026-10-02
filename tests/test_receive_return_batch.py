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
        for sku, quantity in (stock or {"T": 10, "C": 10}).items():
            self.app.restock(sku, quantity)
        self.app.place(order_id, lines)
        self.app.ship(order_id, "DHL", "TRK-" + order_id)

    def test_batch_receives_sorted_with_chained_snapshots(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        self._shipped("O2", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O2", "R1", [{"sku": "T", "quantity": 1}])
        # T: 20 restocked, 3 shipped away, so 17 on hand before the batch.
        result = self.app.receive_return_batch(["R2", "R1"])
        # The result is sorted by return_id, not by input order.
        self.assertEqual([receipt["return_id"] for receipt in result], ["R1", "R2"])
        by_id = {receipt["return_id"]: receipt for receipt in result}
        self.assertEqual(set(by_id["R1"]), {"order_id", "return_id", "lines"})
        self.assertEqual(set(by_id["R1"]["lines"][0]), {"sku", "quantity", "before", "after"})
        # Snapshots chain in input order: R2 was received before R1.
        self.assertEqual(by_id["R2"]["lines"][0]["before"],
                         {"sku": "T", "on_hand": 17, "reserved": 0, "available": 17})
        self.assertEqual(by_id["R2"]["lines"][0]["after"],
                         {"sku": "T", "on_hand": 19, "reserved": 0, "available": 19})
        self.assertEqual(by_id["R1"]["lines"][0]["before"], by_id["R2"]["lines"][0]["after"])
        self.assertEqual(by_id["R1"]["lines"][0]["after"],
                         {"sku": "T", "on_hand": 20, "reserved": 0, "available": 20})
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 20, "reserved": 0, "available": 20})
        # Every receipt is queryable and matches the batch result.
        self.assertEqual(self.app.get_return_receipt("R1"), by_id["R1"])
        self.assertEqual(self.app.get_return_receipt("R2"), by_id["R2"])
        # Stock events follow input order and chain before/after.
        events = [e for e in self.app.stock_history("T")["events"]
                  if e["action"] == "receive-return"]
        self.assertEqual([e["reference_id"] for e in events], ["R2", "R1"])
        self.assertEqual(events[0]["after"], events[1]["before"])
        self.assertEqual(set(events[0]), {"sequence", "action", "reference_id", "before", "after"})

    def test_reserved_stock_survives_and_available_grows(self):
        # Legacy data: 5 on hand, 2 reserved, two shipped orders with returns.
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "inventory": {"T": {"on_hand": 5, "reserved": 2}},
            "orders": {
                "O1": {"order_id": "O1", "status": "shipped",
                       "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100,
                                  "subtotal_cents": 200}],
                       "total_cents": 200, "shipment": {"carrier": "DHL", "tracking_no": "1"}},
                "O2": {"order_id": "O2", "status": "shipped",
                       "lines": [{"sku": "T", "quantity": 1, "unit_price_cents": 100,
                                  "subtotal_cents": 100}],
                       "total_cents": 100, "shipment": {"carrier": "DHL", "tracking_no": "2"}},
            },
            "returns": {
                "O1": [{"order_id": "O1", "return_id": "R1", "lines": [{"sku": "T", "quantity": 2}]}],
                "O2": [{"order_id": "O2", "return_id": "R2", "lines": [{"sku": "T", "quantity": 1}]}],
            },
        }
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        result = app.receive_return_batch(["R1", "R2"])
        self.assertEqual([receipt["return_id"] for receipt in result], ["R1", "R2"])
        self.assertEqual(result[0]["lines"][0]["before"],
                         {"sku": "T", "on_hand": 5, "reserved": 2, "available": 3})
        self.assertEqual(result[0]["lines"][0]["after"],
                         {"sku": "T", "on_hand": 7, "reserved": 2, "available": 5})
        self.assertEqual(result[1]["lines"][0]["before"], result[0]["lines"][0]["after"])
        self.assertEqual(result[1]["lines"][0]["after"],
                         {"sku": "T", "on_hand": 8, "reserved": 2, "available": 6})
        # Reserved is untouched; only on_hand and available grow.
        self.assertEqual(app.stock("T"), {"sku": "T", "on_hand": 8, "reserved": 2, "available": 6})
        # Legacy orders and the legacy-managed product start fresh histories
        # at 1 with complete False; nothing is backfilled.
        for order_id, return_id in (("O1", "R1"), ("O2", "R2")):
            history = app.history(order_id)
            self.assertFalse(history["complete"])
            self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                             [(1, "receive-return")])
            self.assertEqual(history["events"][0]["result"],
                             app.get_return_receipt(return_id))
        stock_history = app.stock_history("T")
        self.assertFalse(stock_history["complete"])
        self.assertEqual([(e["sequence"], e["reference_id"]) for e in stock_history["events"]],
                         [(1, "R1"), (2, "R2")])

    def test_same_order_events_follow_input_order(self):
        self._shipped("O1", [{"sku": "T", "quantity": 4}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 2}])
        result = self.app.receive_return_batch(["R2", "R1"])
        self.assertEqual([receipt["return_id"] for receipt in result], ["R1", "R2"])
        history = self.app.history("O1")
        self.assertTrue(history["complete"])
        events = history["events"]
        self.assertEqual([(e["sequence"], e["action"]) for e in events],
                         [(1, "place"), (2, "ship"), (3, "record-return"),
                          (4, "record-return"), (5, "receive-return"), (6, "receive-return")])
        # Events follow the input order even though the result is sorted.
        self.assertEqual(events[4]["result"]["return_id"], "R2")
        self.assertEqual(events[5]["result"]["return_id"], "R1")
        self.assertEqual(events[4]["result"], self.app.get_return_receipt("R2"))
        self.assertEqual(events[5]["result"], self.app.get_return_receipt("R1"))

    def test_last_failure_rejects_whole_batch(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.receive_return_batch(["R1", "missing"])
        with self.assertRaises(ValueError):
            self.app.receive_return_batch(["missing", "R1"])
        # File, stock, receipts and history are all untouched.
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.stock("T")["on_hand"], 8)
        with self.assertRaises(ValueError):
            self.app.get_return_receipt("R1")
        events = self.app.history("O1")["events"]
        self.assertEqual([e["action"] for e in events],
                         ["place", "ship", "record-return", "record-return"])
        # The batch can be retried once the problem is gone.
        self.assertEqual(len(self.app.receive_return_batch(["R1", "R2"])), 2)

    def test_invalid_lists_and_identifiers(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "ra", [{"sku": "T", "quantity": 1}])
        before = self.app.path.read_bytes()
        bad_batches = [
            [], None, {}, "R1", 0, True,
            [None], [123], [1.5], [b"R1"], [["R1"]], [{"x": 1}], [True], [""], ["   "], ["\t\n"],
            ["R1", " R1 "],      # duplicate after trimming
            ["ra", "RA", "ra"],  # case-sensitive ids are distinct; exact repeat rejects
        ]
        for batch in bad_batches:
            with self.subTest(batch=batch):
                with self.assertRaises(ValueError):
                    self.app.receive_return_batch(batch)
        self.assertEqual(self.app.path.read_bytes(), before)
        # Ids are case sensitive: "RA" is unknown, so the batch fails.
        with self.assertRaises(ValueError):
            self.app.receive_return_batch(["ra", "RA"])
        self.assertEqual(self.app.path.read_bytes(), before)
        # Trimming applies per entry.
        result = self.app.receive_return_batch([" R1 ", " ra "])
        self.assertEqual([receipt["return_id"] for receipt in result], ["R1", "ra"])

    def test_cancelled_received_and_unknown_rejected(self):
        self._shipped("O1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R3", [{"sku": "T", "quantity": 1}])
        self.app.receive_return("R1")
        self.app.cancel_return("R2")
        before = self.app.path.read_bytes()
        for batch in (["R3", "R1"], ["R3", "R2"], ["R3", "nope"], ["R1"], ["R2"]):
            with self.subTest(batch=batch):
                with self.assertRaises(ValueError):
                    self.app.receive_return_batch(batch)
        self.assertEqual(self.app.path.read_bytes(), before)
        # R3 is still pending and can be received afterwards.
        self.assertEqual(self.app.receive_return_batch(["R3"])[0]["return_id"], "R3")

    def test_order_missing_or_not_shipped_rejects_batch(self):
        self._shipped("O1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "ROK", [{"sku": "T", "quantity": 1}])
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        data["orders"]["OP"] = {"order_id": "OP", "status": "placed",
                                "lines": [{"sku": "T", "quantity": 1}], "total_cents": 100}
        data["returns"]["OP"] = [{"order_id": "OP", "return_id": "RP",
                                  "lines": [{"sku": "T", "quantity": 1}]}]
        data["returns"]["GONE"] = [{"order_id": "GONE", "return_id": "RG",
                                    "lines": [{"sku": "T", "quantity": 1}]}]
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        before = self.app.path.read_bytes()
        for batch in (["ROK", "RP"], ["ROK", "RG"]):
            with self.subTest(batch=batch):
                with self.assertRaises(ValueError):
                    self.app.receive_return_batch(batch)
        self.assertEqual(self.app.path.read_bytes(), before)
        with self.assertRaises(ValueError):
            self.app.get_return_receipt("ROK")

    def test_unmanaged_or_unknown_product_rejects_whole_batch(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}, {"sku": "U", "quantity": 2}])
        self.app.ship("O1", "DHL", "X")
        self._shipped("O2", [{"sku": "T", "quantity": 1}], stock={"T": 5})
        self.app.record_return("O1", "R1", [{"sku": "U", "quantity": 2}])
        self.app.record_return("O2", "R2", [{"sku": "T", "quantity": 1}])
        before = self.app.path.read_bytes()
        # The valid entry comes first; the unmanaged product in the last entry
        # still rejects the whole batch.
        with self.assertRaises(ValueError):
            self.app.receive_return_batch(["R2", "R1"])
        self.assertEqual(self.app.path.read_bytes(), before)
        with self.assertRaises(ValueError):
            self.app.get_return_receipt("R2")
        # A product missing from the catalog rejects the batch too.
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        data["returns"]["O2"].append({"order_id": "O2", "return_id": "RX",
                                      "lines": [{"sku": "X", "quantity": 1}]})
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.receive_return_batch(["R2", "RX"])
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_paused_product_still_received(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.set_product_enabled("T", False)
        result = self.app.receive_return_batch(["R1"])
        self.assertEqual(result[0]["lines"][0]["after"]["on_hand"], 9)
        self.assertEqual(self.app.stock("T")["on_hand"], 9)

    def test_product_managed_after_shipment_uses_current_stock(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}, {"sku": "U", "quantity": 4}])
        self.app.ship("O1", "DHL", "X")
        self.app.record_return("O1", "R1", [{"sku": "U", "quantity": 4}])
        self.app.restock("U", 1)
        result = self.app.receive_return_batch(["R1"])
        self.assertEqual(result[0]["lines"][0]["before"],
                         {"sku": "U", "on_hand": 1, "reserved": 0, "available": 1})
        self.assertEqual(result[0]["lines"][0]["after"],
                         {"sku": "U", "on_hand": 5, "reserved": 0, "available": 5})

    def test_reservations_orders_and_return_records_untouched(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])
        record = self.app.record_return("O1", "R1", [
            {"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])
        self.app.place("O2", [{"sku": "T", "quantity": 2}])
        order_before = self.app.get("O1")
        returns_before = self.app.get_returns("O1")
        result = self.app.receive_return_batch(["R1"])
        # Lines keep the single-receive structure and sku ordering.
        self.assertEqual([line["sku"] for line in result[0]["lines"]], ["C", "T"])
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["reservations"]["O2"], {"T": 2})
        self.assertEqual(self.app.stock("T")["reserved"], 2)
        self.assertEqual(self.app.get("O1"), order_before)
        self.assertEqual(self.app.get_returns("O1"), returns_before)
        self.assertEqual(raw["returns"]["O1"], [record])

    def test_failure_does_not_create_directory(self):
        missing = self.root / "does-not-exist"
        app = OrderDesk(missing)
        with self.assertRaises(ValueError):
            app.receive_return_batch([])
        with self.assertRaises(ValueError):
            app.receive_return_batch(["R1"])
        self.assertFalse(missing.exists())

    def test_receipts_survive_reopen_and_later_operations(self):
        self._shipped("O1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        result = self.app.receive_return_batch(["R1", "R2"])
        self.app.restock("T", 20)
        self.app.count_stock("c1", [{"sku": "T", "on_hand": 30}])
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.get_return_receipt("R1"), result[0])
        self.assertEqual(reopened.get_return_receipt("R2"), result[1])

    def test_worklist_and_single_receive_rules_after_batch(self):
        self._shipped("O1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        self.app.receive_return_batch(["R1"])
        stages = {entry["return_id"]: entry["stage"]
                  for entry in self.app.return_worklist("all")}
        self.assertEqual(stages, {"R1": "received", "R2": "pending"})
        # Repeat receive and cancel of a received return keep failing.
        with self.assertRaises(ValueError):
            self.app.receive_return("R1")
        with self.assertRaises(ValueError):
            self.app.receive_return_batch(["R1"])
        with self.assertRaises(ValueError):
            self.app.cancel_return("R1")
        # A pending return can still be received singly.
        self.assertEqual(self.app.receive_return("R2")["return_id"], "R2")

    def test_cli_success_error_and_outer_array_independence(self):
        self._shipped("O1", [{"sku": "T", "quantity": 3}])
        self._shipped("O2", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O2", "R3", [{"sku": "T", "quantity": 1}])
        payload = self.root / "batch.json"
        payload.write_text(json.dumps({"return_ids": ["R2", "R1"]}), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "receive-return-batch", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual([receipt["return_id"] for receipt in result], ["R1", "R2"])
        # A bad id inside one request rejects that whole request.
        bad = self.root / "bad.json"
        bad.write_text(json.dumps({"return_ids": ["R3", "missing"]}), encoding="utf-8")
        failed = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "receive-return-batch", str(bad)],
            text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2, failed.stdout)
        self.assertIn("error", json.loads(failed.stderr))
        with self.assertRaises(ValueError):
            OrderDesk(self.root).get_return_receipt("R3")
        # The outer JSON array runs requests independently: the first succeeds
        # and is not rolled back when the second fails.
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
        self.assertEqual(receipt["return_id"], "R3")


if __name__ == "__main__":
    unittest.main()
