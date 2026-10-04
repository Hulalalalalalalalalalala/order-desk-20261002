import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk


class SplitReturnTests(unittest.TestCase):
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

    def test_split_moves_quantities_and_returns_three_snapshots(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 3}])
        self.app.record_return("O1", "R1", [
            {"sku": "C", "quantity": 2},
            {"sku": "T", "quantity": 3},
        ])
        result = self.app.split_return("R1", "R2", [
            {"sku": "T", "quantity": 3},
            {"sku": "C", "quantity": 2},
        ], [
            {"sku": "T", "quantity": 1},
            {"sku": "C", "quantity": 2},
            {"sku": "T", "quantity": 1, "ignored": True},
        ])
        self.assertEqual(set(result), {"before", "source", "target"})
        self.assertEqual(result, {
            "before": {"order_id": "O1", "return_id": "R1", "lines": [
                {"sku": "C", "quantity": 2},
                {"sku": "T", "quantity": 3},
            ]},
            "source": {"order_id": "O1", "return_id": "R1", "lines": [
                {"sku": "T", "quantity": 1},
            ]},
            "target": {"order_id": "O1", "return_id": "R2", "lines": [
                {"sku": "C", "quantity": 2},
                {"sku": "T", "quantity": 2},
            ]},
        })
        for snapshot in (result["before"], result["source"], result["target"]):
            self.assertEqual(set(snapshot), {"order_id", "return_id", "lines"})
            self.assertTrue(all(set(line) == {"sku", "quantity"} for line in snapshot["lines"]))
        view = self.app.get_returns("O1")
        self.assertEqual(view["records"], [
            {"order_id": "O1", "return_id": "R1", "lines": [{"sku": "T", "quantity": 1}]},
            {"order_id": "O1", "return_id": "R2", "lines": [
                {"sku": "C", "quantity": 2},
                {"sku": "T", "quantity": 2},
            ]},
        ])
        # The cumulative returned quantity and the returnable allowance do not change.
        self.assertEqual(view["remaining"], [
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 2},
        ])

    def test_identifiers_trimmed_and_case_sensitive(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        result = self.app.split_return("  R1 ", "  R2 ", [{"sku": "T", "quantity": 3}],
                                       [{"sku": "T", "quantity": 1}])
        self.assertEqual(result["source"]["return_id"], "R1")
        self.assertEqual(result["target"]["return_id"], "R2")
        # Case-sensitive sku comparison against the stored record.
        with self.assertRaises(ValueError):
            self.app.split_return("R1", "R3", [{"sku": "t", "quantity": 1}],
                                  [{"sku": "t", "quantity": 1}])

    def test_expected_merged_and_compared_without_row_order(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 3}])
        self.app.record_return("O1", "R1", [
            {"sku": "T", "quantity": 1},
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 1},
        ])
        result = self.app.split_return("R1", "R2", [
            {"sku": "T", "quantity": 1},
            {"sku": "T", "quantity": 1},
            {"sku": "C", "quantity": 1},
        ], [{"sku": "T", "quantity": 1}])
        self.assertEqual(result["before"]["lines"], [
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 2},
        ])
        self.assertEqual(result["source"]["lines"], [
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 1},
        ])
        self.assertEqual(result["target"]["lines"], [{"sku": "T", "quantity": 1}])

    def test_expected_mismatch_rejected(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.split_return("R1", "R2", [{"sku": "T", "quantity": 1}],
                                  [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_unknown_cancelled_and_received_sources_rejected(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R3", [{"sku": "T", "quantity": 1}])
        self.app.cancel_return("R2")
        self.app.receive_return("R3")
        good = [{"sku": "T", "quantity": 1}]
        snapshot = self.app.path.read_bytes()
        for source_id in ("R9", "R2", "R3"):
            with self.assertRaises(ValueError):
                self.app.split_return(source_id, "RX", good, good)
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
                "OLD": [{"order_id": "OLD", "return_id": "RP", "lines": [{"sku": "T", "quantity": 1}]}],
                "GONE": [{"order_id": "GONE", "return_id": "RG", "lines": [{"sku": "T", "quantity": 1}]}],
            },
        }
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        before = app.path.read_bytes()
        good = [{"sku": "T", "quantity": 1}]
        for return_id in ("RP", "RG"):
            with self.assertRaises(ValueError):
                app.split_return(return_id, "RX", good, good)
        self.assertEqual(app.path.read_bytes(), before)

    def test_delivered_order_can_be_split(self):
        self._shipped("O1", [{"sku": "T", "quantity": 3}])
        self.app.confirm_delivery("O1", "Sam", "2026-10-01")
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        result = self.app.split_return("R1", "R2", [{"sku": "T", "quantity": 2}],
                                       [{"sku": "T", "quantity": 1}])
        self.assertEqual(result["target"]["lines"], [{"sku": "T", "quantity": 1}])

    def test_same_id_and_occupied_target_rejected_but_order_and_cart_names_allowed(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        one = [{"sku": "T", "quantity": 1}]
        with self.assertRaises(ValueError):
            self.app.split_return("R1", "R1", [{"sku": "T", "quantity": 3}], one)
        with self.assertRaises(ValueError):
            self.app.split_return("R1", "R2", [{"sku": "T", "quantity": 3}], one)
        # A cancelled id occupies the namespace too.
        self.app.cancel_return("R2")
        with self.assertRaises(ValueError):
            self.app.split_return("R1", "R2", [{"sku": "T", "quantity": 3}], one)
        # A received id occupies the namespace as well.
        self.app.record_return("O1", "R3", [{"sku": "T", "quantity": 1}])
        self.app.receive_return("R3")
        with self.assertRaises(ValueError):
            self.app.split_return("R1", "R3", [{"sku": "T", "quantity": 3}], one)
        # Order ids and cart ids live in separate namespaces.
        result = self.app.split_return("R1", "O1", [{"sku": "T", "quantity": 3}], one)
        self.assertEqual(result["target"]["return_id"], "O1")
        self.app.save_cart("O2", [{"sku": "T", "quantity": 1}])
        result = self.app.split_return("R1", "O2", [{"sku": "T", "quantity": 2}], one)
        self.assertEqual(result["target"]["return_id"], "O2")
        self.assertEqual(result["source"]["lines"], [{"sku": "T", "quantity": 1}])

    def test_moved_sku_must_be_in_source_and_cannot_exceed_it(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 3}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.split_return("R1", "R2", [{"sku": "T", "quantity": 2}],
                                  [{"sku": "C", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.split_return("R1", "R2", [{"sku": "T", "quantity": 2}],
                                  [{"sku": "T", "quantity": 3}])
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_splitting_the_whole_registration_rejected(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.split_return("R1", "R2", [{"sku": "T", "quantity": 2}],
                                  [{"sku": "T", "quantity": 1}, {"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_paused_unmanaged_and_uncatalogued_products_do_not_block(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}, {"sku": "U", "quantity": 2}],
                      stock={"T": 10})
        self.app.record_return("O1", "R1", [
            {"sku": "T", "quantity": 2},
            {"sku": "U", "quantity": 2},
        ])
        self.app.set_product_enabled("T", False)
        result = self.app.split_return("R1", "R2", [
            {"sku": "T", "quantity": 2},
            {"sku": "U", "quantity": 2},
        ], [{"sku": "T", "quantity": 1}, {"sku": "U", "quantity": 1}])
        self.assertEqual(result["source"]["lines"], [
            {"sku": "T", "quantity": 1},
            {"sku": "U", "quantity": 1},
        ])
        # A product dropped from the catalog after ordering can still be split.
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        del data["products"]["U"]
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        result = OrderDesk(self.root).split_return(
            "R1", "R3", [{"sku": "T", "quantity": 1}, {"sku": "U", "quantity": 1}],
            [{"sku": "U", "quantity": 1}])
        self.assertEqual(result["target"]["lines"], [{"sku": "U", "quantity": 1}])

    def test_invalid_identifiers_and_line_shapes(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        good = [{"sku": "T", "quantity": 2}]
        move = [{"sku": "T", "quantity": 1}]
        for bad_id in (None, 123, 1.5, b"R1", ["R1"], {"x": 1}, "   ", "\t\n", True):
            with self.assertRaises(ValueError):
                self.app.split_return(bad_id, "R2", good, move)
            with self.assertRaises(ValueError):
                self.app.split_return("R1", bad_id, good, move)
        for bad_lines in (None, "x", 5, {}, [], [{"sku": "T"}], [{"quantity": 1}],
                          [["sku"]], ["x"], [{"sku": " ", "quantity": 1}],
                          [{"sku": "T", "quantity": 0}], [{"sku": "T", "quantity": -1}],
                          [{"sku": "T", "quantity": 1.5}], [{"sku": "T", "quantity": "1"}],
                          [{"sku": "T", "quantity": True}]):
            with self.assertRaises(ValueError):
                self.app.split_return("R1", "R2", bad_lines, move)
            with self.assertRaises(ValueError):
                self.app.split_return("R1", "R2", good, bad_lines)

    def test_failures_create_no_directory_and_consume_no_sequence(self):
        empty_root = Path(self.temp.name) / "empty"
        fresh = OrderDesk(empty_root)
        good = [{"sku": "T", "quantity": 1}]
        with self.assertRaises(ValueError):
            fresh.split_return("R1", "R2", good, good)
        self.assertFalse(empty_root.exists())
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        with self.assertRaises(ValueError):
            self.app.split_return("R1", "R2", [{"sku": "T", "quantity": 9}],
                                  [{"sku": "T", "quantity": 1}])
        self.app.split_return("R1", "R2", [{"sku": "T", "quantity": 2}],
                              [{"sku": "T", "quantity": 1}])
        events = self.app.history("O1")["events"]
        self.assertEqual([(e["sequence"], e["action"]) for e in events],
                         [(1, "place"), (2, "ship"), (3, "record-return"), (4, "split-return")])

    def test_history_event_snapshot_and_legacy_order(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        result = self.app.split_return("R1", "R2", [{"sku": "T", "quantity": 2}],
                                       [{"sku": "T", "quantity": 1}])
        history = self.app.history("O1")
        self.assertTrue(history["complete"])
        event = history["events"][-1]
        self.assertEqual(set(event), {"sequence", "action", "result"})
        self.assertEqual(event["action"], "split-return")
        self.assertEqual(event["result"], result)
        # Exactly one event is appended and older snapshots stay untouched.
        self.assertEqual([e["action"] for e in history["events"]],
                         ["place", "ship", "record-return", "split-return"])
        self.assertEqual(history["events"][2]["result"]["lines"], [{"sku": "T", "quantity": 2}])
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
                {"order_id": "OLD", "return_id": "L1", "lines": [{"sku": "T", "quantity": 2}]},
            ]},
        }
        legacy_root = Path(self.temp.name) / "legacy"
        legacy_root.mkdir()
        OrderDesk(legacy_root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(legacy_root)
        result = app.split_return("L1", "L2", [{"sku": "T", "quantity": 2}],
                                  [{"sku": "T", "quantity": 1}])
        history = app.history("OLD")
        self.assertFalse(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                         [(1, "split-return")])
        self.assertEqual(history["events"][0]["result"], result)

    def test_stock_reservations_order_shipment_and_receipts_untouched(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}, {"sku": "C", "quantity": 1}])
        self.app.place("O2", [{"sku": "T", "quantity": 2}])
        order_before = self.app.get("O1")
        stocks = {sku: self.app.stock(sku) for sku in ("T", "C")}
        stock_events = {sku: len(self.app.stock_history(sku)["events"]) for sku in ("T", "C")}
        self.app.split_return("R1", "R2", [
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 1},
        ], [{"sku": "C", "quantity": 1}])
        self.assertEqual(self.app.get("O1"), order_before)
        self.assertEqual(self.app.get("O1")["shipment"], {"carrier": "DHL", "tracking_no": "TRK-O1"})
        for sku in ("T", "C"):
            self.assertEqual(self.app.stock(sku), stocks[sku])
            self.assertEqual(len(self.app.stock_history(sku)["events"]), stock_events[sku])
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["reservations"]["O2"], {"T": 2})
        self.assertNotIn("R2", raw.get("return_receipts", {}))
        with self.assertRaises(ValueError):
            self.app.get_return_receipt("R2")

    def test_persistence_worklist_and_receiving_only_target(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.app.split_return("R1", "R2", [{"sku": "T", "quantity": 3}],
                              [{"sku": "T", "quantity": 1}])
        reopened = OrderDesk(self.root)
        entries = reopened.return_worklist()
        self.assertEqual([(e["return_id"], e["stage"]) for e in entries], [("R1", "pending"), ("R2", "pending")])
        self.assertEqual(entries[0]["lines"], [{"sku": "T", "quantity": 2}])
        self.assertEqual(entries[1]["lines"], [{"sku": "T", "quantity": 1}])
        # Receive only the new registration: the source stays pending.
        receipt = reopened.receive_return("R2")
        self.assertEqual([(line["sku"], line["quantity"]) for line in receipt["lines"]], [("T", 1)])
        self.assertEqual(reopened.stock("T")["on_hand"], 6)
        pending = reopened.return_worklist("pending")
        self.assertEqual([e["return_id"] for e in pending], ["R1"])
        received = reopened.return_worklist("received")
        self.assertEqual([e["return_id"] for e in received], ["R2"])
        # The source can still be received afterwards.
        receipt = reopened.receive_return("R1")
        self.assertEqual([(line["sku"], line["quantity"]) for line in receipt["lines"]], [("T", 2)])
        self.assertEqual(reopened.stock("T")["on_hand"], 8)

    def test_quantity_and_amount_queries_count_both_registrations_by_stage(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.app.split_return("R1", "R2", [{"sku": "T", "quantity": 3}],
                              [{"sku": "T", "quantity": 1}])
        progress = self.app.order_progress("O1")["lines"]
        self.assertEqual(progress, [{"sku": "T", "ordered": 5, "reserved": 0, "needed": 0,
                                     "shipped": 5, "pending": 3, "received": 0,
                                     "remaining": 2, "net": 5}])
        # The order worklist keeps one receive-return task for the order.
        worklist = self.app.order_worklist("receive-return")
        self.assertEqual([item["order_id"] for item in worklist], ["O1"])
        # Receive the target only: pending/received/net split across the two registrations.
        self.app.receive_return("R2")
        progress = self.app.order_progress("O1")["lines"]
        self.assertEqual(progress, [{"sku": "T", "ordered": 5, "reserved": 0, "needed": 0,
                                     "shipped": 5, "pending": 2, "received": 1,
                                     "remaining": 2, "net": 4}])
        summary = self.app.fulfillment_summary()
        self.assertEqual(summary["order_count"], 1)
        self.assertEqual(summary["lines"], [{
            "sku": "T", "shipped": 5, "pending": 2, "received": 1, "net": 4,
            "shipped_cents": 500, "pending_cents": 200, "received_cents": 100, "net_cents": 400,
        }])
        self.assertEqual(summary["totals"], {
            "shipped": 5, "pending": 2, "received": 1, "net": 4,
            "shipped_cents": 500, "pending_cents": 200, "received_cents": 100, "net_cents": 400,
        })

    def test_each_registration_can_be_cancelled_independently(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.app.split_return("R1", "R2", [{"sku": "T", "quantity": 3}],
                              [{"sku": "T", "quantity": 1}])
        self.app.cancel_return("R2")
        view = self.app.get_returns("O1")
        self.assertEqual([r["return_id"] for r in view["records"]], ["R1"])
        self.assertEqual(view["remaining"], [{"sku": "T", "quantity": 3}])
        self.app.cancel_return("R1")
        self.assertEqual(self.app.get_returns("O1")["remaining"], [{"sku": "T", "quantity": 5}])
        # Both ids stay occupied.
        snapshot = self.app.path.read_bytes()
        for return_id in ("R1", "R2"):
            with self.assertRaises(ValueError):
                self.app.record_return("O1", return_id, [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.path.read_bytes(), snapshot)

    def test_amend_and_batch_receive_follow_the_split(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 3}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.app.split_return("R1", "R2", [{"sku": "T", "quantity": 3}],
                              [{"sku": "T", "quantity": 1}])
        self.app.amend_return("R1", [{"sku": "T", "quantity": 2}],
                              [{"sku": "T", "quantity": 1}, {"sku": "C", "quantity": 1}])
        # The two registrations can be received together in one all-or-nothing batch.
        receipts = self.app.receive_return_batch(["R2", "R1"])
        self.assertEqual([r["return_id"] for r in receipts], ["R1", "R2"])
        # Two T units return in total (R1 has one, R2 has one) and one C.
        self.assertEqual(self.app.stock("T")["on_hand"], 7)
        self.assertEqual(self.app.stock("C")["on_hand"], 8)
        # Both are now received; splitting either is rejected.
        snapshot = self.app.path.read_bytes()
        for return_id in ("R1", "R2"):
            with self.assertRaises(ValueError):
                self.app.split_return(return_id, "RX", [{"sku": "T", "quantity": 1}],
                                      [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.path.read_bytes(), snapshot)

    def test_repeated_target_submission_rejected_as_occupied(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.app.split_return("R1", "R2", [{"sku": "T", "quantity": 3}],
                              [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.split_return("R1", "R2", [{"sku": "T", "quantity": 2}],
                                  [{"sku": "T", "quantity": 1}])

    def test_cli_split_return_and_array_partial_failure(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R3", [{"sku": "T", "quantity": 2}])
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([
            {"source_id": "R1", "target_id": "R2",
             "expected_lines": [{"sku": "T", "quantity": 2}],
             "lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": "R3", "target_id": "R4",
             "expected_lines": [{"sku": "T", "quantity": 9}],
             "lines": [{"sku": "T", "quantity": 1}]},
        ]), encoding="utf-8")
        # Each array entry runs independently: the first succeeds, the second
        # fails and the command reports the failure.
        run = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "split-return", str(batch)],
            text=True, capture_output=True)
        self.assertEqual(run.returncode, 2, run.stdout)
        self.assertIn("expected lines", json.loads(run.stderr)["error"])
        records = self.app.get_returns("O1")["records"]
        self.assertEqual([(r["return_id"], r["lines"]) for r in records], [
            ("R1", [{"sku": "T", "quantity": 1}]),
            ("R2", [{"sku": "T", "quantity": 1}]),
            ("R3", [{"sku": "T", "quantity": 2}]),
        ])
        # A fully successful call prints the result JSON and exits zero.
        single = self.root / "single.json"
        single.write_text(json.dumps({
            "source_id": "R3", "target_id": "R4",
            "expected_lines": [{"sku": "T", "quantity": 2}],
            "lines": [{"sku": "T", "quantity": 1}],
        }), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "split-return", str(single)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        payload = json.loads(ok.stdout)
        self.assertEqual(set(payload), {"before", "source", "target"})
        self.assertEqual(payload["target"]["return_id"], "R4")


if __name__ == "__main__":
    unittest.main()
