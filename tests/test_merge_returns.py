import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk


class MergeReturnsTests(unittest.TestCase):
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

    def test_merge_combines_quantities_and_returns_three_snapshots(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 3}])
        self.app.record_return("O1", "R1", [
            {"sku": "T", "quantity": 2},
            {"sku": "C", "quantity": 1},
        ])
        self.app.record_return("O1", "R2", [
            {"sku": "C", "quantity": 2},
            {"sku": "T", "quantity": 1},
        ])
        result = self.app.merge_returns("R1", "R2", [
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 2},
        ], [
            {"sku": "T", "quantity": 1},
            {"sku": "C", "quantity": 2},
        ])
        self.assertEqual(set(result), {"source", "target_before", "target_after"})
        self.assertEqual(result, {
            "source": {"order_id": "O1", "return_id": "R1", "lines": [
                {"sku": "C", "quantity": 1},
                {"sku": "T", "quantity": 2},
            ]},
            "target_before": {"order_id": "O1", "return_id": "R2", "lines": [
                {"sku": "C", "quantity": 2},
                {"sku": "T", "quantity": 1},
            ]},
            "target_after": {"order_id": "O1", "return_id": "R2", "lines": [
                {"sku": "C", "quantity": 3},
                {"sku": "T", "quantity": 3},
            ]},
        })
        for snapshot in result.values():
            self.assertEqual(set(snapshot), {"order_id", "return_id", "lines"})
            self.assertTrue(all(set(line) == {"sku", "quantity"} for line in snapshot["lines"]))
        # The source leaves the active records; the target carries the sums.
        view = self.app.get_returns("O1")
        self.assertEqual(view["records"], [
            {"order_id": "O1", "return_id": "R2", "lines": [
                {"sku": "C", "quantity": 3},
                {"sku": "T", "quantity": 3},
            ]},
        ])
        # The cumulative returned quantity and the returnable allowance do not change.
        self.assertEqual(view["remaining"], [
            {"sku": "C", "quantity": 0},
            {"sku": "T", "quantity": 2},
        ])

    def test_identifiers_trimmed_and_case_sensitive(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        result = self.app.merge_returns("  R1 ", "  R2 ", [{"sku": "T", "quantity": 2}],
                                        [{"sku": "T", "quantity": 1}])
        self.assertEqual(result["source"]["return_id"], "R1")
        self.assertEqual(result["target_after"]["return_id"], "R2")
        # Case-sensitive sku comparison against the stored records.
        self.app.record_return("O1", "R3", [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.merge_returns("R3", "R2", [{"sku": "t", "quantity": 1}],
                                   [{"sku": "T", "quantity": 3}])

    def test_expected_lists_merged_and_compared_without_row_order(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 3}])
        self.app.record_return("O1", "R1", [
            {"sku": "T", "quantity": 1},
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 1},
        ])
        self.app.record_return("O1", "R2", [{"sku": "C", "quantity": 2}])
        result = self.app.merge_returns("R1", "R2", [
            {"sku": "T", "quantity": 1, "ignored": True},
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 1},
        ], [
            {"sku": "C", "quantity": 1},
            {"sku": "C", "quantity": 1},
        ])
        self.assertEqual(result["source"]["lines"], [
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 2},
        ])
        self.assertEqual(result["target_after"]["lines"], [
            {"sku": "C", "quantity": 3},
            {"sku": "T", "quantity": 2},
        ])

    def test_expected_mismatch_on_either_side_rejected(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.merge_returns("R1", "R2", [{"sku": "T", "quantity": 1}],
                                   [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.merge_returns("R1", "R2", [{"sku": "T", "quantity": 2}],
                                   [{"sku": "T", "quantity": 2}])
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_unknown_cancelled_and_received_registrations_rejected(self):
        self._shipped("O1", [{"sku": "T", "quantity": 6}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R3", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R4", [{"sku": "T", "quantity": 1}])
        self.app.cancel_return("R3")
        self.app.receive_return("R4")
        one = [{"sku": "T", "quantity": 1}]
        snapshot = self.app.path.read_bytes()
        for source_id, target_id in (
            ("R9", "R1"), ("R1", "R9"), ("R3", "R1"), ("R1", "R3"),
            ("R4", "R1"), ("R1", "R4"),
        ):
            with self.assertRaises(ValueError):
                self.app.merge_returns(source_id, target_id, one, one)
        self.assertEqual(self.app.path.read_bytes(), snapshot)

    def test_same_id_and_different_orders_rejected(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self._shipped("O2", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O2", "R2", [{"sku": "T", "quantity": 1}])
        one = [{"sku": "T", "quantity": 1}]
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.merge_returns("R1", "R1", one, one)
        with self.assertRaises(ValueError):
            self.app.merge_returns("R1", "R2", one, one)
        with self.assertRaises(ValueError):
            self.app.merge_returns("R2", "R1", one, one)
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
                "OLD": [
                    {"order_id": "OLD", "return_id": "RA", "lines": [{"sku": "T", "quantity": 1}]},
                    {"order_id": "OLD", "return_id": "RB", "lines": [{"sku": "T", "quantity": 1}]},
                ],
                "GONE": [
                    {"order_id": "GONE", "return_id": "RC", "lines": [{"sku": "T", "quantity": 1}]},
                    {"order_id": "GONE", "return_id": "RD", "lines": [{"sku": "T", "quantity": 1}]},
                ],
            },
        }
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        before = app.path.read_bytes()
        one = [{"sku": "T", "quantity": 1}]
        for source_id, target_id in (("RA", "RB"), ("RC", "RD")):
            with self.assertRaises(ValueError):
                app.merge_returns(source_id, target_id, one, one)
        self.assertEqual(app.path.read_bytes(), before)

    def test_delivered_order_can_merge(self):
        self._shipped("O1", [{"sku": "T", "quantity": 3}])
        self.app.confirm_delivery("O1", "Sam", "2026-10-01")
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        one = [{"sku": "T", "quantity": 1}]
        result = self.app.merge_returns("R1", "R2", one, one)
        self.assertEqual(result["target_after"]["lines"], [{"sku": "T", "quantity": 2}])

    def test_paused_unmanaged_and_uncatalogued_products_do_not_block(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}, {"sku": "U", "quantity": 3}],
                      stock={"T": 10})
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}, {"sku": "U", "quantity": 1}])
        self.app.record_return("O1", "R2", [{"sku": "U", "quantity": 1}])
        self.app.set_product_enabled("T", False)
        result = self.app.merge_returns("R1", "R2",
                                        [{"sku": "T", "quantity": 1}, {"sku": "U", "quantity": 1}],
                                        [{"sku": "U", "quantity": 1}])
        self.assertEqual(result["target_after"]["lines"], [
            {"sku": "T", "quantity": 1},
            {"sku": "U", "quantity": 2},
        ])
        # A product dropped from the catalog after ordering can still be merged.
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        del data["products"]["U"]
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        app.record_return("O1", "R3", [{"sku": "U", "quantity": 1}])
        result = app.merge_returns("R3", "R2", [{"sku": "U", "quantity": 1}],
                                   [{"sku": "T", "quantity": 1}, {"sku": "U", "quantity": 2}])
        self.assertEqual(result["target_after"]["lines"], [
            {"sku": "T", "quantity": 1},
            {"sku": "U", "quantity": 3},
        ])

    def test_invalid_identifiers_and_line_shapes(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        two = [{"sku": "T", "quantity": 2}]
        one = [{"sku": "T", "quantity": 1}]
        for bad_id in (None, 123, 1.5, b"R1", ["R1"], {"x": 1}, "   ", "\t\n", True):
            with self.assertRaises(ValueError):
                self.app.merge_returns(bad_id, "R2", two, one)
            with self.assertRaises(ValueError):
                self.app.merge_returns("R1", bad_id, two, one)
        for bad_lines in (None, "x", 5, {}, [], [{"sku": "T"}], [{"quantity": 1}],
                          [["sku"]], ["x"], [{"sku": " ", "quantity": 1}],
                          [{"sku": "T", "quantity": 0}], [{"sku": "T", "quantity": -1}],
                          [{"sku": "T", "quantity": 1.5}], [{"sku": "T", "quantity": "1"}],
                          [{"sku": "T", "quantity": True}]):
            with self.assertRaises(ValueError):
                self.app.merge_returns("R1", "R2", bad_lines, one)
            with self.assertRaises(ValueError):
                self.app.merge_returns("R1", "R2", two, bad_lines)

    def test_failures_create_no_directory_and_consume_no_sequence(self):
        empty_root = Path(self.temp.name) / "empty"
        fresh = OrderDesk(empty_root)
        one = [{"sku": "T", "quantity": 1}]
        with self.assertRaises(ValueError):
            fresh.merge_returns("R1", "R2", one, one)
        self.assertFalse(empty_root.exists())
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.merge_returns("R1", "R2", [{"sku": "T", "quantity": 9}], one)
        self.app.merge_returns("R1", "R2", [{"sku": "T", "quantity": 2}], one)
        events = self.app.history("O1")["events"]
        self.assertEqual([(e["sequence"], e["action"]) for e in events],
                         [(1, "place"), (2, "ship"), (3, "record-return"),
                          (4, "record-return"), (5, "merge-returns")])

    def test_history_event_snapshot_and_legacy_order(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        result = self.app.merge_returns("R1", "R2", [{"sku": "T", "quantity": 2}],
                                        [{"sku": "T", "quantity": 1}])
        history = self.app.history("O1")
        self.assertTrue(history["complete"])
        event = history["events"][-1]
        self.assertEqual(set(event), {"sequence", "action", "result"})
        self.assertEqual(event["action"], "merge-returns")
        self.assertEqual(event["result"], result)
        # Exactly one event is appended and older snapshots stay untouched.
        self.assertEqual([e["action"] for e in history["events"]],
                         ["place", "ship", "record-return", "record-return", "merge-returns"])
        self.assertEqual(history["events"][2]["result"]["lines"], [{"sku": "T", "quantity": 2}])
        # A legacy order without history starts at 1 with complete=False, and
        # legacy registrations without receipts read as pending.
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "orders": {"OLD": {
                "order_id": "OLD", "status": "shipped",
                "lines": [{"sku": "T", "quantity": 3, "unit_price_cents": 100, "subtotal_cents": 300}],
                "total_cents": 300,
                "shipment": {"carrier": "DHL", "tracking_no": "Z"},
            }},
            "returns": {"OLD": [
                {"order_id": "OLD", "return_id": "L1", "lines": [{"sku": "T", "quantity": 2}]},
                {"order_id": "OLD", "return_id": "L2", "lines": [{"sku": "T", "quantity": 1}]},
            ]},
        }
        legacy_root = Path(self.temp.name) / "legacy"
        legacy_root.mkdir()
        OrderDesk(legacy_root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(legacy_root)
        result = app.merge_returns("L1", "L2", [{"sku": "T", "quantity": 2}],
                                   [{"sku": "T", "quantity": 1}])
        history = app.history("OLD")
        self.assertFalse(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                         [(1, "merge-returns")])
        self.assertEqual(history["events"][0]["result"], result)
        self.assertEqual(result["target_after"]["lines"], [{"sku": "T", "quantity": 3}])

    def test_stock_reservations_order_shipment_and_receipts_untouched(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R2", [{"sku": "C", "quantity": 1}])
        self.app.place("O2", [{"sku": "T", "quantity": 2}])
        order_before = self.app.get("O1")
        stocks = {sku: self.app.stock(sku) for sku in ("T", "C")}
        stock_events = {sku: len(self.app.stock_history(sku)["events"]) for sku in ("T", "C")}
        self.app.merge_returns("R1", "R2", [{"sku": "T", "quantity": 1}],
                               [{"sku": "C", "quantity": 1}])
        self.assertEqual(self.app.get("O1"), order_before)
        self.assertEqual(self.app.get("O1")["shipment"], {"carrier": "DHL", "tracking_no": "TRK-O1"})
        for sku in ("T", "C"):
            self.assertEqual(self.app.stock(sku), stocks[sku])
            self.assertEqual(len(self.app.stock_history(sku)["events"]), stock_events[sku])
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["reservations"]["O2"], {"T": 2})
        self.assertNotIn("R1", raw.get("return_receipts", {}))
        self.assertNotIn("R2", raw.get("return_receipts", {}))
        with self.assertRaises(ValueError):
            self.app.get_return_receipt("R1")

    def test_persistence_worklist_and_receiving_the_merged_target(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}, {"sku": "C", "quantity": 1}])
        self.app.merge_returns("R1", "R2", [{"sku": "T", "quantity": 2}],
                               [{"sku": "T", "quantity": 1}, {"sku": "C", "quantity": 1}])
        reopened = OrderDesk(self.root)
        entries = reopened.return_worklist("all")
        self.assertEqual([(e["return_id"], e["stage"]) for e in entries],
                         [("R1", "cancelled"), ("R2", "pending")])
        # The cancelled source keeps its original lines; the target shows the merged list.
        self.assertEqual(entries[0]["lines"], [{"sku": "T", "quantity": 2}])
        self.assertEqual(entries[1]["lines"], [
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 3},
        ])
        # Receiving the target takes the merged quantities.
        receipt = reopened.receive_return("R2")
        self.assertEqual([(line["sku"], line["quantity"]) for line in receipt["lines"]], [("C", 1), ("T", 3)])
        self.assertEqual(reopened.stock("T")["on_hand"], 8)
        self.assertEqual(reopened.stock("C")["on_hand"], 9)
        self.assertEqual([e["return_id"] for e in reopened.return_worklist("pending")], [])
        self.assertEqual([e["return_id"] for e in reopened.return_worklist("received")], ["R2"])
        self.assertEqual([e["return_id"] for e in reopened.return_worklist("cancelled")], ["R1"])

    def test_worklist_blockers_follow_the_merged_lines(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}, {"sku": "U", "quantity": 2}],
                      stock={"T": 10})
        self.app.record_return("O1", "R1", [{"sku": "U", "quantity": 1}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        self.app.merge_returns("R1", "R2", [{"sku": "U", "quantity": 1}],
                               [{"sku": "T", "quantity": 1}])
        entries = self.app.return_worklist("pending")
        self.assertEqual([e["return_id"] for e in entries], ["R2"])
        # The merged target is blocked by the unmanaged sku from the source.
        self.assertFalse(entries[0]["can_receive"])
        self.assertEqual(entries[0]["blockers"], [{"sku": "U", "reason": "unmanaged"}])

    def test_quantity_and_amount_queries_unchanged_by_merge(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        progress_before = self.app.order_progress("O1")["lines"]
        summary_before = self.app.fulfillment_summary()
        self.app.merge_returns("R1", "R2", [{"sku": "T", "quantity": 2}],
                               [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.order_progress("O1")["lines"], progress_before)
        self.assertEqual(self.app.fulfillment_summary(), summary_before)
        self.assertEqual(self.app.order_progress("O1")["lines"], [{
            "sku": "T", "ordered": 5, "reserved": 0, "needed": 0,
            "shipped": 5, "pending": 3, "received": 0, "remaining": 2, "net": 5,
        }])
        # The order worklist keeps one receive-return task for the order.
        worklist = self.app.order_worklist("receive-return")
        self.assertEqual([item["order_id"] for item in worklist], ["O1"])

    def test_follow_up_operations_use_the_merged_target(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 3}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        self.app.merge_returns("R1", "R2", [{"sku": "T", "quantity": 2}],
                               [{"sku": "T", "quantity": 1}])
        # Amend checks the merged content and the merged quantities cap splits.
        self.app.amend_return("R2", [{"sku": "T", "quantity": 3}],
                              [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])
        self.app.split_return("R2", "R3",
                              [{"sku": "C", "quantity": 1}, {"sku": "T", "quantity": 2}],
                              [{"sku": "T", "quantity": 1}])
        self.app.cancel_return("R3")
        receipt = self.app.receive_return("R2")
        self.assertEqual([(line["sku"], line["quantity"]) for line in receipt["lines"]], [("C", 1), ("T", 1)])
        # The merged-away source id stays occupied for the whole root.
        snapshot = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.cancel_return("R1")
        with self.assertRaises(ValueError):
            self.app.receive_return("R1")
        self.assertEqual(self.app.path.read_bytes(), snapshot)

    def test_repeated_merge_rejected(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        self.app.merge_returns("R1", "R2", [{"sku": "T", "quantity": 2}],
                               [{"sku": "T", "quantity": 1}])
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.merge_returns("R1", "R2", [{"sku": "T", "quantity": 2}],
                                   [{"sku": "T", "quantity": 1}])
        # The merged target cannot be merged away under stale expectations either.
        with self.assertRaises(ValueError):
            self.app.merge_returns("R2", "R1", [{"sku": "T", "quantity": 1}],
                                   [{"sku": "T", "quantity": 2}])
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_cli_merge_returns_and_array_partial_failure(self):
        self._shipped("O1", [{"sku": "T", "quantity": 6}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R3", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R4", [{"sku": "T", "quantity": 1}])
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([
            {"source_id": "R1", "target_id": "R2",
             "expected_source_lines": [{"sku": "T", "quantity": 1}],
             "expected_target_lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": "R3", "target_id": "R4",
             "expected_source_lines": [{"sku": "T", "quantity": 9}],
             "expected_target_lines": [{"sku": "T", "quantity": 1}]},
        ]), encoding="utf-8")
        # Each array entry runs independently: the first succeeds, the second
        # fails and the command reports the failure.
        run = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "merge-returns", str(batch)],
            text=True, capture_output=True)
        self.assertEqual(run.returncode, 2, run.stdout)
        self.assertIn("expected source lines", json.loads(run.stderr)["error"])
        records = self.app.get_returns("O1")["records"]
        self.assertEqual([(r["return_id"], r["lines"]) for r in records], [
            ("R2", [{"sku": "T", "quantity": 2}]),
            ("R3", [{"sku": "T", "quantity": 1}]),
            ("R4", [{"sku": "T", "quantity": 1}]),
        ])
        # A fully successful call prints the result JSON and exits zero.
        single = self.root / "single.json"
        single.write_text(json.dumps({
            "source_id": "R3", "target_id": "R4",
            "expected_source_lines": [{"sku": "T", "quantity": 1}],
            "expected_target_lines": [{"sku": "T", "quantity": 1}],
        }), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "merge-returns", str(single)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        payload = json.loads(ok.stdout)
        self.assertEqual(set(payload), {"source", "target_before", "target_after"})
        self.assertEqual(payload["target_after"]["lines"], [{"sku": "T", "quantity": 2}])


if __name__ == "__main__":
    unittest.main()
