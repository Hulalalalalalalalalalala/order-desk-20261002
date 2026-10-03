import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk


class AmendReturnTests(unittest.TestCase):
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

    def test_amend_replaces_lines_and_returns_before_after(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 3}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        result = self.app.amend_return("R1", [{"sku": "T", "quantity": 2}], [
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 1},
            {"sku": "T", "quantity": 2, "ignored": True},
        ])
        self.assertEqual(result, {
            "order_id": "O1",
            "return_id": "R1",
            "before": [{"sku": "T", "quantity": 2}],
            "after": [{"sku": "C", "quantity": 1}, {"sku": "T", "quantity": 3}],
        })
        self.assertEqual(set(result), {"order_id", "return_id", "before", "after"})
        self.assertTrue(all(set(line) == {"sku", "quantity"}
                            for line in result["before"] + result["after"]))
        # The stored record keeps its id and order but carries the new lines.
        records = self.app.get_returns("O1")["records"]
        self.assertEqual(records, [{
            "order_id": "O1",
            "return_id": "R1",
            "lines": [{"sku": "C", "quantity": 1}, {"sku": "T", "quantity": 3}],
        }])

    def test_expected_checked_against_merged_current_regardless_of_row_order(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 3}])
        self.app.record_return("O1", "R1", [
            {"sku": "T", "quantity": 1},
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 1},
        ])
        result = self.app.amend_return("  R1 ", [
            {"sku": "T", "quantity": 2},
            {"sku": "C", "quantity": 1},
        ], [{"sku": "C", "quantity": 2}])
        self.assertEqual(result["before"], [
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 2},
        ])
        self.assertEqual(result["after"], [{"sku": "C", "quantity": 2}])

    def test_expected_mismatch_rejected_even_when_target_equals_current(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.amend_return("R1", [{"sku": "T", "quantity": 1}],
                                  [{"sku": "T", "quantity": 2}])
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_no_change_succeeds_without_write_or_event(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        before = self.app.path.read_bytes()
        result = self.app.amend_return("R1", [{"sku": "T", "quantity": 2}],
                                       [{"sku": "T", "quantity": 1}, {"sku": "T", "quantity": 1}])
        self.assertEqual(result, {
            "order_id": "O1",
            "return_id": "R1",
            "before": [{"sku": "T", "quantity": 2}],
            "after": [{"sku": "T", "quantity": 2}],
        })
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual([e["action"] for e in self.app.history("O1")["events"]],
                         ["place", "ship", "record-return"])

    def test_quota_counts_other_active_and_received_but_not_self_or_cancelled(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        # Ordered 5, other active registration holds 1: R1 may grow to 4.
        result = self.app.amend_return("R1", [{"sku": "T", "quantity": 3}],
                                       [{"sku": "T", "quantity": 4}])
        self.assertEqual(result["after"], [{"sku": "T", "quantity": 4}])
        with self.assertRaises(ValueError):
            self.app.amend_return("R1", [{"sku": "T", "quantity": 4}],
                                  [{"sku": "T", "quantity": 5}])
        # Shrink back, register and receive R3: a received registration still
        # occupies allowance, so R1 may only reach 3 again.
        self.app.amend_return("R1", [{"sku": "T", "quantity": 4}], [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R3", [{"sku": "T", "quantity": 1}])
        self.app.receive_return("R3")
        with self.assertRaises(ValueError):
            self.app.amend_return("R1", [{"sku": "T", "quantity": 3}],
                                  [{"sku": "T", "quantity": 4}])
        # Cancelling the other pending registration frees its allowance again.
        self.app.cancel_return("R2")
        result = self.app.amend_return("R1", [{"sku": "T", "quantity": 3}],
                                       [{"sku": "T", "quantity": 4}])
        self.assertEqual(result["after"], [{"sku": "T", "quantity": 4}])

    def test_new_sku_must_belong_to_original_order(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.amend_return("R1", [{"sku": "T", "quantity": 1}],
                                  [{"sku": "C", "quantity": 1}])
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_paused_unmanaged_and_uncatalogued_products_do_not_block(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}, {"sku": "U", "quantity": 2}],
                      stock={"T": 10})
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.set_product_enabled("T", False)
        # Paused and unmanaged original-order products may be corrected freely.
        result = self.app.amend_return("R1", [{"sku": "T", "quantity": 1}],
                                       [{"sku": "T", "quantity": 2}, {"sku": "U", "quantity": 2}])
        self.assertEqual(result["after"], [
            {"sku": "T", "quantity": 2},
            {"sku": "U", "quantity": 2},
        ])
        # A product dropped from the catalog after ordering can still be amended.
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        del data["products"]["U"]
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        result = self.app.amend_return("R1", result["after"], [{"sku": "U", "quantity": 1}])
        self.assertEqual(result["after"], [{"sku": "U", "quantity": 1}])

    def test_unknown_cancelled_and_received_returns_rejected(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R3", [{"sku": "T", "quantity": 1}])
        self.app.cancel_return("R2")
        self.app.receive_return("R3")
        before = self.app.path.read_bytes()
        for return_id in ("R9", "R2", "R3"):
            with self.assertRaises(ValueError):
                self.app.amend_return(return_id, [{"sku": "T", "quantity": 1}],
                                      [{"sku": "T", "quantity": 2}])
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_order_missing_or_not_shipped_rejected(self):
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
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
        for return_id in ("RP", "RG"):
            with self.assertRaises(ValueError):
                app.amend_return(return_id, [{"sku": "T", "quantity": 1}],
                                 [{"sku": "T", "quantity": 2}])
        self.assertEqual(app.path.read_bytes(), before)

    def test_delivered_order_can_be_amended(self):
        self._shipped("O1", [{"sku": "T", "quantity": 3}])
        self.app.confirm_delivery("O1", "Sam", "2026-10-01")
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        result = self.app.amend_return("R1", [{"sku": "T", "quantity": 1}],
                                       [{"sku": "T", "quantity": 3}])
        self.assertEqual(result["after"], [{"sku": "T", "quantity": 3}])

    def test_invalid_identifiers_and_line_shapes(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        good = [{"sku": "T", "quantity": 2}]
        for bad_id in (None, 123, 1.5, b"R1", ["R1"], {"x": 1}, "   ", "\t\n", True, "r1"):
            with self.assertRaises(ValueError):
                self.app.amend_return(bad_id, good, good)
        for bad_lines in (None, "x", 5, {}, [], [{"sku": "T"}], [{"quantity": 1}],
                          [["sku"]], ["x"], [{"sku": " ", "quantity": 1}],
                          [{"sku": "T", "quantity": 0}], [{"sku": "T", "quantity": -1}],
                          [{"sku": "T", "quantity": 1.5}], [{"sku": "T", "quantity": "1"}],
                          [{"sku": "T", "quantity": True}]):
            with self.assertRaises(ValueError):
                self.app.amend_return("R1", bad_lines, good)
            with self.assertRaises(ValueError):
                self.app.amend_return("R1", good, bad_lines)
        # Case-sensitive sku matching against the stored record.
        with self.assertRaises(ValueError):
            self.app.amend_return("R1", [{"sku": "t", "quantity": 2}], good)

    def test_failures_create_no_file_and_consume_no_sequence(self):
        empty_root = Path(self.temp.name) / "empty"
        fresh = OrderDesk(empty_root)
        with self.assertRaises(ValueError):
            fresh.amend_return("R1", [{"sku": "T", "quantity": 1}], [{"sku": "T", "quantity": 1}])
        self.assertFalse(empty_root.exists())
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        with self.assertRaises(ValueError):
            self.app.amend_return("R1", [{"sku": "T", "quantity": 9}], [{"sku": "T", "quantity": 1}])
        self.app.amend_return("R1", [{"sku": "T", "quantity": 2}], [{"sku": "T", "quantity": 3}])
        events = self.app.history("O1")["events"]
        self.assertEqual([(e["sequence"], e["action"]) for e in events],
                         [(1, "place"), (2, "ship"), (3, "record-return"), (4, "amend-return")])

    def test_history_event_snapshot_and_legacy_order(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        result = self.app.amend_return("R1", [{"sku": "T", "quantity": 2}],
                                       [{"sku": "T", "quantity": 3}])
        history = self.app.history("O1")
        self.assertTrue(history["complete"])
        event = history["events"][-1]
        self.assertEqual(set(event), {"sequence", "action", "result"})
        self.assertEqual(event["action"], "amend-return")
        self.assertEqual(event["result"], result)
        # Old snapshots are untouched.
        self.assertEqual(history["events"][2]["result"]["lines"], [{"sku": "T", "quantity": 2}])
        # A legacy order without history starts at 1 with complete=False.
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "orders": {"OLD": {
                "order_id": "OLD", "status": "shipped",
                "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200}],
                "total_cents": 200,
                "shipment": {"carrier": "DHL", "tracking_no": "Z"},
            }},
            "returns": {"OLD": [
                {"order_id": "OLD", "return_id": "L1", "lines": [{"sku": "T", "quantity": 1}]},
            ]},
        }
        legacy_root = Path(self.temp.name) / "legacy"
        legacy_root.mkdir()
        OrderDesk(legacy_root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(legacy_root)
        result = app.amend_return("L1", [{"sku": "T", "quantity": 1}],
                                  [{"sku": "T", "quantity": 2}])
        history = app.history("OLD")
        self.assertFalse(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                         [(1, "amend-return")])
        self.assertEqual(history["events"][0]["result"], result)

    def test_stock_reservations_order_and_shipment_untouched(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.place("O2", [{"sku": "T", "quantity": 2}])
        order_before = self.app.get("O1")
        stock_t = self.app.stock("T")
        self.app.amend_return("R1", [{"sku": "T", "quantity": 1}],
                              [{"sku": "C", "quantity": 2}])
        self.assertEqual(self.app.get("O1"), order_before)
        self.assertEqual(self.app.get("O1")["shipment"], {"carrier": "DHL", "tracking_no": "TRK-O1"})
        self.assertEqual(self.app.stock("T"), stock_t)
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["reservations"]["O2"], {"T": 2})

    def test_persistence_and_follow_up_operations_use_new_lines(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.app.amend_return("R1", [{"sku": "T", "quantity": 3}],
                              [{"sku": "C", "quantity": 2}, {"sku": "T", "quantity": 1}])
        reopened = OrderDesk(self.root)
        # Remaining returnable quantity reflects the new registration.
        self.assertEqual(reopened.get_returns("O1")["remaining"], [
            {"sku": "C", "quantity": 0},
            {"sku": "T", "quantity": 4},
        ])
        # The worklist shows the new lines.
        entries = reopened.return_worklist()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["lines"], [
            {"sku": "C", "quantity": 2},
            {"sku": "T", "quantity": 1},
        ])
        # Receiving uses the new quantities.
        receipt = reopened.receive_return("R1")
        self.assertEqual([(line["sku"], line["quantity"]) for line in receipt["lines"]],
                         [("C", 2), ("T", 1)])
        self.assertEqual(reopened.stock("C")["on_hand"], 10)

    def test_cancel_after_amend_uses_new_lines(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.app.amend_return("R1", [{"sku": "T", "quantity": 3}], [{"sku": "T", "quantity": 1}])
        result = self.app.cancel_return("R1")
        self.assertEqual(result["lines"], [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.get_returns("O1")["remaining"], [{"sku": "T", "quantity": 5}])

    def test_cli_amend_return_and_array_partial_failure(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([
            {"return_id": "R1",
             "expected_lines": [{"sku": "T", "quantity": 1}],
             "lines": [{"sku": "T", "quantity": 2}]},
            {"return_id": "R2",
             "expected_lines": [{"sku": "T", "quantity": 9}],
             "lines": [{"sku": "T", "quantity": 2}]},
        ]), encoding="utf-8")
        # Each array entry runs independently: the first succeeds, the second
        # fails and the command reports the failure.
        run = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "amend-return", str(batch)],
            text=True, capture_output=True)
        self.assertEqual(run.returncode, 2, run.stdout)
        self.assertIn("expected lines", json.loads(run.stderr)["error"])
        view = self.app.get_returns("O1")
        self.assertEqual(view["records"], [
            {"order_id": "O1", "return_id": "R1", "lines": [{"sku": "T", "quantity": 2}]},
            {"order_id": "O1", "return_id": "R2", "lines": [{"sku": "T", "quantity": 1}]},
        ])
        # A fully successful batch prints the results in order.
        batch.write_text(json.dumps([
            {"return_id": "R2",
             "expected_lines": [{"sku": "T", "quantity": 1}],
             "lines": [{"sku": "T", "quantity": 2}]},
        ]), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "amend-return", str(batch)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        payload = json.loads(ok.stdout)
        self.assertEqual(payload[0]["after"], [{"sku": "T", "quantity": 2}])


if __name__ == "__main__":
    unittest.main()
