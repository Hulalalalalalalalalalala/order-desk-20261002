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

    def _received(self, order_id, return_id, lines, stock=None):
        self._shipped(order_id, lines, stock)
        self.app.record_return(order_id, return_id, lines)
        return self.app.receive_return(return_id)

    def test_reversal_subtracts_receipt_quantities_from_current_stock(self):
        self._received("O1", "R1", [{"sku": "T", "quantity": 4}])
        # 10 restocked - 4 shipped + 4 received = 10 on hand.
        self.assertEqual(self.app.stock("T")["on_hand"], 10)
        result = self.app.cancel_received_return("R1")
        self.assertEqual(result, {
            "order_id": "O1",
            "return_id": "R1",
            "lines": [{
                "sku": "T",
                "quantity": 4,
                "before": {"sku": "T", "on_hand": 10, "reserved": 0, "available": 10},
                "after": {"sku": "T", "on_hand": 6, "reserved": 0, "available": 6},
            }],
        })
        self.assertEqual(set(result), {"order_id", "return_id", "lines"})
        self.assertEqual(set(result["lines"][0]), {"sku", "quantity", "before", "after"})
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 6, "reserved": 0, "available": 6})

    def test_lines_merged_sorted_and_later_changes_preserved(self):
        self._received("O1", "R1", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])
        # Later restock and count changes survive: only the receipt quantity
        # is subtracted from the current on_hand.
        self.app.restock("T", 5)
        self.app.count_stock("c1", [{"sku": "C", "on_hand": 12}])
        result = self.app.cancel_received_return("R1")
        self.assertEqual([line["sku"] for line in result["lines"]], ["C", "T"])
        self.assertEqual(result["lines"][0]["before"]["on_hand"], 12)
        self.assertEqual(result["lines"][0]["after"]["on_hand"], 11)
        self.assertEqual(result["lines"][1]["before"]["on_hand"], 15)
        self.assertEqual(result["lines"][1]["after"]["on_hand"], 13)
        self.assertEqual(self.app.stock("T")["on_hand"], 13)
        self.assertEqual(self.app.stock("C")["on_hand"], 11)

    def test_reservations_and_other_orders_untouched(self):
        self._received("O1", "R1", [{"sku": "T", "quantity": 2}])
        self.app.place("O2", [{"sku": "T", "quantity": 3}])
        before = self.app.stock("T")
        result = self.app.cancel_received_return("R1")
        self.assertEqual(result["lines"][0]["before"]["reserved"], 3)
        self.assertEqual(result["lines"][0]["after"]["reserved"], 3)
        self.assertEqual(self.app.stock("T"), {
            "sku": "T", "on_hand": before["on_hand"] - 2,
            "reserved": 3, "available": before["available"] - 2,
        })
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["reservations"]["O2"], {"T": 3})

    def test_negative_or_below_reserved_on_hand_rejects_whole_reversal(self):
        self._received("O1", "R1", [{"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 1}])
        # Drain T below the receipt quantity via a stock count.
        self.app.count_stock("c1", [{"sku": "T", "on_hand": 2}])
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.cancel_received_return("R1")
        self.assertEqual(self.app.path.read_bytes(), before)
        # C must not be deducted either; the return stays active and received.
        self.assertEqual(self.app.stock("C")["on_hand"], 10)
        self.assertEqual(self.app.return_worklist("received")[0]["return_id"], "R1")
        # Below current reserved quantity also rejects.
        self.app.count_stock("c2", [{"sku": "T", "on_hand": 9}])
        self.app.place("O2", [{"sku": "T", "quantity": 8}])
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.cancel_received_return("R1")
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_unknown_or_unmanaged_product_rejects(self):
        self._received("O1", "R1", [{"sku": "T", "quantity": 1}])
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        del raw["products"]["T"]
        self.app.path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.app.cancel_received_return("R1")
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        raw["products"]["T"] = {"sku": "T", "name": "Tea", "price_cents": 100}
        del raw["inventory"]["T"]
        self.app.path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.app.cancel_received_return("R1")

    def test_invalid_and_unknown_identifiers(self):
        self._received("O1", "R1", [{"sku": "T", "quantity": 1}])
        for bad in (None, 123, 1.5, b"R1", ["R1"], {"x": 1}, "   ", "\t\n", True):
            with self.assertRaises(ValueError):
                self.app.cancel_received_return(bad)
        with self.assertRaises(ValueError):
            self.app.cancel_received_return("nope")
        # Ids are trimmed and case sensitive.
        self.assertEqual(self.app.cancel_received_return("  R1  ")["return_id"], "R1")
        self._received("O2", "Ra", [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.cancel_received_return("ra")

    def test_not_received_pending_or_cancelled_return_rejected(self):
        self._shipped("O1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        self.app.cancel_return("R2")
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.cancel_received_return("R1")
        with self.assertRaises(ValueError):
            self.app.cancel_received_return("R2")
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_repeat_reversal_rejected_without_double_subtract(self):
        self._received("O1", "R1", [{"sku": "T", "quantity": 2}])
        first = self.app.cancel_received_return("R1")
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.cancel_received_return("R1")
        with self.assertRaises(ValueError):
            self.app.cancel_received_return("  R1  ")
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.stock("T")["on_hand"], first["lines"][0]["after"]["on_hand"])

    def test_order_missing_or_not_shipped_rejected(self):
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "inventory": {"T": {"on_hand": 5, "reserved": 0}},
            "orders": {"OLD": {
                "order_id": "OLD", "status": "placed",
                "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200}],
                "total_cents": 200,
            }},
            "returns": {
                "OLD": [{"order_id": "OLD", "return_id": "RP", "lines": [{"sku": "T", "quantity": 1}]}],
                "GONE": [{"order_id": "GONE", "return_id": "RG", "lines": [{"sku": "T", "quantity": 1}]}],
            },
            "return_receipts": {
                "RP": {"order_id": "OLD", "return_id": "RP", "lines": [
                    {"sku": "T", "quantity": 1,
                     "before": {"sku": "T", "on_hand": 4, "reserved": 0, "available": 4},
                     "after": {"sku": "T", "on_hand": 5, "reserved": 0, "available": 5}}]},
                "RG": {"order_id": "GONE", "return_id": "RG", "lines": [
                    {"sku": "T", "quantity": 1,
                     "before": {"sku": "T", "on_hand": 4, "reserved": 0, "available": 4},
                     "after": {"sku": "T", "on_hand": 5, "reserved": 0, "available": 5}}]},
            },
        }
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        with self.assertRaises(ValueError):
            app.cancel_received_return("RP")
        with self.assertRaises(ValueError):
            app.cancel_received_return("RG")
        self.assertEqual(app.stock("T")["on_hand"], 5)

    def test_legacy_registration_without_receipt_treated_as_not_received(self):
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
            app.cancel_received_return("L1")
        # It can still be received and then reversed; legacy history starts at
        # 1 with complete=False and is never backfilled.
        app.receive_return("L1")
        result = app.cancel_received_return("L1")
        history = app.history("OLD")
        self.assertFalse(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                         [(1, "receive-return"), (2, "cancel-received-return")])
        self.assertEqual(history["events"][1]["result"], result)
        self.assertEqual(app.stock("T")["on_hand"], 3)

    def test_return_becomes_cancelled_and_id_stays_occupied(self):
        self._received("O1", "R1", [{"sku": "T", "quantity": 2}])
        self.app.cancel_received_return("R1")
        # The id can never be registered, amended, received or cancelled again.
        with self.assertRaises(ValueError):
            self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.amend_return("R1", [{"sku": "T", "quantity": 2}], [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.receive_return("R1")
        with self.assertRaises(ValueError):
            self.app.cancel_return("R1")
        # Worklist shows it as cancelled; returns and quote-return freed the
        # allowance; the original receipt snapshot is still queryable.
        stages = {e["return_id"]: e["stage"] for e in self.app.return_worklist("all")}
        self.assertEqual(stages["R1"], "cancelled")
        self.assertEqual(self.app.get_returns("O1")["remaining"], [{"sku": "T", "quantity": 2}])
        quote = self.app.quote_return("O1", [{"sku": "T", "quantity": 2}])
        self.assertTrue(quote["can_record"])
        receipt = self.app.get_return_receipt("R1")
        self.assertEqual(receipt["lines"][0]["after"]["on_hand"], 10)

    def test_fulfillment_views_exclude_the_reversed_receipt(self):
        self._received("O1", "R1", [{"sku": "T", "quantity": 3}])
        progress = self.app.order_progress("O1")["lines"][0]
        self.assertEqual((progress["received"], progress["net"]), (3, 0))
        summary_before = self.app.fulfillment_summary()["lines"][0]
        self.assertEqual((summary_before["received"], summary_before["net"]), (3, 0))
        self.app.cancel_received_return("R1")
        progress = self.app.order_progress("O1")["lines"][0]
        self.assertEqual((progress["received"], progress["pending"], progress["net"]), (0, 0, 3))
        self.assertEqual(progress["remaining"], 3)
        summary = self.app.fulfillment_summary()["lines"][0]
        self.assertEqual((summary["received"], summary["net"], summary["net_cents"]), (0, 3, 300))
        shipment = self.app.shipment_orders("DHL", "TRK-O1")["lines"][0]
        self.assertEqual((shipment["received"], shipment["net"]), (0, 3))
        # No new receive-return task appears on the worklist.
        tasks = {e["order_id"]: e["tasks"] for e in self.app.order_worklist("all")}
        self.assertNotIn("receive-return", tasks["O1"])

    def test_order_and_deal_records_remain_unchanged(self):
        self._received("O1", "R1", [{"sku": "T", "quantity": 2}])
        self.app.confirm_delivery("O1", "Ann", "2026-10-01")
        order_before = self.app.get("O1")
        self.app.cancel_received_return("R1")
        order_after = self.app.get("O1")
        self.assertEqual(order_after, order_before)
        self.assertEqual(order_after["status"], "delivered")
        self.assertEqual(order_after["delivery"], {"recipient": "Ann", "delivered_on": "2026-10-01"})
        self.assertEqual(order_after["total_cents"], 200)

    def test_paused_product_does_not_block(self):
        self._received("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.set_product_enabled("T", False)
        result = self.app.cancel_received_return("R1")
        self.assertEqual(result["lines"][0]["after"]["on_hand"], 9)

    def test_history_and_stock_events_recorded_with_sequences(self):
        self._received("O1", "R1", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])
        result = self.app.cancel_received_return("R1")
        history = self.app.history("O1")
        self.assertTrue(history["complete"])
        events = history["events"]
        self.assertEqual([(e["sequence"], e["action"]) for e in events], [
            (1, "place"), (2, "ship"), (3, "record-return"),
            (4, "receive-return"), (5, "cancel-received-return"),
        ])
        self.assertEqual(events[4]["result"], result)
        for sku, quantity in (("C", 1), ("T", 2)):
            stock_events = self.app.stock_history(sku)["events"]
            last = stock_events[-1]
            self.assertEqual(last["action"], "cancel-received-return")
            self.assertEqual(last["reference_id"], "R1")
            self.assertEqual(last["sequence"], len(stock_events))
            self.assertEqual(last["after"]["on_hand"], last["before"]["on_hand"] - quantity)
        # Reopening the same root gives identical results.
        again = OrderDesk(self.root)
        self.assertEqual(again.history("O1"), history)
        self.assertEqual(again.stock("T"), self.app.stock("T"))

    def test_failures_add_no_history_and_consume_no_sequence(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}, {"sku": "U", "quantity": 1}])
        self.app.ship("O1", "DHL", "X")
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}, {"sku": "U", "quantity": 1}])
        self.app.restock("U", 1)
        self.app.receive_return("R1")
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        del raw["inventory"]["U"]
        self.app.path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.app.cancel_received_return("R1")
        with self.assertRaises(ValueError):
            self.app.cancel_received_return("nope")
        events = self.app.history("O1")["events"]
        self.assertEqual([(e["sequence"], e["action"]) for e in events],
                         [(1, "place"), (2, "ship"), (3, "record-return"), (4, "receive-return")])

    def test_cli_success_failure_and_array_independence(self):
        self._received("O1", "R1", [{"sku": "T", "quantity": 1}])
        self._received("O2", "R2", [{"sku": "T", "quantity": 1}])
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([
            {"return_id": "R1"},
            {"return_id": "R1"},
            {"return_id": "R2"},
        ]), encoding="utf-8")
        run = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "cancel-received-return", str(batch)],
            text=True, capture_output=True)
        self.assertEqual(run.returncode, 2, run.stdout)
        self.assertIn("already cancelled", json.loads(run.stderr)["error"])
        # The outer array ran each row independently: R1's reversal persisted,
        # the failing repeat stopped the batch and R2 was never attempted.
        self.assertEqual(self.app.stock("T")["on_hand"], 19)
        stages = {e["return_id"]: e["stage"] for e in self.app.return_worklist("all")}
        self.assertEqual(stages, {"R1": "cancelled", "R2": "received"})
        # R2 can still be reversed on its own.
        query = self.root / "q.json"
        query.write_text(json.dumps({"return_id": "R2"}), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "cancel-received-return", str(query)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(self.app.stock("T")["on_hand"], 18)
        # A single successful call prints the receipt JSON and exits zero.
        self._received("O3", "R3", [{"sku": "T", "quantity": 1}])
        query.write_text(json.dumps({"return_id": " R3 "}), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "cancel-received-return", str(query)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual(result["return_id"], "R3")
        self.assertEqual(result["lines"][0]["quantity"], 1)


if __name__ == "__main__":
    unittest.main()
