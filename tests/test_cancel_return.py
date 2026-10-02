import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk


class CancelReturnTests(unittest.TestCase):
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

    def test_cancel_returns_original_record_snapshot(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [
            {"sku": "T", "quantity": 1},
            {"sku": "T", "quantity": 2},
        ])
        result = self.app.cancel_return("R1")
        self.assertEqual(result, {
            "order_id": "O1",
            "return_id": "R1",
            "lines": [{"sku": "T", "quantity": 3}],
        })
        self.assertEqual(set(result), {"order_id", "return_id", "lines"})
        self.assertEqual(set(result["lines"][0]), {"sku", "quantity"})

    def test_lines_merged_sorted_and_only_sku_quantity(self):
        self._shipped("O1", [{"sku": "T", "quantity": 4}, {"sku": "C", "quantity": 3}])
        self.app.record_return("O1", "R1", [
            {"sku": "T", "quantity": 1},
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 1},
        ])
        result = self.app.cancel_return("R1")
        self.assertEqual([line["sku"] for line in result["lines"]], ["C", "T"])
        self.assertEqual(result["lines"][1]["quantity"], 2)

    def test_remaining_restored_and_other_returns_unchanged(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}, {"sku": "C", "quantity": 2}])
        before = self.app.get_returns("O1")
        self.assertEqual(before["remaining"], [
            {"sku": "C", "quantity": 0},
            {"sku": "T", "quantity": 1},
        ])
        self.app.cancel_return("R1")
        after = self.app.get_returns("O1")
        self.assertEqual([r["return_id"] for r in after["records"]], ["R2"])
        self.assertEqual(after["records"], [
            {"order_id": "O1", "return_id": "R2",
             "lines": [{"sku": "C", "quantity": 2}, {"sku": "T", "quantity": 1}]},
        ])
        # Freed quantity returns; the full original catalog with zero rows stays.
        self.assertEqual(after["remaining"], [
            {"sku": "C", "quantity": 0},
            {"sku": "T", "quantity": 4},
        ])

    def test_cancelling_last_return_keeps_order_queryable_with_empty_records(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.app.cancel_return("R1")
        view = self.app.get_returns("O1")
        self.assertEqual(view["records"], [])
        self.assertEqual(view["remaining"], [{"sku": "T", "quantity": 5}])

    def test_stock_reservations_order_and_shipment_untouched(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        # Another open order holds a reservation while the cancellation happens.
        self.app.place("O2", [{"sku": "T", "quantity": 2}])
        order_before = self.app.get("O1")
        stock_t = self.app.stock("T")
        stock_c = self.app.stock("C")
        self.app.cancel_return("R1")
        self.assertEqual(self.app.get("O1"), order_before)
        self.assertEqual(self.app.get("O1")["status"], "shipped")
        self.assertEqual(self.app.get("O1")["shipment"], {"carrier": "DHL", "tracking_no": "TRK-O1"})
        self.assertEqual(self.app.stock("T"), stock_t)
        self.assertEqual(self.app.stock("C"), stock_c)
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["reservations"]["O2"], {"T": 2})

    def test_received_return_cannot_be_cancelled(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        receipt = self.app.receive_return("R1")
        on_hand = self.app.stock("T")["on_hand"]
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.cancel_return("R1")
        self.assertEqual(self.app.path.read_bytes(), before)
        # Receipt and stock stay in place; record still lists the return.
        self.assertEqual(self.app.get_return_receipt("R1"), receipt)
        self.assertEqual(self.app.stock("T")["on_hand"], on_hand)
        self.assertEqual([r["return_id"] for r in self.app.get_returns("O1")["records"]], ["R1"])

    def test_cancelled_return_cannot_be_cancelled_or_received_again(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.cancel_return("R1")
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.cancel_return("R1")
        with self.assertRaises(ValueError):
            self.app.cancel_return("  R1  ")
        with self.assertRaises(ValueError):
            self.app.receive_return("R1")
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_return_id_stays_occupied_across_orders(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.cancel_return("R1")
        self._shipped("O2", [{"sku": "T", "quantity": 2}])
        with self.assertRaises(ValueError):
            self.app.record_return("O2", "R1", [{"sku": "T", "quantity": 1}])
        # The freed quantity can be registered again under a fresh id.
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 2}])
        receipt = self.app.receive_return("R2")
        self.assertEqual(receipt["return_id"], "R2")

    def test_whitespace_trimmed_and_case_sensitive(self):
        self._shipped("O1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "Ra", [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.cancel_return("  Ra  ")["return_id"], "Ra")
        with self.assertRaises(ValueError):
            self.app.cancel_return("ra")
        with self.assertRaises(ValueError):
            self.app.receive_return("Ra")

    def test_invalid_and_unknown_identifiers(self):
        self._shipped("O1", [{"sku": "T", "quantity": 1}])
        for bad in (None, 123, 1.5, b"R1", ["R1"], {"x": 1}, "   ", "\t\n", True):
            with self.assertRaises(ValueError):
                self.app.cancel_return(bad)
        with self.assertRaises(ValueError):
            self.app.cancel_return("R1")

    def test_failures_rewrite_nothing_and_consume_no_sequence(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.cancel_return("nope")
        with self.assertRaises(ValueError):
            self.app.cancel_return("   ")
        self.assertEqual(self.app.path.read_bytes(), before)
        self.app.cancel_return("R1")
        with self.assertRaises(ValueError):
            self.app.cancel_return("R1")
        with self.assertRaises(ValueError):
            self.app.receive_return("R1")
        events = self.app.history("O1")["events"]
        self.assertEqual([(e["sequence"], e["action"]) for e in events],
                         [(1, "place"), (2, "ship"), (3, "record-return"), (4, "cancel-return")])

    def test_history_event_snapshot_and_sequence(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])
        self.app.record_return("O1", "R1", [
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 1},
        ])
        result = self.app.cancel_return("R1")
        history = self.app.history("O1")
        self.assertTrue(history["complete"])
        events = history["events"]
        self.assertEqual([(e["sequence"], e["action"]) for e in events],
                         [(1, "place"), (2, "ship"), (3, "record-return"), (4, "cancel-return")])
        self.assertEqual(set(events[3]), {"sequence", "action", "result"})
        self.assertEqual(events[3]["result"], result)
        # Old snapshots are untouched.
        self.assertEqual(events[2]["result"]["return_id"], "R1")

    def test_legacy_order_without_history_starts_at_one_complete_false(self):
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "inventory": {"T": {"on_hand": 3, "reserved": 0}},
            "orders": {"OLD": {
                "order_id": "OLD", "status": "shipped",
                "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200}],
                "total_cents": 200,
                "shipment": {"carrier": "DHL", "tracking_no": "Z"},
            }},
            "returns": {"OLD": [
                {"order_id": "OLD", "return_id": "L1", "lines": [{"sku": "T", "quantity": 2}]},
            ]},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        result = app.cancel_return("L1")
        self.assertEqual(result["lines"], [{"sku": "T", "quantity": 2}])
        history = app.history("OLD")
        self.assertFalse(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                         [(1, "cancel-return")])
        self.assertEqual(history["events"][0]["result"], result)
        self.assertEqual(app.stock("T")["on_hand"], 3)

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
        before = app.path.read_bytes()
        with self.assertRaises(ValueError):
            app.cancel_return("RP")
        with self.assertRaises(ValueError):
            app.cancel_return("RG")
        self.assertEqual(app.path.read_bytes(), before)
        # Still treated as active registrations: quantity still counted.
        self.assertEqual(app.get_returns("OLD")["remaining"], [{"sku": "T", "quantity": 1}])
        self.assertEqual(app.stock("T")["on_hand"], 3)

    def test_legacy_data_without_cancel_info_treats_returns_as_active(self):
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "orders": {"OLD": {
                "order_id": "OLD", "status": "shipped",
                "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200}],
                "total_cents": 200,
                "shipment": {"carrier": "DHL", "tracking_no": "Z"},
            }},
            "returns": {"OLD": [
                {"order_id": "OLD", "return_id": "L1", "lines": [{"sku": "T", "quantity": 2}]},
            ]},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        view = app.get_returns("OLD")
        self.assertEqual([r["return_id"] for r in view["records"]], ["L1"])
        self.assertEqual(view["remaining"], [{"sku": "T", "quantity": 0}])

    def test_query_and_failed_cancel_create_no_file(self):
        empty_root = Path(self.temp.name) / "empty"
        fresh = OrderDesk(empty_root)
        with self.assertRaises(ValueError):
            fresh.cancel_return("missing")
        with self.assertRaises(ValueError):
            fresh.cancel_return("   ")
        self.assertFalse(empty_root.exists())

    def test_persistence_after_reopen(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        result = self.app.cancel_return("R1")
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.get_returns("O1"), {
            "order_id": "O1",
            "records": [],
            "remaining": [{"sku": "T", "quantity": 5}],
        })
        with self.assertRaises(ValueError):
            reopened.cancel_return("R1")
        with self.assertRaises(ValueError):
            reopened.receive_return("R1")
        self._shipped_via(reopened, "O2", [{"sku": "T", "quantity": 2}])
        with self.assertRaises(ValueError):
            reopened.record_return("O2", "R1", [{"sku": "T", "quantity": 1}])
        events = reopened.history("O1")["events"]
        self.assertEqual(events[-1]["action"], "cancel-return")
        self.assertEqual(events[-1]["result"], result)

    def _shipped_via(self, app, order_id, lines):
        app.place(order_id, lines)
        app.ship(order_id, "DHL", "TRK-" + order_id)

    def test_cli_cancel_and_array_partial_failure(self):
        self._shipped("O1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([
            {"return_id": "R1"},
            {"return_id": "R1"},
        ]), encoding="utf-8")
        failed = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "cancel-return", str(batch)],
            text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2, failed.stdout)
        self.assertIn("already cancelled", json.loads(failed.stderr)["error"])
        # Remaining was recomputed from active registrations only.
        view = self.app.get_returns("O1")
        self.assertEqual([r["return_id"] for r in view["records"]], ["R2"])
        self.assertEqual(view["remaining"], [{"sku": "T", "quantity": 2}])
        # R2 is still active and can now be received; R1 cannot.
        query = self.root / "q.json"
        query.write_text(json.dumps({"return_id": "R2"}), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "receive-return", str(query)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        query.write_text(json.dumps({"return_id": "R1"}), encoding="utf-8")
        rejected = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "receive-return", str(query)],
            text=True, capture_output=True)
        self.assertEqual(rejected.returncode, 2, rejected.stdout)
        self.assertIn("cancelled", json.loads(rejected.stderr)["error"])


if __name__ == "__main__":
    unittest.main()
