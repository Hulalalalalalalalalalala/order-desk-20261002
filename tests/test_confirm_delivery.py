import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk


class ConfirmDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)
        self.app.add_product("U", "Unmanaged", 0)

    def _shipped(self, order_id="O1", lines=None, stock=None):
        for sku, quantity in (stock or {"T": 10, "C": 10}).items():
            self.app.restock(sku, quantity)
        order = self.app.place(order_id, lines or [{"sku": "T", "quantity": 3}])
        shipped = self.app.ship(order_id, " DHL ", " X-1 ")
        return order, shipped

    def test_delivery_confirms_shipped_order_and_preserves_snapshot(self):
        order, shipped = self._shipped()
        result = self.app.confirm_delivery(" O1 ", "  Ada  ", " 2026-03-08 ")
        self.assertEqual(set(result), {"order_id", "status", "lines", "total_cents", "shipment", "delivery"})
        self.assertEqual(result["order_id"], "O1")
        self.assertEqual(result["status"], "delivered")
        self.assertEqual(result["delivery"], {"recipient": "Ada", "delivered_on": "2026-03-08"})
        self.assertEqual(set(result["delivery"]), {"recipient", "delivered_on"})
        # Fulfillment never rewrites the deal or the shipment.
        self.assertEqual(result["lines"], order["lines"])
        self.assertEqual(result["total_cents"], order["total_cents"])
        self.assertEqual(result["shipment"], shipped["shipment"])
        got = self.app.get("O1")
        self.assertEqual(got, result)
        self.assertEqual(got["status"], "delivered")
        self.assertEqual([o["status"] for o in self.app.list_orders()], ["delivered"])
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.get("O1"), result)
        self.assertEqual(reopened.list_orders()[0]["delivery"], result["delivery"])

    def test_confirmation_is_case_sensitive_and_trims_fields(self):
        self._shipped()
        with self.assertRaises(ValueError):
            self.app.confirm_delivery("o1", "Ada", "2026-03-08")
        result = self.app.confirm_delivery("O1", "\t Ada L. \n", "  2024-02-29 ")
        self.assertEqual(result["delivery"], {"recipient": "Ada L.", "delivered_on": "2024-02-29"})

    def test_invalid_dates_are_rejected(self):
        self._shipped()
        bad_dates = (
            None, 7, 20260308, ["2026-03-08"], {"d": "2026-03-08"}, True,
            "", "   ", "2026/03/08", "2026-3-8", "20260308", "08-03-2026",
            "2026-03-8", "2026-13-01", "2026-00-01", "2026-02-30",
            "2023-02-29", "2100-02-29", "2026-03-00", "2026-03-32",
            "2026- 03-08", "2026-03-08x", "abcd-ef-gh", "²⁰²⁶-03-08",
        )
        before = self.app.path.read_bytes()
        for delivered_on in bad_dates:
            with self.assertRaises(ValueError, msg=repr(delivered_on)):
                self.app.confirm_delivery("O1", "Ada", delivered_on)
        # Real calendar dates, including leap day, are accepted only via confirm.
        self.assertEqual(self.app.get("O1")["status"], "shipped")
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_invalid_order_id_and_recipient_are_rejected(self):
        self._shipped()
        for order_id in (None, 123, ["O1"], {"id": "O1"}, "   ", "\t"):
            with self.assertRaises(ValueError, msg=repr(order_id)):
                self.app.confirm_delivery(order_id, "Ada", "2026-03-08")
        for recipient in (None, 9, ["Ada"], {"name": "Ada"}, "   ", "\n"):
            with self.assertRaises(ValueError, msg=repr(recipient)):
                self.app.confirm_delivery("O1", recipient, "2026-03-08")
        self.assertEqual(self.app.get("O1")["status"], "shipped")

    def test_unknown_placed_cancelled_and_delivered_orders_are_rejected(self):
        self.app.restock("T", 10)
        self.app.place("P", [{"sku": "T", "quantity": 1}])
        self.app.place("X", [{"sku": "T", "quantity": 1}])
        self.app.cancel("X")
        self._shipped("O1")
        self.app.confirm_delivery("O1", "Ada", "2026-03-08")
        for order_id in ("missing", "P", "X", "O1"):
            with self.assertRaises(ValueError, msg=order_id):
                self.app.confirm_delivery(order_id, "Ada", "2026-03-08")
        # Repeating with identical content never overwrites the record.
        with self.assertRaises(ValueError):
            self.app.confirm_delivery("O1", "Ada", "2026-03-08")
        with self.assertRaises(ValueError):
            self.app.confirm_delivery("O1", "Bo", "2026-04-01")
        self.assertEqual(self.app.get("O1")["delivery"], {"recipient": "Ada", "delivered_on": "2026-03-08"})

    def test_failed_confirmation_changes_nothing_and_consumes_no_sequence(self):
        self._shipped()
        before = self.app.path.read_bytes()
        for kwargs in (
            {"order_id": "missing", "recipient": "Ada", "delivered_on": "2026-03-08"},
            {"order_id": "O1", "recipient": "Ada", "delivered_on": "2026-02-30"},
            {"order_id": "  ", "recipient": "Ada", "delivered_on": "2026-03-08"},
            {"order_id": "O1", "recipient": "  ", "delivered_on": "2026-03-08"},
        ):
            with self.assertRaises(ValueError):
                self.app.confirm_delivery(**kwargs)
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual([e["action"] for e in self.app.history("O1")["events"]], ["place", "ship"])

    def test_confirmation_touches_no_stock_reservation_or_return_data(self):
        self.app.restock("T", 10)
        self.app.place("O2", [{"sku": "T", "quantity": 2}])  # keeps a live reservation
        self.app.place("O1", [{"sku": "T", "quantity": 3}, {"sku": "U", "quantity": 5}])
        self.app.ship("O1", "DHL", "1")
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        stock_before = self.app.stock("T")
        stock_history_before = self.app.stock_history("T")
        raw_before = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.app.confirm_delivery("O1", "Ada", "2026-03-08")
        raw_after = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(self.app.stock("T"), stock_before)
        self.assertEqual(self.app.stock_history("T"), stock_history_before)
        self.assertEqual(raw_after["inventory"], raw_before["inventory"])
        self.assertEqual(raw_after["reservations"], raw_before["reservations"])
        # No new stock event for the unmanaged sku either.
        self.assertEqual(self.app.stock_history("U")["events"], [])
        self.assertEqual(raw_after["returns"], raw_before["returns"])
        self.assertNotIn("return_receipts", raw_after)

    def test_existing_returns_do_not_block_confirmation(self):
        self._shipped("O1", [{"sku": "T", "quantity": 4}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        result = self.app.confirm_delivery("O1", "Ada", "2026-03-08")
        self.assertEqual(result["status"], "delivered")
        self.assertEqual(self.app.get_returns("O1")["records"][0]["return_id"], "R1")

    def test_return_flows_continue_after_delivery_while_status_stays_delivered(self):
        self._shipped("O1", [{"sku": "T", "quantity": 4}])
        self.app.confirm_delivery("O1", "Ada", "2026-03-08")
        record = self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        self.assertEqual(self.app.get("O1")["status"], "delivered")
        received = self.app.receive_return("R1")
        self.assertEqual(received["lines"][0]["after"]["on_hand"], received["lines"][0]["before"]["on_hand"] + 2)
        self.assertEqual(self.app.get("O1")["status"], "delivered")
        self.assertEqual(self.app.get_return_receipt("R1")["order_id"], "O1")
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        cancelled = self.app.cancel_return("R2")
        self.assertEqual(cancelled["return_id"], "R2")
        self.assertEqual(self.app.get("O1")["status"], "delivered")
        worklist = self.app.return_worklist(stage="all", order_id="O1")
        self.assertEqual([(e["return_id"], e["stage"]) for e in worklist],
                         [("R1", "received"), ("R2", "cancelled")])
        pending = self.app.return_worklist(order_id="O1")
        self.assertEqual(pending, [])
        # Return limits are still enforced against the original order.
        with self.assertRaises(ValueError):
            self.app.record_return("O1", "R3", [{"sku": "T", "quantity": 99}])
        self.assertEqual(self.app.get("O1")["status"], "delivered")

    def test_delivered_order_is_blocked_from_amend_cancel_ship_and_pick_list(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.ship("O1", "DHL", "1")
        self.app.confirm_delivery("O1", "Ada", "2026-03-08")
        with self.assertRaises(ValueError):
            self.app.amend("O1", [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.cancel("O1")
        with self.assertRaises(ValueError):
            self.app.ship("O1", "UPS", "2")
        with self.assertRaises(ValueError):
            self.app.pick_list(["O1"])
        # A batch mixing the delivered order with a healthy placed order is
        # rejected wholesale: the placed order stays placed.
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.ship_batch([
                {"order_id": "O1", "carrier": "UPS", "tracking_no": "2"},
                {"order_id": "O2", "carrier": "UPS", "tracking_no": "3"},
            ])
        self.assertEqual(self.app.get("O2")["status"], "placed")
        self.assertEqual(self.app.get("O1")["status"], "delivered")

    def test_history_appends_confirm_delivery_snapshot_and_keeps_complete(self):
        self._shipped()
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        result = self.app.confirm_delivery("O1", "Ada", "2026-03-08")
        history = self.app.history("O1")
        self.assertEqual(history["status"], "delivered")
        self.assertTrue(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                         [(1, "place"), (2, "ship"), (3, "record-return"), (4, "confirm-delivery")])
        event = history["events"][-1]
        self.assertEqual(set(event), {"sequence", "action", "result"})
        self.assertEqual(event["result"], result)
        # Older snapshots keep their pre-delivery status.
        self.assertEqual(history["events"][1]["result"]["status"], "shipped")
        self.assertNotIn("delivery", history["events"][1]["result"])

    def test_legacy_shipped_order_without_shipment_or_history_can_confirm(self):
        order = {"order_id": "OLD", "status": "shipped",
                 "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200}],
                 "total_cents": 200}
        data = {"products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
                "orders": {"OLD": order}}
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        result = app.confirm_delivery("OLD", "Ada", "2026-03-08")
        self.assertEqual(result["status"], "delivered")
        self.assertNotIn("shipment", result)  # shipping data is never fabricated
        self.assertEqual(result["delivery"], {"recipient": "Ada", "delivered_on": "2026-03-08"})
        history = app.history("OLD")
        self.assertEqual(history["status"], "delivered")
        self.assertFalse(history["complete"])
        self.assertEqual(len(history["events"]), 1)
        self.assertEqual(history["events"][0]["sequence"], 1)
        self.assertEqual(history["events"][0]["action"], "confirm-delivery")
        self.assertEqual(history["events"][0]["result"], result)
        # Persisted and identical after reopening.
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.get("OLD"), result)
        self.assertEqual(reopened.history("OLD"), history)

    def test_legacy_orders_with_shipment_but_no_history_keep_it(self):
        order = {"order_id": "OLD", "status": "shipped",
                 "lines": [{"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100}],
                 "total_cents": 100,
                 "shipment": {"carrier": "DHL", "tracking_no": "Z-9"}}
        data = {"products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
                "orders": {"OLD": order}}
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        result = OrderDesk(self.root).confirm_delivery("OLD", "Ada", "2026-03-08")
        self.assertEqual(result["shipment"], {"carrier": "DHL", "tracking_no": "Z-9"})

    def test_cli_confirm_delivery_success_failure_and_array(self):
        self._shipped("A")
        self._shipped("B", [{"sku": "C", "quantity": 1}])
        payload = self.root / "confirm.json"
        payload.write_text(json.dumps(
            {"order_id": " A ", "recipient": " Ada ", "delivered_on": " 2026-03-08 "}), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "confirm-delivery", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual(result["status"], "delivered")
        self.assertEqual(result["delivery"], {"recipient": "Ada", "delivered_on": "2026-03-08"})
        # Repeating fails with exit 2 and changes nothing.
        failed = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "confirm-delivery", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2)
        self.assertIn("error", json.loads(failed.stderr))
        # A bad date is rejected.
        payload.write_text(json.dumps({"order_id": "B", "recipient": "Bo", "delivered_on": "2026-02-30"}),
                           encoding="utf-8")
        bad = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "confirm-delivery", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(bad.returncode, 2, bad.stdout)
        self.assertEqual(OrderDesk(self.root).get("B")["status"], "shipped")
        # Array input runs rows independently; an earlier success is retained.
        payload.write_text(json.dumps([
            {"order_id": "B", "recipient": "Bo", "delivered_on": "2026-03-09"},
            {"order_id": "missing", "recipient": "X", "delivered_on": "2026-03-09"},
        ]), encoding="utf-8")
        stopped = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "confirm-delivery", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(stopped.returncode, 2, stopped.stdout)
        self.assertEqual(OrderDesk(self.root).get("B")["status"], "delivered")
        # history and list show the new status and preserve delivery.
        h = self.root / "h.json"
        h.write_text(json.dumps({"order_id": "A"}), encoding="utf-8")
        history_run = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "history", str(h)],
            text=True, capture_output=True)
        self.assertEqual(history_run.returncode, 0, history_run.stderr)
        self.assertEqual(json.loads(history_run.stdout)["status"], "delivered")
        listed = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "list"],
            text=True, capture_output=True)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual([o["order_id"] for o in json.loads(listed.stdout)], ["A", "B"])
        self.assertTrue(all(o["status"] == "delivered" for o in json.loads(listed.stdout)))


if __name__ == "__main__":
    unittest.main()
