import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk


class ReceiveReturnTests(unittest.TestCase):
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

    def test_receive_adds_full_quantities_back_to_on_hand(self):
        self._shipped("O1", [{"sku": "T", "quantity": 4}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        # Registering the return does not touch stock.
        self.assertEqual(self.app.stock("T")["on_hand"], 6)
        result = self.app.receive_return("R1")
        self.assertEqual(result, {
            "order_id": "O1",
            "return_id": "R1",
            "lines": [{
                "sku": "T",
                "quantity": 3,
                "before": {"sku": "T", "on_hand": 6, "reserved": 0, "available": 6},
                "after": {"sku": "T", "on_hand": 9, "reserved": 0, "available": 9},
            }],
        })
        self.assertEqual(set(result), {"order_id", "return_id", "lines"})
        self.assertEqual(set(result["lines"][0]), {"sku", "quantity", "before", "after"})
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 9, "reserved": 0, "available": 9})

    def test_lines_sorted_and_reservations_untouched(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])
        self.app.record_return("O1", "R1", [
            {"sku": "T", "quantity": 1},
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 1},
        ])
        # Another open order reserves T while the return is received.
        self.app.place("O2", [{"sku": "T", "quantity": 2}])
        before_t = self.app.stock("T")
        result = self.app.receive_return("R1")
        self.assertEqual([line["sku"] for line in result["lines"]], ["C", "T"])
        self.assertEqual(result["lines"][1]["quantity"], 2)
        # Reserved stock and O2's reservation record survive; only on_hand grows.
        self.assertEqual(self.app.stock("T"), {
            "sku": "T", "on_hand": before_t["on_hand"] + 2,
            "reserved": 2, "available": before_t["on_hand"],
        })
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["reservations"]["O2"], {"T": 2})
        self.assertEqual(self.app.stock("C")["on_hand"], 10)

    def test_product_managed_after_shipment_uses_current_stock(self):
        # U ships while unmanaged; restocking it later makes receiving valid.
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}, {"sku": "U", "quantity": 4}])
        self.app.ship("O1", "DHL", "X")
        self.app.record_return("O1", "R1", [{"sku": "U", "quantity": 4}])
        with self.assertRaises(ValueError):
            self.app.receive_return("R1")
        self.app.restock("U", 1)
        result = self.app.receive_return("R1")
        self.assertEqual(result["lines"][0]["before"], {"sku": "U", "on_hand": 1, "reserved": 0, "available": 1})
        self.assertEqual(result["lines"][0]["after"], {"sku": "U", "on_hand": 5, "reserved": 0, "available": 5})

    def test_receive_does_not_manage_products_itself(self):
        self._shipped("O1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("U", raw["inventory"])

    def test_duplicate_receive_rejected_without_double_add_or_overwrite(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        first = self.app.receive_return("R1")
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.receive_return("R1")
        with self.assertRaises(ValueError):
            self.app.receive_return("  R1  ")
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.stock("T")["on_hand"], first["lines"][0]["after"]["on_hand"])
        self.assertEqual(self.app.get_return_receipt("R1"), first)

    def test_invalid_and_unknown_identifiers(self):
        self._shipped("O1", [{"sku": "T", "quantity": 1}])
        for bad in (None, 123, 1.5, b"R1", ["R1"], {"x": 1}, "   ", "\t\n", True):
            with self.assertRaises(ValueError):
                self.app.receive_return(bad)
            with self.assertRaises(ValueError):
                self.app.get_return_receipt(bad)
        with self.assertRaises(ValueError):
            self.app.receive_return("R1")
        with self.assertRaises(ValueError):
            self.app.get_return_receipt("R1")
        # Whitespace is trimmed; ids are case sensitive.
        self.app.record_return("O1", "Ra", [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.receive_return("  Ra  ")["return_id"], "Ra")
        with self.assertRaises(ValueError):
            self.app.receive_return("ra")

    def test_unmanaged_or_unknown_product_rejects_whole_receive(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}, {"sku": "U", "quantity": 2}])
        self.app.ship("O1", "DHL", "X")
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}, {"sku": "U", "quantity": 2}])
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.receive_return("R1")
        # The managed line in the same batch must not be added either.
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.stock("T")["on_hand"], 4)

    def test_failures_add_no_history_and_consume_no_sequence(self):
        self._shipped("O1", [{"sku": "T", "quantity": 1}, {"sku": "U", "quantity": 1}])
        self.app.record_return("O1", "R1", [{"sku": "U", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.receive_return("R1")
        with self.assertRaises(ValueError):
            self.app.receive_return("nope")
        events = self.app.history("O1")["events"]
        self.assertEqual([(e["sequence"], e["action"]) for e in events],
                         [(1, "place"), (2, "ship"), (3, "record-return")])

    def test_order_and_return_records_remain_unchanged(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        record = self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        order_before = self.app.get("O1")
        returns_before = self.app.get_returns("O1")
        self.app.receive_return("R1")
        self.assertEqual(self.app.get("O1"), order_before)
        self.assertEqual(self.app.get("O1")["status"], "shipped")
        self.assertEqual(self.app.get_returns("O1"), returns_before)
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["returns"]["O1"], [record])

    def test_history_records_receive_event_with_snapshot(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        receipt = self.app.receive_return("R1")
        history = self.app.history("O1")
        self.assertTrue(history["complete"])
        events = history["events"]
        self.assertEqual([(e["sequence"], e["action"]) for e in events],
                         [(1, "place"), (2, "ship"), (3, "record-return"), (4, "receive-return")])
        self.assertEqual(set(events[3]), {"sequence", "action", "result"})
        self.assertEqual(events[3]["result"], receipt)

    def test_receipt_snapshot_untouched_by_later_operations(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        snapshot = self.app.receive_return("R1")
        self.app.restock("T", 20)
        self.app.count_stock("c1", [{"sku": "T", "on_hand": 30}])
        self.assertEqual(self.app.get_return_receipt("R1"), snapshot)
        self.assertEqual(OrderDesk(self.root).get_return_receipt("R1"), snapshot)

    def test_receipt_query_creates_no_file(self):
        empty_root = Path(self.temp.name) / "empty"
        fresh = OrderDesk(empty_root)
        with self.assertRaises(ValueError):
            fresh.get_return_receipt("missing")
        self.assertFalse(empty_root.exists())

    def test_legacy_data_without_receipts_treated_as_not_received(self):
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "inventory": {"T": {"on_hand": 3, "reserved": 0}},
            "orders": {"OLD": {
                "order_id": "OLD", "status": "shipped",
                "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200}],
                "total_cents": 200, "shipment": {"carrier": "DHL", "tracking_no": "Z"},
            }},
            "returns": {"OLD": [
                {"order_id": "OLD", "return_id": "L1", "lines": [{"sku": "T", "quantity": 2}]},
            ]},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        with self.assertRaises(ValueError):
            app.get_return_receipt("L1")
        receipt = app.receive_return("L1")
        history = app.history("OLD")
        self.assertFalse(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                         [(1, "receive-return")])
        self.assertEqual(history["events"][0]["result"], receipt)
        self.assertEqual(app.stock("T")["on_hand"], 5)

    def test_legacy_order_missing_or_not_shipped_rejected(self):
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "inventory": {"T": {"on_hand": 3, "reserved": 0}},
            "orders": {"OLD": {
                "order_id": "OLD", "status": "placed",
                "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200}],
                "total_cents": 200,
            }},
            "returns": {
                "OLD": [{"order_id": "OLD", "return_id": "RP", "lines": [{"sku": "T", "quantity": 1}]}],
                "GONE": [{"order_id": "GONE", "return_id": "RG", "lines": [{"sku": "T", "quantity": 1}]}],
            },
        }
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        with self.assertRaises(ValueError):
            app.receive_return("RP")
        with self.assertRaises(ValueError):
            app.receive_return("RG")
        self.assertEqual(app.stock("T")["on_hand"], 3)

    def test_legacy_unknown_product_rejected(self):
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "inventory": {"T": {"on_hand": 3, "reserved": 0}},
            "orders": {"OLD": {
                "order_id": "OLD", "status": "shipped",
                "lines": [{"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100}],
                "total_cents": 100, "shipment": {"carrier": "DHL", "tracking_no": "Z"},
            }},
            "returns": {"OLD": [
                {"order_id": "OLD", "return_id": "L1", "lines": [{"sku": "X", "quantity": 1}]},
            ]},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            OrderDesk(self.root).receive_return("L1")

    def test_cli_receive_receipt_and_array_partial_failure(self):
        self._shipped("O1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([
            {"return_id": "R1"},
            {"return_id": "R1"},
        ]), encoding="utf-8")
        failed = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "receive-return", str(batch)],
            text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2, failed.stdout)
        self.assertIn("already received", json.loads(failed.stderr)["error"])
        # The first row succeeded and its receipt is queryable via return-receipt.
        query = self.root / "q.json"
        query.write_text(json.dumps({"return_id": " R1 "}), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "return-receipt", str(query)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        receipt = json.loads(ok.stdout)
        self.assertEqual(receipt["return_id"], "R1")
        self.assertEqual(receipt["lines"][0]["after"]["on_hand"], 8)
        # R2 was never received despite sharing the failed batch with R1's repeat.
        query.write_text(json.dumps({"return_id": "R2"}), encoding="utf-8")
        missing = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "return-receipt", str(query)],
            text=True, capture_output=True)
        self.assertEqual(missing.returncode, 2, missing.stdout)


if __name__ == "__main__":
    unittest.main()
