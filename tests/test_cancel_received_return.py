import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk


class CancelReceivedReturnTests(unittest.TestCase):
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

    def _received(self, return_id, order_id, lines):
        self.app.record_return(order_id, return_id, lines)
        return self.app.receive_return(return_id)

    def test_reversal_deducts_receipt_quantities_from_current_stock(self):
        self._shipped("O1", [{"sku": "T", "quantity": 4}])
        receipt = self._received("R1", "O1", [
            {"sku": "T", "quantity": 1},
            {"sku": "T", "quantity": 2},
        ])
        self.assertEqual(receipt["lines"][0]["after"]["on_hand"], 9)
        result = self.app.cancel_received_return("R1")
        self.assertEqual(result, {
            "order_id": "O1",
            "return_id": "R1",
            "lines": [{
                "sku": "T",
                "quantity": 3,
                "before": {"sku": "T", "on_hand": 9, "reserved": 0, "available": 9},
                "after": {"sku": "T", "on_hand": 6, "reserved": 0, "available": 6},
            }],
        })
        self.assertEqual(set(result), {"order_id", "return_id", "lines"})
        self.assertEqual(set(result["lines"][0]), {"sku", "quantity", "before", "after"})
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 6, "reserved": 0, "available": 6})

    def test_lines_merged_sorted_and_later_business_changes_preserved(self):
        # Restock 10, O1 ships 4 (on_hand 6), the return of 2T + 1C is received
        # (T 8, C 9), then O2 ships 2T (T 6). The reversal subtracts the
        # ORIGINAL receipt quantities from the CURRENT on_hand, keeping the
        # later shipment: T -> 4, C -> 8.
        self._shipped("O1", [{"sku": "T", "quantity": 4}, {"sku": "C", "quantity": 1}])
        self._received("R1", "O1", [
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 1},
            {"sku": "T", "quantity": 1},
        ])
        self.app.place("O2", [{"sku": "T", "quantity": 2}])
        self.app.ship("O2", "DHL", "TRK-O2")
        result = self.app.cancel_received_return("R1")
        self.assertEqual([line["sku"] for line in result["lines"]], ["C", "T"])
        self.assertEqual(result["lines"][1]["quantity"], 2)
        self.assertEqual(result["lines"][1]["before"]["on_hand"], 6)
        self.assertEqual(result["lines"][1]["after"]["on_hand"], 4)
        self.assertEqual(self.app.stock("T")["on_hand"], 4)
        self.assertEqual(self.app.stock("C")["on_hand"], 9)

    def test_reservations_and_allocations_untouched_available_drops_equally(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        self._received("R1", "O1", [{"sku": "T", "quantity": 2}])
        # O2 holds a reservation while the reversal happens.
        self.app.place("O2", [{"sku": "T", "quantity": 3}])
        before = self.app.stock("T")
        self.assertEqual(before, {"sku": "T", "on_hand": 10, "reserved": 3, "available": 7})
        self.app.cancel_received_return("R1")
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 8, "reserved": 3, "available": 5})
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["reservations"]["O2"], {"T": 3})

    def test_order_status_lines_amounts_shipment_delivery_preserved(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        self.app.confirm_delivery("O1", "Ada", "2026-03-02")
        self._received("R1", "O1", [{"sku": "T", "quantity": 1}])
        order_before = self.app.get("O1")
        self.app.cancel_received_return("R1")
        order_after = self.app.get("O1")
        self.assertEqual(order_after, order_before)
        self.assertEqual(order_after["status"], "delivered")
        self.assertEqual(order_after["shipment"], {"carrier": "DHL", "tracking_no": "TRK-O1"})
        self.assertEqual(order_after["delivery"], {"recipient": "Ada", "delivered_on": "2026-03-02"})
        self.assertEqual(order_after["total_cents"], 200)

    def test_paused_sales_do_not_block(self):
        self._shipped("O1", [{"sku": "T", "quantity": 1}])
        self._received("R1", "O1", [{"sku": "T", "quantity": 1}])
        self.app.set_product_enabled("T", False)
        result = self.app.cancel_received_return("R1")
        self.assertEqual(result["lines"][0]["after"]["on_hand"], 9)

    def test_becoming_cancelled_frees_allowance_and_removes_record(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self._received("R1", "O1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        self.app.cancel_received_return("R1")
        view = self.app.get_returns("O1")
        self.assertEqual([r["return_id"] for r in view["records"]], ["R2"])
        self.assertEqual(view["remaining"], [{"sku": "T", "quantity": 4}])
        preview = self.app.quote_return("O1", [{"sku": "T", "quantity": 4}])
        self.assertTrue(preview["can_record"])
        self.assertEqual(preview["lines"][0]["remaining"], 4)

    def test_id_stays_occupied_and_cannot_be_received_amended_or_cancelled(self):
        self._shipped("O1", [{"sku": "T", "quantity": 3}])
        self._received("R1", "O1", [{"sku": "T", "quantity": 1}])
        self.app.cancel_received_return("R1")
        for call in (
            lambda: self.app.cancel_received_return("R1"),
            lambda: self.app.cancel_received_return("  R1  "),
            lambda: self.app.receive_return("R1"),
            lambda: self.app.cancel_return("R1"),
            lambda: self.app.amend_return("R1", [{"sku": "T", "quantity": 1}], [{"sku": "T", "quantity": 1}]),
        ):
            with self.assertRaises(ValueError):
                call()
        self._shipped("O2", [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.record_return("O2", "R1", [{"sku": "T", "quantity": 1}])
        # The freed quantity can be registered again under a fresh id.
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])

    def test_original_receipt_still_returned_with_original_snapshot(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        receipt = self._received("R1", "O1", [{"sku": "T", "quantity": 2}])
        self.app.restock("T", 5)
        self.app.cancel_received_return("R1")
        self.assertEqual(self.app.get_return_receipt("R1"), receipt)
        self.assertEqual(receipt["lines"][0]["after"]["on_hand"], 10)

    def test_pending_cancelled_and_unknown_returns_rejected(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "RP", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "RC", [{"sku": "T", "quantity": 1}])
        self.app.cancel_return("RC")
        with self.assertRaises(ValueError):
            self.app.cancel_received_return("RP")
        with self.assertRaises(ValueError):
            self.app.cancel_received_return("RC")
        with self.assertRaises(ValueError):
            self.app.cancel_received_return("nope")

    def test_invalid_identifiers(self):
        self._shipped("O1", [{"sku": "T", "quantity": 1}])
        for bad in (None, 123, 1.5, b"R1", ["R1"], {"x": 1}, "   ", "\t\n", True):
            with self.assertRaises(ValueError):
                self.app.cancel_received_return(bad)

    def test_whitespace_trimmed_and_case_sensitive(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        self._received("Ra", "O1", [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.cancel_received_return("  Ra  ")["return_id"], "Ra")
        with self.assertRaises(ValueError):
            self.app.cancel_received_return("ra")

    def test_negative_or_below_reserved_new_stock_rejects_whole_request(self):
        self._shipped("O1", [{"sku": "T", "quantity": 4}])
        self._received("R1", "O1", [{"sku": "T", "quantity": 4}])  # on_hand 10
        # A later shipment leaves only 4 on hand, so reversing the receipt of
        # 4 would be exactly zero and is fine; reserve first to create a
        # below-reserved failure.
        self.app.place("O2", [{"sku": "T", "quantity": 8}])
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.cancel_received_return("R1")
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.stock("T")["on_hand"], 10)
        self.app.ship("O2", "DHL", "TRK-O2")  # on_hand 2, reserved 0
        with self.assertRaises(ValueError):
            self.app.cancel_received_return("R1")  # would go to -2
        self.assertEqual(self.app.stock("T")["on_hand"], 2)
        self.assertEqual(self.app.get_return_receipt("R1")["lines"][0]["quantity"], 4)
        self.assertEqual([r["return_id"] for r in self.app.get_returns("O1")["records"]], ["R1"])

    def test_unknown_or_unmanaged_receipt_product_rejects_whole_request(self):
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
                {"order_id": "OLD", "return_id": "RX", "lines": [{"sku": "X", "quantity": 1}]},
            ]},
            "return_receipts": {"RX": {
                "order_id": "OLD", "return_id": "RX",
                "lines": [{"sku": "X", "quantity": 1,
                           "before": {"sku": "X", "on_hand": 0, "reserved": 0, "available": 0},
                           "after": {"sku": "X", "on_hand": 1, "reserved": 0, "available": 1}}],
            }},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        before = app.path.read_bytes()
        with self.assertRaises(ValueError):
            app.cancel_received_return("RX")
        self.assertEqual(app.path.read_bytes(), before)
        # Same shape but the product is known while its inventory record is gone.
        data["products"]["X"] = {"sku": "X", "name": "Ex", "price_cents": 5}
        app.path.write_text(json.dumps(data), encoding="utf-8")
        before = app.path.read_bytes()
        with self.assertRaises(ValueError):
            OrderDesk(self.root).cancel_received_return("RX")
        self.assertEqual(app.path.read_bytes(), before)

    def test_order_missing_or_wrong_status_rejected(self):
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "inventory": {"T": {"on_hand": 3, "reserved": 0}},
            "orders": {"OLD": {
                "order_id": "OLD", "status": "placed",
                "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200}],
                "total_cents": 200,
            }},
            "returns": {"OLD": [
                {"order_id": "OLD", "return_id": "RP", "lines": [{"sku": "T", "quantity": 1}]},
            ]},
            "return_receipts": {"RP": {
                "order_id": "OLD", "return_id": "RP",
                "lines": [{"sku": "T", "quantity": 1,
                           "before": {"sku": "T", "on_hand": 3, "reserved": 0, "available": 3},
                           "after": {"sku": "T", "on_hand": 4, "reserved": 0, "available": 4}}],
            }},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        before = app.path.read_bytes()
        with self.assertRaises(ValueError):
            app.cancel_received_return("RP")
        self.assertEqual(app.path.read_bytes(), before)
        raw = json.loads(before)
        del raw["orders"]
        app.path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(ValueError):
            OrderDesk(self.root).cancel_received_return("RP")

    def test_failure_consumes_no_order_or_stock_sequence(self):
        self._shipped("O1", [{"sku": "T", "quantity": 4}])
        self._received("R1", "O1", [{"sku": "T", "quantity": 4}])
        self.app.place("O2", [{"sku": "T", "quantity": 8}])
        with self.assertRaises(ValueError):
            self.app.cancel_received_return("R1")
        stock_events = [(e["sequence"], e["action"]) for e in self.app.stock_history("T")["events"]]
        self.assertEqual(stock_events, [
            (1, "restock"), (2, "place"), (3, "ship"), (4, "receive-return"), (5, "place"),
        ])
        order_events = [(e["sequence"], e["action"]) for e in self.app.history("O1")["events"]]
        self.assertEqual(order_events, [(1, "place"), (2, "ship"), (3, "record-return"), (4, "receive-return")])

    def test_history_events_snapshots_and_sequences(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])
        result_receive = self._received("R1", "O1", [{"sku": "C", "quantity": 1}, {"sku": "T", "quantity": 1}])
        result = self.app.cancel_received_return("R1")
        history = self.app.history("O1")
        self.assertTrue(history["complete"])
        events = history["events"]
        self.assertEqual([(e["sequence"], e["action"]) for e in events],
                         [(1, "place"), (2, "ship"), (3, "record-return"),
                          (4, "receive-return"), (5, "cancel-received-return")])
        self.assertEqual(set(events[4]), {"sequence", "action", "result"})
        self.assertEqual(events[4]["result"], result)
        # Old snapshots, including the receive receipt, stay untouched.
        self.assertEqual(events[3]["result"], result_receive)
        t_events = self.app.stock_history("T")["events"]
        self.assertEqual([(e["sequence"], e["action"], e["reference_id"]) for e in t_events], [
            (1, "restock", None), (2, "place", "O1"), (3, "ship", "O1"),
            (4, "receive-return", "R1"), (5, "cancel-received-return", "R1"),
        ])
        last = t_events[-1]
        self.assertEqual(last["before"], {"sku": "T", "on_hand": 9, "reserved": 0, "available": 9})
        self.assertEqual(last["after"], {"sku": "T", "on_hand": 8, "reserved": 0, "available": 8})
        c_events = self.app.stock_history("C")["events"]
        self.assertEqual([(e["action"], e["reference_id"]) for e in c_events[-2:]],
                         [("receive-return", "R1"), ("cancel-received-return", "R1")])

    def test_legacy_data_without_history_starts_at_one_complete_false(self):
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "inventory": {"T": {"on_hand": 5, "reserved": 0}},
            "orders": {"OLD": {
                "order_id": "OLD", "status": "shipped",
                "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200}],
                "total_cents": 200,
                "shipment": {"carrier": "DHL", "tracking_no": "Z"},
            }},
            "returns": {"OLD": [
                {"order_id": "OLD", "return_id": "L1", "lines": [{"sku": "T", "quantity": 2}]},
            ]},
            "return_receipts": {"L1": {
                "order_id": "OLD", "return_id": "L1",
                "lines": [{"sku": "T", "quantity": 2,
                           "before": {"sku": "T", "on_hand": 3, "reserved": 0, "available": 3},
                           "after": {"sku": "T", "on_hand": 5, "reserved": 0, "available": 5}}],
            }},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        result = app.cancel_received_return("L1")
        self.assertEqual(result["lines"][0]["after"]["on_hand"], 3)
        order_history = app.history("OLD")
        self.assertFalse(order_history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in order_history["events"]],
                         [(1, "cancel-received-return")])
        stock_history = app.stock_history("T")
        self.assertFalse(stock_history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in stock_history["events"]],
                         [(1, "cancel-received-return")])

    def test_worklist_progress_summary_and_orders_exclude_reversed_receipt(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self._received("R1", "O1", [{"sku": "T", "quantity": 3}])
        self.app.cancel_received_return("R1")
        cancelled = self.app.return_worklist(stage="cancelled")
        self.assertEqual([item["return_id"] for item in cancelled], ["R1"])
        self.assertEqual(cancelled[0]["stage"], "cancelled")
        self.assertFalse(cancelled[0]["can_receive"])
        self.assertEqual(cancelled[0]["blockers"], [])
        self.assertEqual(self.app.return_worklist(stage="pending"), [])
        self.assertEqual(self.app.return_worklist(stage="received"), [])
        self.assertEqual([i["return_id"] for i in self.app.return_worklist(stage="all")], ["R1"])
        progress = self.app.order_progress("O1")
        (line,) = progress["lines"]
        self.assertEqual({k: line[k] for k in ("shipped", "pending", "received", "remaining", "net")},
                         {"shipped": 5, "pending": 0, "received": 0, "remaining": 5, "net": 5})
        summary = self.app.fulfillment_summary()
        self.assertEqual(summary["order_count"], 1)
        self.assertEqual({k: summary["lines"][0][k] for k in ("shipped", "received", "net", "net_cents")},
                         {"shipped": 5, "received": 0, "net": 5, "net_cents": 500})
        shipment = self.app.shipment_orders("DHL", "TRK-O1")
        self.assertEqual({k: shipment["lines"][0][k] for k in ("received", "remaining", "net")},
                         {"received": 0, "remaining": 5, "net": 5})
        worklist = self.app.order_worklist(stage="all")
        self.assertEqual(worklist[0]["tasks"], ["deliver"])

    def test_persistence_after_reopen(self):
        self._shipped("O1", [{"sku": "T", "quantity": 3}])
        result = self._received("R1", "O1", [{"sku": "T", "quantity": 2}])
        self.app.cancel_received_return("R1")
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.stock("T")["on_hand"], 7)
        self.assertEqual(reopened.get_returns("O1")["records"], [])
        self.assertEqual(reopened.get_return_receipt("R1"), result)
        with self.assertRaises(ValueError):
            reopened.cancel_received_return("R1")
        with self.assertRaises(ValueError):
            reopened.receive_return("R1")
        with self.assertRaises(ValueError):
            reopened.amend_return(
                "R1", [{"sku": "T", "quantity": 2}], [{"sku": "T", "quantity": 1}])
        self.assertEqual(reopened.history("O1")["events"][-1]["action"], "cancel-received-return")

    def test_failed_call_creates_no_directory(self):
        empty_root = Path(self.temp.name) / "empty"
        fresh = OrderDesk(empty_root)
        with self.assertRaises(ValueError):
            fresh.cancel_received_return("missing")
        with self.assertRaises(ValueError):
            fresh.cancel_received_return("   ")
        self.assertFalse(empty_root.exists())

    def test_cli_success_failure_and_array_independent_execution(self):
        self._shipped("O1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.receive_return("R1")
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        self.app.receive_return("R2")
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([
            {"return_id": "R1"},
            {"return_id": "R1"},
        ]), encoding="utf-8")
        failed = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "cancel-received-return", str(batch)],
            text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2, failed.stdout)
        self.assertIn("already cancelled", json.loads(failed.stderr)["error"])
        # The first array row succeeded independently and was saved.
        self.assertEqual(self.app.stock("T")["on_hand"], 8)
        with self.assertRaises(ValueError):
            self.app.cancel_received_return("R1")
        # R2 is still received and can now be reversed on its own.
        query = self.root / "q.json"
        query.write_text(json.dumps({"return_id": "R2"}), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "cancel-received-return", str(query)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        payload = json.loads(ok.stdout)
        self.assertEqual(payload["return_id"], "R2")
        self.assertEqual(payload["lines"][0]["after"]["on_hand"], 7)
        # A pending return fails through the CLI as a standard error object.
        self.app.record_return("O1", "R3", [{"sku": "T", "quantity": 1}])
        query.write_text(json.dumps({"return_id": "R3"}), encoding="utf-8")
        rejected = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "cancel-received-return", str(query)],
            text=True, capture_output=True)
        self.assertEqual(rejected.returncode, 2, rejected.stdout)
        self.assertIn("not been received", json.loads(rejected.stderr)["error"])
        # cancel-return still refuses the received-then-reversed ids too.
        query.write_text(json.dumps({"return_id": "R2"}), encoding="utf-8")
        rejected2 = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "cancel-return", str(query)],
            text=True, capture_output=True)
        self.assertEqual(rejected2.returncode, 2, rejected2.stdout)
        self.assertIn("already cancelled", json.loads(rejected2.stderr)["error"])


if __name__ == "__main__":
    unittest.main()
