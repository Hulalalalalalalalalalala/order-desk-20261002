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
        self.app.restock("T", 10)
        self.app.restock("C", 5)

    def _shipped_order_with_return(self):
        self.app.place("O1", [
            {"sku": "T", "quantity": 3},
            {"sku": "C", "quantity": 2},
        ])
        self.app.ship("O1", "DHL", "X-1")
        # Stock after shipment: T 7, C 3.
        self.app.record_return("O1", "R1", [
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 2},
        ])

    def test_receive_adds_full_quantities_to_on_hand(self):
        self._shipped_order_with_return()
        result = self.app.receive_return("R1")
        self.assertEqual(set(result), {"order_id", "return_id", "lines"})
        self.assertEqual(result["order_id"], "O1")
        self.assertEqual(result["return_id"], "R1")
        # Lines follow the return record: sku ascending.
        self.assertEqual([line["sku"] for line in result["lines"]], ["C", "T"])
        self.assertEqual([set(line) for line in result["lines"]],
                         [{"sku", "quantity", "before", "after"}] * 2)
        self.assertEqual(result["lines"][0], {
            "sku": "C",
            "quantity": 1,
            "before": {"sku": "C", "on_hand": 3, "reserved": 0, "available": 3},
            "after": {"sku": "C", "on_hand": 4, "reserved": 0, "available": 4},
        })
        self.assertEqual(result["lines"][1], {
            "sku": "T",
            "quantity": 2,
            "before": {"sku": "T", "on_hand": 7, "reserved": 0, "available": 7},
            "after": {"sku": "T", "on_hand": 9, "reserved": 0, "available": 9},
        })
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 9, "reserved": 0, "available": 9})
        self.assertEqual(self.app.stock("C"),
                         {"sku": "C", "on_hand": 4, "reserved": 0, "available": 4})

    def test_receive_keeps_reservations_intact(self):
        self._shipped_order_with_return()
        # A later placed order holds a reservation while stock is received.
        self.app.place("O2", [{"sku": "T", "quantity": 4}])
        before_t = self.app.stock("T")
        self.assertEqual(before_t, {"sku": "T", "on_hand": 7, "reserved": 4, "available": 3})
        result = self.app.receive_return("R1")
        t_line = next(line for line in result["lines"] if line["sku"] == "T")
        self.assertEqual(t_line["before"], {"sku": "T", "on_hand": 7, "reserved": 4, "available": 3})
        self.assertEqual(t_line["after"], {"sku": "T", "on_hand": 9, "reserved": 4, "available": 5})
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 9, "reserved": 4, "available": 5})
        # Per-order reservation record is untouched.
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["reservations"]["O2"], {"T": 4})

    def test_duplicate_receive_rejected_without_double_restock(self):
        self._shipped_order_with_return()
        first = self.app.receive_return("R1")
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.receive_return("R1")
        with self.assertRaises(ValueError):
            self.app.receive_return("  R1  ")
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.stock("T")["on_hand"], 9)
        self.assertEqual(self.app.stock("C")["on_hand"], 4)
        # The stored receipt is not overwritten.
        self.assertEqual(self.app.get_return_receipt("R1"), first)

    def test_return_id_validation(self):
        self._shipped_order_with_return()
        before = self.app.path.read_bytes()
        for bad in (None, 123, 1.5, True, b"R1", ["R1"], {"x": 1}, "   ", "\t\n"):
            with self.assertRaises(ValueError):
                self.app.receive_return(bad)
            with self.assertRaises(ValueError):
                self.app.get_return_receipt(bad)
        with self.assertRaises(ValueError):
            self.app.receive_return("ghost")
        with self.assertRaises(ValueError):
            self.app.get_return_receipt("ghost")
        # Not yet received is distinct from unknown.
        with self.assertRaises(ValueError):
            self.app.get_return_receipt("R1")
        self.assertEqual(self.app.path.read_bytes(), before)
        # Whitespace is trimmed; ids are case sensitive.
        self.assertEqual(self.app.receive_return("  R1  ")["return_id"], "R1")
        with self.assertRaises(ValueError):
            self.app.receive_return("r1")

    def test_unknown_and_unmanaged_products_reject_whole_receive(self):
        self.app.place("O1", [{"sku": "U", "quantity": 2}, {"sku": "T", "quantity": 1}])
        self.app.ship("O1", "DHL", "X-1")
        # U was never restocked: unmanaged at receive time.
        self.app.record_return("O1", "RU", [{"sku": "U", "quantity": 2}])
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.receive_return("RU")
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertIsNone(self.app.stock("U")["on_hand"])
        # A bad line later in the list must not add stock for earlier lines.
        self.app.place("O2", [{"sku": "T", "quantity": 1}, {"sku": "U", "quantity": 1}])
        self.app.ship("O2", "DHL", "X-2")
        self.app.record_return("O2", "RM", [
            {"sku": "T", "quantity": 1},
            {"sku": "U", "quantity": 1},
        ])
        t_on_hand_before = self.app.stock("T")["on_hand"]
        with self.assertRaises(ValueError):
            self.app.receive_return("RM")
        self.assertEqual(self.app.stock("T")["on_hand"], t_on_hand_before)
        # Remove the product from the catalog to simulate an unknown product.
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        del raw["products"]["U"]
        self.app._write(raw)
        with self.assertRaises(ValueError):
            self.app.receive_return("RU")
        self.assertEqual(json.loads(self.app.path.read_text(encoding="utf-8")), raw)

    def test_product_managed_after_shipment_uses_current_inventory(self):
        # Unmanaged at order/ship time: no reservation and no stock deduction.
        self.app.place("O1", [{"sku": "U", "quantity": 3}])
        self.app.ship("O1", "DHL", "X-1")
        self.app.record_return("O1", "R1", [{"sku": "U", "quantity": 3}])
        self.app.restock("U", 2)
        result = self.app.receive_return("R1")
        self.assertEqual(result["lines"][0], {
            "sku": "U",
            "quantity": 3,
            "before": {"sku": "U", "on_hand": 2, "reserved": 0, "available": 2},
            "after": {"sku": "U", "on_hand": 5, "reserved": 0, "available": 5},
        })
        self.assertEqual(self.app.stock("U")["on_hand"], 5)

    def test_legacy_return_without_receipt_is_receivable_once(self):
        order = {"order_id": "OLD", "status": "shipped",
                 "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200}],
                 "total_cents": 200,
                 "shipment": {"carrier": "DHL", "tracking_no": "Z-9"}}
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "inventory": {"T": {"on_hand": 8, "reserved": 0}},
            "orders": {"OLD": order},
            "returns": {"OLD": [{"order_id": "OLD", "return_id": "L1",
                                 "lines": [{"sku": "T", "quantity": 2}]}]},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        result = app.receive_return("L1")
        self.assertEqual(result["order_id"], "OLD")
        self.assertEqual(result["lines"][0]["before"]["on_hand"], 8)
        self.assertEqual(result["lines"][0]["after"]["on_hand"], 10)
        with self.assertRaises(ValueError):
            app.receive_return("L1")

    def _legacy_order(self, status):
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "inventory": {"T": {"on_hand": 8, "reserved": 0}},
            "orders": {"OLD": {"order_id": "OLD", "status": status, "lines": [], "total_cents": 0}},
            "returns": {"OLD": [{"order_id": "OLD", "return_id": "L1",
                                 "lines": [{"sku": "T", "quantity": 1}]}]},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        self.app.path.write_text(json.dumps(data), encoding="utf-8")

    def test_receive_requires_shipped_order(self):
        self._legacy_order("placed")
        app = OrderDesk(self.root)
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            app.receive_return("L1")
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_receive_requires_existing_order(self):
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "inventory": {"T": {"on_hand": 8, "reserved": 0}},
            "orders": {},
            "returns": {"GONE": [{"order_id": "GONE", "return_id": "L1",
                                  "lines": [{"sku": "T", "quantity": 1}]}]},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        with self.assertRaises(ValueError):
            app.receive_return("L1")

    def test_history_records_receive_with_continuing_sequences(self):
        self._shipped_order_with_return()
        self.app.receive_return("R1")
        history = self.app.history("O1")
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                         [(1, "place"), (2, "ship"), (3, "record-return"), (4, "receive-return")])
        snapshot = history["events"][3]["result"]
        self.assertEqual(snapshot, self.app.get_return_receipt("R1"))
        # Failed duplicate receives consume no sequence.
        with self.assertRaises(ValueError):
            self.app.receive_return("R1")
        self.assertEqual([e["sequence"] for e in self.app.history("O1")["events"]], [1, 2, 3, 4])

    def test_legacy_order_history_starts_at_one_complete_false(self):
        self._legacy_order("shipped")
        # Restore a valid shipped order for this case.
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        raw["orders"]["OLD"]["shipment"] = {"carrier": "DHL", "tracking_no": "Z"}
        raw["orders"]["OLD"]["lines"] = [
            {"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100}]
        self.app._write(raw)
        app = OrderDesk(self.root)
        app.receive_return("L1")
        history = app.history("OLD")
        self.assertFalse(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                         [(1, "receive-return")])
        self.assertEqual(OrderDesk(self.root).history("OLD")["complete"], False)

    def test_receipt_snapshot_immutable_and_persists(self):
        self._shipped_order_with_return()
        snapshot = self.app.receive_return("R1")
        self.app.restock("T", 20)
        self.app.place("O3", [{"sku": "T", "quantity": 2}])
        self.app.count_stock("CNT", [{"sku": "T", "on_hand": 50}])
        self.assertEqual(self.app.get_return_receipt("R1"), snapshot)
        self.assertEqual(OrderDesk(self.root).get_return_receipt("R1"), snapshot)

    def test_receipt_query_creates_no_directory(self):
        empty = self.root / "empty"
        app = OrderDesk(empty)
        with self.assertRaises(ValueError):
            app.get_return_receipt("ghost")
        self.assertFalse(empty.exists())

    def test_record_and_returns_outputs_and_order_unchanged(self):
        self._shipped_order_with_return()
        record = self.app.get_returns("O1")["records"][0]
        order_before = self.app.get("O1")
        self.app.receive_return("R1")
        # record-return / returns quantity semantics stay the same.
        returns = self.app.get_returns("O1")
        self.assertEqual(returns["records"], [record])
        self.assertEqual(returns["remaining"], [
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 1},
        ])
        order_after = self.app.get("O1")
        self.assertEqual(order_after, order_before)
        self.assertEqual(order_after["status"], "shipped")
        self.assertEqual(order_after["shipment"], {"carrier": "DHL", "tracking_no": "X-1"})

    def test_each_return_received_independently(self):
        self.app.place("O1", [{"sku": "T", "quantity": 5}])
        self.app.ship("O1", "DHL", "1")
        self.app.record_return("O1", "RA", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "RB", [{"sku": "T", "quantity": 3}])
        self.app.receive_return("RA")
        self.assertEqual(self.app.stock("T")["on_hand"], 7)
        self.app.receive_return("RB")
        self.assertEqual(self.app.stock("T")["on_hand"], 10)
        with self.assertRaises(ValueError):
            self.app.receive_return("RA")
        with self.assertRaises(ValueError):
            self.app.receive_return("RB")
        self.assertEqual([e["action"] for e in self.app.history("O1")["events"]],
                         ["place", "ship", "record-return", "record-return",
                          "receive-return", "receive-return"])

    def test_cli_receive_and_receipt_and_array_partial_failure(self):
        self._shipped_order_with_return()
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([
            {"return_id": "R1"},
            {"return_id": "R1"},
        ]), encoding="utf-8")
        failed = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "receive-return", str(batch)],
            text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2, failed.stdout)
        self.assertIn("already received", json.loads(failed.stderr)["error"])
        # Stock was added exactly once.
        self.assertEqual(OrderDesk(self.root).stock("T")["on_hand"], 9)
        query = self.root / "q.json"
        query.write_text(json.dumps({"return_id": " R1 "}), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "return-receipt", str(query)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual(result["order_id"], "O1")
        self.assertEqual(result["lines"][1]["after"]["on_hand"], 9)
        for bad in ("ghost", "   "):
            query.write_text(json.dumps({"return_id": bad}), encoding="utf-8")
            rejected = subprocess.run(
                [sys.executable, "-m", "order_desk", "--root", str(self.root),
                 "return-receipt", str(query)],
                text=True, capture_output=True)
            self.assertEqual(rejected.returncode, 2, rejected.stdout)


if __name__ == "__main__":
    unittest.main()
