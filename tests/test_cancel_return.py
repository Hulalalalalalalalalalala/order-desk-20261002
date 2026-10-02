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

    def test_cancel_returns_snapshot_of_original_registration(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 2}])
        record = self.app.record_return("O1", "R1", [
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 1},
            {"sku": "T", "quantity": 2},
        ])
        result = self.app.cancel_return("R1")
        self.assertEqual(set(result), {"order_id", "return_id", "lines"})
        self.assertEqual(result["order_id"], "O1")
        self.assertEqual(result["return_id"], "R1")
        # Duplicate skus merged and sorted ascending; each line is sku+quantity only.
        self.assertEqual(result["lines"], [{"sku": "C", "quantity": 1}, {"sku": "T", "quantity": 3}])
        for line in result["lines"]:
            self.assertEqual(set(line), {"sku", "quantity"})
        # The snapshot equals the sku/quantity projection of the stored record.
        self.assertEqual(result["lines"], record["lines"])

    def test_remaining_restored_and_record_disappears(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.assertEqual(self.app.get_returns("O1")["remaining"], [{"sku": "T", "quantity": 2}])
        self.app.cancel_return("R1")
        view = self.app.get_returns("O1")
        self.assertEqual(view["records"], [])
        self.assertEqual(view["remaining"], [{"sku": "T", "quantity": 5}])

    def test_other_returns_are_unaffected(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 2}])
        r1 = self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        r2 = self.app.record_return("O1", "R2", [{"sku": "C", "quantity": 1}])
        self.app.cancel_return("R1")
        view = self.app.get_returns("O1")
        self.assertEqual([r["return_id"] for r in view["records"]], ["R2"])
        self.assertEqual(view["records"][0], r2)
        self.assertEqual(view["remaining"], [{"sku": "C", "quantity": 1}, {"sku": "T", "quantity": 5}])
        # Cancelling one never frees room counted by another active registration.
        with self.assertRaises(ValueError):
            self.app.record_return("O1", "R3", [{"sku": "C", "quantity": 2}])
        self.assertEqual(self.app.get_returns("O1")["records"], [r2])

    def test_zero_quantity_rows_kept_in_remaining(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])
        self.app.cancel_return("R1")
        self.assertEqual(
            self.app.get_returns("O1")["remaining"],
            [{"sku": "C", "quantity": 1}, {"sku": "T", "quantity": 2}],
        )

    def test_released_quantity_can_be_registered_under_new_id(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 5}])
        with self.assertRaises(ValueError):
            self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        self.app.cancel_return("R1")
        r2 = self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 4}])
        self.assertEqual(r2["lines"], [{"sku": "T", "quantity": 4}])
        self.assertEqual(self.app.get_returns("O1")["remaining"], [{"sku": "T", "quantity": 1}])

    def test_received_return_cannot_be_cancelled(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        self.app.receive_return("R1")
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.cancel_return("R1")
        self.assertEqual(self.app.path.read_bytes(), before)
        # Stock added by the receipt survives.
        self.assertEqual(self.app.stock("T")["on_hand"], 10)

    def test_cancelled_return_cannot_be_received_or_cancelled_again(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.cancel_return("R1")
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.receive_return("R1")
        with self.assertRaises(ValueError):
            self.app.cancel_return("R1")
        with self.assertRaises(ValueError):
            self.app.cancel_return("  R1  ")
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_cancelled_id_remains_occupied_even_on_another_order(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.cancel_return("R1")
        self._shipped("O2", [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.record_return("O2", "R1", [{"sku": "T", "quantity": 1}])
        # A different id on the second order works normally.
        self.app.record_return("O2", "R9", [{"sku": "T", "quantity": 1}])

    def test_invalid_and_unknown_identifiers(self):
        self._shipped("O1", [{"sku": "T", "quantity": 1}])
        for bad in (None, 123, 1.5, b"R1", ["R1"], {"x": 1}, "   ", "\t\n", True):
            with self.assertRaises(ValueError):
                self.app.cancel_return(bad)
        with self.assertRaises(ValueError):
            self.app.cancel_return("nope")

    def test_whitespace_trimmed_and_case_sensitive(self):
        self._shipped("O1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "Ra", [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.cancel_return("  Ra  ")["return_id"], "Ra")
        with self.assertRaises(ValueError):
            self.app.cancel_return("ra")

    def test_cancel_does_not_touch_stock_reservations_or_order(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        # An open order holds a reservation while the return is cancelled.
        self.app.place("O2", [{"sku": "T", "quantity": 3}])
        order_before = self.app.get("O1")
        stock_before = self.app.stock("T")
        self.app.cancel_return("R1")
        self.assertEqual(self.app.stock("T"), stock_before)
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["reservations"]["O2"], {"T": 3})
        self.assertEqual(self.app.get("O1"), order_before)
        self.assertEqual(self.app.get("O1")["status"], "shipped")
        self.assertEqual(self.app.get("O1")["shipment"], {"carrier": "DHL", "tracking_no": "TRK-O1"})

    def test_failures_add_no_history_and_consume_no_sequence(self):
        self._shipped("O1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.cancel_return("R1")
        with self.assertRaises(ValueError):
            self.app.cancel_return("R1")
        with self.assertRaises(ValueError):
            self.app.cancel_return("nope")
        with self.assertRaises(ValueError):
            self.app.receive_return("R1")
        events = self.app.history("O1")["events"]
        self.assertEqual([(e["sequence"], e["action"]) for e in events],
                         [(1, "place"), (2, "ship"), (3, "record-return"), (4, "cancel-return")])

    def test_history_records_cancel_event_with_snapshot(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        snapshot = self.app.cancel_return("R1")
        history = self.app.history("O1")
        self.assertTrue(history["complete"])
        events = history["events"]
        self.assertEqual([(e["sequence"], e["action"]) for e in events],
                         [(1, "place"), (2, "ship"), (3, "record-return"), (4, "cancel-return")])
        self.assertEqual(set(events[3]), {"sequence", "action", "result"})
        self.assertEqual(events[3]["result"], snapshot)
        # Old snapshots and the complete flag are untouched.
        self.assertEqual(events[2]["action"], "record-return")

    def test_legacy_order_without_history_starts_at_one(self):
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
        snapshot = app.cancel_return("L1")
        history = app.history("OLD")
        self.assertFalse(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                         [(1, "cancel-return")])
        self.assertEqual(history["events"][0]["result"], snapshot)
        # Legacy data with no cancel info treats the original registration as
        # active until cancelled; queries alone never write a file.
        self.assertEqual(app.get_returns("OLD")["records"], [])

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

    def test_persistence_across_reopen(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.app.cancel_return("R1")
        reopened = OrderDesk(self.root)
        view = reopened.get_returns("O1")
        self.assertEqual(view["records"], [])
        self.assertEqual(view["remaining"], [{"sku": "T", "quantity": 5}])
        # The old id stays unusable after reopen.
        with self.assertRaises(ValueError):
            reopened.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            reopened.receive_return("R1")
        with self.assertRaises(ValueError):
            reopened.cancel_return("R1")
        # But the released quantity is available under a new id.
        reopened.record_return("O1", "R2", [{"sku": "T", "quantity": 5}])
        events = reopened.history("O1")["events"]
        self.assertEqual([e["action"] for e in events],
                         ["place", "ship", "record-return", "cancel-return", "record-return"])

    def test_query_creates_no_file(self):
        empty_root = Path(self.temp.name) / "empty"
        fresh = OrderDesk(empty_root)
        with self.assertRaises(ValueError):
            fresh.cancel_return("missing")
        with self.assertRaises(ValueError):
            fresh.get_returns("anything")
        self.assertFalse(empty_root.exists())

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
        # First row succeeded; R2 is untouched and still active.
        query = self.root / "q.json"
        query.write_text(json.dumps({"order_id": "O1"}), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "returns", str(query)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        view = json.loads(ok.stdout)
        self.assertEqual([r["return_id"] for r in view["records"]], ["R2"])
        self.assertEqual(view["remaining"], [{"sku": "T", "quantity": 2}])


if __name__ == "__main__":
    unittest.main()
