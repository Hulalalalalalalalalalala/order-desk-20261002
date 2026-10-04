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

    def test_merge_folds_quantities_and_returns_three_snapshots(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 3}])
        self.app.record_return("O1", "R1", [
            {"sku": "C", "quantity": 2},
            {"sku": "T", "quantity": 3},
        ])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        result = self.app.merge_returns("R1", "R2", [
            {"sku": "T", "quantity": 1},
            {"sku": "C", "quantity": 2},
            {"sku": "T", "quantity": 2, "ignored": True},
        ], [{"sku": "T", "quantity": 1}])
        self.assertEqual(set(result), {"source", "target_before", "target_after"})
        self.assertEqual(result, {
            "source": {"order_id": "O1", "return_id": "R1", "lines": [
                {"sku": "C", "quantity": 2},
                {"sku": "T", "quantity": 3},
            ]},
            "target_before": {"order_id": "O1", "return_id": "R2", "lines": [
                {"sku": "T", "quantity": 1},
            ]},
            "target_after": {"order_id": "O1", "return_id": "R2", "lines": [
                {"sku": "C", "quantity": 2},
                {"sku": "T", "quantity": 4},
            ]},
        })
        for snapshot in (result["source"], result["target_before"], result["target_after"]):
            self.assertEqual(set(snapshot), {"order_id", "return_id", "lines"})
            self.assertTrue(all(set(line) == {"sku", "quantity"} for line in snapshot["lines"]))
        # The source leaves the active registrations; only the target remains.
        view = self.app.get_returns("O1")
        self.assertEqual(view["records"], [
            {"order_id": "O1", "return_id": "R2", "lines": [
                {"sku": "C", "quantity": 2},
                {"sku": "T", "quantity": 4},
            ]},
        ])
        # The cumulative returned quantity and the returnable allowance do not change.
        self.assertEqual(view["remaining"], [
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 1},
        ])

    def test_identifiers_trimmed_and_case_sensitive(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        result = self.app.merge_returns("  R1 ", "  R2 ",
                                        [{"sku": "T", "quantity": 3}],
                                        [{"sku": "T", "quantity": 1}])
        self.assertEqual(result["source"]["return_id"], "R1")
        self.assertEqual(result["target_after"]["return_id"], "R2")
        # Case-sensitive sku comparison against the stored record.
        self.app.record_return("O1", "R3", [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.merge_returns("R2", "R3", [{"sku": "t", "quantity": 4}],
                                   [{"sku": "T", "quantity": 1}])

    def test_expected_lists_merged_and_compared_without_row_order(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 3}])
        self.app.record_return("O1", "R1", [
            {"sku": "T", "quantity": 1}, {"sku": "C", "quantity": 1}, {"sku": "T", "quantity": 1},
        ])
        self.app.record_return("O1", "R2", [{"sku": "C", "quantity": 2}])
        result = self.app.merge_returns("R1", "R2", [
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 1},
            {"sku": "T", "quantity": 1},
        ], [{"sku": "C", "quantity": 1}, {"sku": "C", "quantity": 1}])
        self.assertEqual(result["source"]["lines"], [
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 2},
        ])
        self.assertEqual(result["target_after"]["lines"], [
            {"sku": "C", "quantity": 3},
            {"sku": "T", "quantity": 2},
        ])

    def test_either_expected_mismatch_rejected(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 2}])
        good = [{"sku": "T", "quantity": 2}]
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.merge_returns("R1", "R2", [{"sku": "T", "quantity": 1}], good)
        with self.assertRaises(ValueError):
            self.app.merge_returns("R1", "R2", good, [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.path.read_bytes(), before)
        # Nothing changed.
        self.assertEqual([r["return_id"] for r in self.app.get_returns("O1")["records"]], ["R1", "R2"])

    def test_unknown_cancelled_and_received_returns_rejected(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R3", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R4", [{"sku": "T", "quantity": 1}])
        self.app.cancel_return("R2")
        self.app.receive_return("R3")
        good = [{"sku": "T", "quantity": 1}]
        snapshot = self.app.path.read_bytes()
        for source_id, target_id in (
            ("R9", "R1"), ("R2", "R1"), ("R3", "R1"),
            ("R1", "R9"), ("R1", "R2"), ("R1", "R3"),
        ):
            with self.assertRaises(ValueError):
                self.app.merge_returns(source_id, target_id, good, good)
        self.assertEqual(self.app.path.read_bytes(), snapshot)

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
                    {"order_id": "OLD", "return_id": "RP", "lines": [{"sku": "T", "quantity": 1}]},
                    {"order_id": "OLD", "return_id": "RQ", "lines": [{"sku": "T", "quantity": 1}]},
                ],
                "GONE": [
                    {"order_id": "GONE", "return_id": "RG", "lines": [{"sku": "T", "quantity": 1}]},
                    {"order_id": "GONE", "return_id": "RH", "lines": [{"sku": "T", "quantity": 1}]},
                ],
            },
        }
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        before = app.path.read_bytes()
        good = [{"sku": "T", "quantity": 1}]
        with self.assertRaises(ValueError):
            app.merge_returns("RP", "RQ", good, good)
        with self.assertRaises(ValueError):
            app.merge_returns("RG", "RH", good, good)
        self.assertEqual(app.path.read_bytes(), before)

    def test_delivered_order_can_be_merged(self):
        self._shipped("O1", [{"sku": "T", "quantity": 3}])
        self.app.confirm_delivery("O1", "Sam", "2026-10-01")
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        result = self.app.merge_returns("R1", "R2", [{"sku": "T", "quantity": 2}],
                                        [{"sku": "T", "quantity": 1}])
        self.assertEqual(result["target_after"]["lines"], [{"sku": "T", "quantity": 3}])

    def test_same_id_and_different_orders_rejected(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.restock("T", 10)
        self.app.place("O2", [{"sku": "T", "quantity": 2}])
        self.app.ship("O2", "DHL", "TRK-O2")
        self.app.record_return("O2", "R2", [{"sku": "T", "quantity": 1}])
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.merge_returns("R1", "R1", [{"sku": "T", "quantity": 1}],
                                   [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.merge_returns("R1", "R2", [{"sku": "T", "quantity": 1}],
                                   [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_paused_unmanaged_and_uncatalogued_products_do_not_block(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}, {"sku": "U", "quantity": 2}],
                      stock={"T": 10})
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R2", [{"sku": "U", "quantity": 2}])
        self.app.set_product_enabled("T", False)
        # A product dropped from the catalog after ordering does not block either.
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        del data["products"]["U"]
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        result = OrderDesk(self.root).merge_returns(
            "R1", "R2", [{"sku": "T", "quantity": 2}], [{"sku": "U", "quantity": 2}])
        self.assertEqual(result["target_after"]["lines"], [
            {"sku": "T", "quantity": 2},
            {"sku": "U", "quantity": 2},
        ])

    def test_invalid_identifiers_and_line_shapes(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 2}])
        good = [{"sku": "T", "quantity": 2}]
        for bad_id in (None, 123, 1.5, b"R1", ["R1"], {"x": 1}, "   ", "\t\n", True):
            with self.assertRaises(ValueError):
                self.app.merge_returns(bad_id, "R2", good, good)
            with self.assertRaises(ValueError):
                self.app.merge_returns("R1", bad_id, good, good)
        for bad_lines in (None, "x", 5, {}, [], [{"sku": "T"}], [{"quantity": 1}],
                          [["sku"]], ["x"], [{"sku": " ", "quantity": 1}],
                          [{"sku": "T", "quantity": 0}], [{"sku": "T", "quantity": -1}],
                          [{"sku": "T", "quantity": 1.5}], [{"sku": "T", "quantity": "1"}],
                          [{"sku": "T", "quantity": True}]):
            with self.assertRaises(ValueError):
                self.app.merge_returns("R1", "R2", bad_lines, good)
            with self.assertRaises(ValueError):
                self.app.merge_returns("R1", "R2", good, bad_lines)

    def test_failures_create_no_directory_and_consume_no_sequence(self):
        empty_root = Path(self.temp.name) / "empty"
        fresh = OrderDesk(empty_root)
        good = [{"sku": "T", "quantity": 1}]
        with self.assertRaises(ValueError):
            fresh.merge_returns("R1", "R2", good, good)
        self.assertFalse(empty_root.exists())
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 2}])
        with self.assertRaises(ValueError):
            self.app.merge_returns("R1", "R2", [{"sku": "T", "quantity": 9}], good)
        self.app.merge_returns("R1", "R2", [{"sku": "T", "quantity": 2}],
                               [{"sku": "T", "quantity": 2}])
        events = self.app.history("O1")["events"]
        self.assertEqual([(e["sequence"], e["action"]) for e in events],
                         [(1, "place"), (2, "ship"), (3, "record-return"),
                          (4, "record-return"), (5, "merge-returns")])

    def test_history_event_snapshot_and_legacy_order(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 2}])
        result = self.app.merge_returns("R1", "R2", [{"sku": "T", "quantity": 2}],
                                        [{"sku": "T", "quantity": 2}])
        history = self.app.history("O1")
        self.assertTrue(history["complete"])
        event = history["events"][-1]
        self.assertEqual(set(event), {"sequence", "action", "result"})
        self.assertEqual(event["action"], "merge-returns")
        self.assertEqual(event["result"], result)
        # Exactly one event is appended and older snapshots stay untouched.
        self.assertEqual([e["action"] for e in history["events"]],
                         ["place", "ship", "record-return", "record-return", "merge-returns"])
        # A legacy order without history starts at 1 with complete=False, and a
        # legacy registration without a receipt reads as pending.
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
                {"order_id": "OLD", "return_id": "L2", "lines": [{"sku": "T", "quantity": 1}]},
            ]},
        }
        legacy_root = Path(self.temp.name) / "legacy"
        legacy_root.mkdir()
        OrderDesk(legacy_root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(legacy_root)
        one = [{"sku": "T", "quantity": 1}]
        result = app.merge_returns("L1", "L2", one, one)
        history = app.history("OLD")
        self.assertFalse(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                         [(1, "merge-returns")])
        self.assertEqual(history["events"][0]["result"], result)

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
        self.assertNotIn("R1", raw.get("returns", {}).get("O1", []))
        self.assertNotIn("R2", raw.get("return_receipts", {}))
        with self.assertRaises(ValueError):
            self.app.get_return_receipt("R2")
        with self.assertRaises(ValueError):
            self.app.get_return_receipt("R1")

    def test_persistence_worklist_and_receiving_merged_target(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}, {"sku": "U", "quantity": 2}],
                      stock={"T": 10})
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R2", [{"sku": "U", "quantity": 2}])
        self.app.merge_returns("R1", "R2", [{"sku": "T", "quantity": 3}],
                               [{"sku": "U", "quantity": 2}])
        reopened = OrderDesk(self.root)
        cancelled = reopened.return_worklist("cancelled")
        self.assertEqual([(e["return_id"], e["stage"]) for e in cancelled], [("R1", "cancelled")])
        # The source stays in the cancelled stage with its ORIGINAL quantities.
        self.assertEqual(cancelled[0]["lines"], [{"sku": "T", "quantity": 3}])
        pending = reopened.return_worklist("pending")
        self.assertEqual(len(pending), 1)
        entry = pending[0]
        self.assertEqual(entry["return_id"], "R2")
        self.assertEqual(entry["lines"], [
            {"sku": "T", "quantity": 3},
            {"sku": "U", "quantity": 2},
        ])
        # Blockers are judged on the merged lines: U is unmanaged.
        self.assertFalse(entry["can_receive"])
        self.assertEqual(entry["blockers"], [{"sku": "U", "reason": "unmanaged"}])
        # Manage the missing product: the merged target can now be received whole.
        reopened.restock("U", 4)
        pending = reopened.return_worklist("pending")
        self.assertTrue(pending[0]["can_receive"])
        receipt = reopened.receive_return("R2")
        self.assertEqual([(line["sku"], line["quantity"]) for line in receipt["lines"]],
                         [("T", 3), ("U", 2)])
        self.assertEqual(reopened.stock("T")["on_hand"], 8)
        self.assertEqual(reopened.stock("U")["on_hand"], 6)
        # The source cannot be received, amended or cancelled again.
        for call in (
            lambda: reopened.receive_return("R1"),
            lambda: reopened.cancel_return("R1"),
            lambda: reopened.amend_return("R1", [{"sku": "T", "quantity": 3}],
                                          [{"sku": "T", "quantity": 3}]),
        ):
            with self.assertRaises(ValueError):
                call()

    def test_quantity_and_amount_queries_unchanged_by_merge(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        self.app.merge_returns("R1", "R2", [{"sku": "T", "quantity": 3}],
                               [{"sku": "T", "quantity": 1}])
        progress = self.app.order_progress("O1")["lines"]
        self.assertEqual(progress, [{"sku": "T", "ordered": 5, "reserved": 0, "needed": 0,
                                     "shipped": 5, "pending": 4, "received": 0,
                                     "remaining": 1, "net": 5}])
        # The order worklist keeps one receive-return task for the order.
        worklist = self.app.order_worklist("receive-return")
        self.assertEqual([item["order_id"] for item in worklist], ["O1"])
        summary = self.app.fulfillment_summary()
        self.assertEqual(summary["order_count"], 1)
        self.assertEqual(summary["lines"], [{
            "sku": "T", "shipped": 5, "pending": 4, "received": 0, "net": 5,
            "shipped_cents": 500, "pending_cents": 400, "received_cents": 0, "net_cents": 500,
        }])
        self.assertEqual(summary["totals"], {
            "shipped": 5, "pending": 4, "received": 0, "net": 5,
            "shipped_cents": 500, "pending_cents": 400, "received_cents": 0, "net_cents": 500,
        })

    def test_follow_up_operations_use_new_target_quantities(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 3}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R2", [{"sku": "C", "quantity": 2}])
        self.app.merge_returns("R1", "R2", [{"sku": "T", "quantity": 3}],
                               [{"sku": "C", "quantity": 2}])
        # Amend the merged target at the new quantities.
        self.app.amend_return("R2", [
            {"sku": "C", "quantity": 2},
            {"sku": "T", "quantity": 3},
        ], [{"sku": "C", "quantity": 1}, {"sku": "T", "quantity": 3}])
        # Split the merged target; the new registration and what remains are both pending.
        split = self.app.split_return("R2", "R5", [
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 3},
        ], [{"sku": "T", "quantity": 2}])
        self.assertEqual(split["target"]["lines"], [{"sku": "T", "quantity": 2}])
        receipts = self.app.receive_return_batch(["R2", "R5"])
        self.assertEqual([r["return_id"] for r in receipts], ["R2", "R5"])
        self.assertEqual(self.app.stock("T")["on_hand"], 8)
        self.assertEqual(self.app.stock("C")["on_hand"], 8)
        # The source id stays occupied for the whole root.
        snapshot = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.split_return("R1", "R6", [{"sku": "T", "quantity": 3}],
                                  [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.path.read_bytes(), snapshot)

    def test_cancel_merged_target_frees_full_allowance(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        self.app.merge_returns("R1", "R2", [{"sku": "T", "quantity": 3}],
                               [{"sku": "T", "quantity": 1}])
        self.app.cancel_return("R2")
        view = self.app.get_returns("O1")
        self.assertEqual(view["records"], [])
        self.assertEqual(view["remaining"], [{"sku": "T", "quantity": 5}])
        stages = self.app.return_worklist("all")
        self.assertEqual([(e["return_id"], e["stage"]) for e in stages],
                         [("R1", "cancelled"), ("R2", "cancelled")])
        # Both ids remain occupied.
        snapshot = self.app.path.read_bytes()
        for return_id in ("R1", "R2"):
            with self.assertRaises(ValueError):
                self.app.record_return("O1", return_id, [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.path.read_bytes(), snapshot)

    def test_repeated_merge_rejected(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 2}])
        self.app.merge_returns("R1", "R2", [{"sku": "T", "quantity": 2}],
                               [{"sku": "T", "quantity": 2}])
        snapshot = self.app.path.read_bytes()
        # The source is already cancelled; and even the reverse direction fails
        # because the original target list no longer matches.
        with self.assertRaises(ValueError):
            self.app.merge_returns("R1", "R2", [{"sku": "T", "quantity": 2}],
                                   [{"sku": "T", "quantity": 4}])
        with self.assertRaises(ValueError):
            self.app.merge_returns("R2", "R1", [{"sku": "T", "quantity": 4}],
                                   [{"sku": "T", "quantity": 2}])
        self.assertEqual(self.app.path.read_bytes(), snapshot)
        # Only one merge event exists.
        self.assertEqual([e["action"] for e in self.app.history("O1")["events"]].count("merge-returns"), 1)

    def test_cli_merge_returns_and_array_partial_failure(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R3", [{"sku": "T", "quantity": 2}])
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([
            {"source_id": "R1", "target_id": "R2",
             "expected_source_lines": [{"sku": "T", "quantity": 2}],
             "expected_target_lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": "R3", "target_id": "R4",
             "expected_source_lines": [{"sku": "T", "quantity": 2}],
             "expected_target_lines": [{"sku": "T", "quantity": 1}]},
        ]), encoding="utf-8")
        # Each array entry runs independently: the first succeeds, the second
        # fails (R4 unknown) and the command reports the failure.
        run = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "merge-returns", str(batch)],
            text=True, capture_output=True)
        self.assertEqual(run.returncode, 2, run.stdout)
        self.assertIn("unknown return", json.loads(run.stderr)["error"])
        records = self.app.get_returns("O1")["records"]
        self.assertEqual([(r["return_id"], r["lines"]) for r in records], [
            ("R2", [{"sku": "T", "quantity": 3}]),
            ("R3", [{"sku": "T", "quantity": 2}]),
        ])
        # A fully successful call prints the result JSON and exits zero.
        single = self.root / "single.json"
        single.write_text(json.dumps({
            "source_id": "R3", "target_id": "R2",
            "expected_source_lines": [{"sku": "T", "quantity": 2}],
            "expected_target_lines": [{"sku": "T", "quantity": 3}],
        }), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "merge-returns", str(single)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        payload = json.loads(ok.stdout)
        self.assertEqual(set(payload), {"source", "target_before", "target_after"})
        self.assertEqual(payload["target_after"]["return_id"], "R2")
        self.assertEqual(payload["target_after"]["lines"], [{"sku": "T", "quantity": 5}])


if __name__ == "__main__":
    unittest.main()
