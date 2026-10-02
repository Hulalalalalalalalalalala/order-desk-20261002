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

    def _shipped(self, order_id="O1", lines=None):
        self.app.restock("T", 10)
        lines = lines if lines is not None else [{"sku": "T", "quantity": 3}]
        self.app.place(order_id, lines)
        return self.app.ship(order_id, " DHL ", " X-1 ")

    def test_confirm_returns_full_order_with_delivery(self):
        shipped = self._shipped()
        delivered = self.app.confirm_delivery(" O1 ", "  Wang Wu ", " 2026-03-01 ")
        self.assertEqual(delivered["status"], "delivered")
        self.assertEqual(delivered["delivery"], {"recipient": "Wang Wu", "delivered_on": "2026-03-01"})
        self.assertEqual(set(delivered),
                         {"order_id", "status", "lines", "total_cents", "shipment", "delivery"})
        # Everything else from the shipped snapshot is untouched.
        self.assertEqual(delivered["lines"], shipped["lines"])
        self.assertEqual(delivered["total_cents"], shipped["total_cents"])
        self.assertEqual(delivered["shipment"], {"carrier": "DHL", "tracking_no": "X-1"})
        # get, list and history expose the new status and keep delivery.
        self.assertEqual(self.app.get("O1"), delivered)
        self.assertEqual(self.app.list_orders(), [delivered])
        history = self.app.history("O1")
        self.assertEqual(history["status"], "delivered")
        self.assertEqual(history["events"][-1]["result"], delivered)
        # Persisted together with the history event.
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.get("O1"), delivered)
        self.assertEqual(reopened.history("O1")["status"], "delivered")

    def test_history_event_continues_sequence_and_keeps_complete(self):
        self._shipped()
        delivered = self.app.confirm_delivery("O1", "Wang", "2026-03-01")
        history = self.app.history("O1")
        self.assertTrue(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                         [(1, "place"), (2, "ship"), (3, "confirm-delivery")])
        event = history["events"][2]
        self.assertEqual(set(event), {"sequence", "action", "result"})
        self.assertEqual(event["result"], delivered)
        # Older snapshots are not rewritten; the ship snapshot stays shipped.
        self.assertEqual(history["events"][1]["result"]["status"], "shipped")
        self.assertNotIn("delivery", history["events"][1]["result"])

    def test_confirmation_adds_no_stock_event_and_changes_no_inventory(self):
        self._shipped()
        before_stock = self.app.stock("T")
        before_stock_events = self.app.stock_history("T")["events"]
        raw_before = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.app.confirm_delivery("O1", "Wang", "2026-03-01")
        self.assertEqual(self.app.stock("T"), before_stock)
        self.assertEqual(self.app.stock_history("T")["events"], before_stock_events)
        raw_after = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(raw_after["inventory"], raw_before["inventory"])
        self.assertEqual(raw_after["reservations"], raw_before.get("reservations", {}))

    def test_invalid_inputs_are_rejected_without_consuming_sequence(self):
        self._shipped()
        before = self.app.path.read_bytes()
        bad_dates = (
            None, 20260301, 2026.0, b"2026-03-01", ["2026-03-01"], {"x": 1},
            "", "   ", "\t\n",
            "2026/03/01", "2026-3-1", "03-01-2026", "20260301",
            "2026-13-01", "2026-00-15", "2026-10-32", "2026-02-29",
            "2026-03-01T00:00:00",
        )
        for bad in bad_dates:
            with self.assertRaises(ValueError):
                self.app.confirm_delivery("O1", "Wang", bad)
        for bad in (None, 123, b"O1", ["O1"], {"x": 1}, "   ", "\t"):
            with self.assertRaises(ValueError):
                self.app.confirm_delivery(bad, "Wang", "2026-03-01")
        for bad in (None, 123, b"Wang", ["Wang"], {"x": 1}, "   ", "\t"):
            with self.assertRaises(ValueError):
                self.app.confirm_delivery("O1", bad, "2026-03-01")
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.get("O1")["status"], "shipped")
        self.assertEqual([(e["sequence"], e["action"]) for e in self.app.history("O1")["events"]],
                         [(1, "place"), (2, "ship")])

    def test_valid_leap_day_is_accepted(self):
        self._shipped()
        delivered = self.app.confirm_delivery("O1", "Wang", "2024-02-29")
        self.assertEqual(delivered["delivery"]["delivered_on"], "2024-02-29")

    def test_unknown_unshipped_and_repeated_confirmation_rejected(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        self.app.cancel("O2")
        self.app.ship("O1", "DHL", "1")
        with self.assertRaises(ValueError):
            self.app.confirm_delivery("missing", "Wang", "2026-03-01")
        with self.assertRaises(ValueError):
            self.app.confirm_delivery("o1", "Wang", "2026-03-01")  # case sensitive
        # Placed and cancelled orders cannot be signed for.
        self.app.place("O3", [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.confirm_delivery("O3", "Wang", "2026-03-01")
        with self.assertRaises(ValueError):
            self.app.confirm_delivery("O2", "Wang", "2026-03-01")
        self.app.confirm_delivery("O1", "Wang", "2026-03-01")
        snapshot = self.app.path.read_bytes()
        # Repeating, even with identical content, is rejected and overwrites nothing.
        for args in (("O1", "Wang", "2026-03-01"), ("O1", "Li", "2026-03-02")):
            with self.assertRaises(ValueError):
                self.app.confirm_delivery(*args)
        self.assertEqual(self.app.path.read_bytes(), snapshot)
        self.assertEqual(self.app.get("O1")["delivery"],
                         {"recipient": "Wang", "delivered_on": "2026-03-01"})

    def test_existing_returns_do_not_block_confirmation(self):
        self._shipped(lines=[{"sku": "T", "quantity": 4}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        delivered = self.app.confirm_delivery("O1", "Wang", "2026-03-01")
        self.assertEqual(delivered["status"], "delivered")
        records = self.app.get_returns("O1")
        self.assertEqual([r["return_id"] for r in records["records"]], ["R1"])
        self.assertEqual(records["remaining"][0]["quantity"], 3)

    def test_return_flow_after_delivery_keeps_delivered_status(self):
        self._shipped(lines=[{"sku": "T", "quantity": 4}])
        self.app.confirm_delivery("O1", "Wang", "2026-03-01")
        # Register two partial returns after delivery.
        record = self.app.record_return(" O1 ", "R2", [{"sku": "T", "quantity": 1}])
        self.assertEqual(record["order_id"], "O1")
        self.assertEqual(self.app.get("O1")["status"], "delivered")
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        # Receiving one return adds stock back but leaves the order delivered.
        on_hand_before = self.app.stock("T")["on_hand"]
        receipt = self.app.receive_return("R1")
        self.assertEqual(receipt["lines"][0]["before"]["on_hand"], on_hand_before)
        self.assertEqual(self.app.stock("T")["on_hand"], on_hand_before + 1)
        self.assertEqual(self.app.get("O1")["status"], "delivered")
        self.assertEqual(self.app.get_return_receipt("R1")["return_id"], "R1")
        # The other registration can still be cancelled; its id stays occupied.
        cancelled = self.app.cancel_return("R2")
        self.assertEqual(cancelled["return_id"], "R2")
        self.assertEqual(self.app.get("O1")["status"], "delivered")
        # The cancelled return id stays occupied even on the same order.
        with self.assertRaises(ValueError):
            self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        # The worklist shows delivered-order registrations under the same rules.
        pending = self.app.return_worklist()
        self.assertEqual(pending, [])
        received = self.app.return_worklist(stage="all")
        self.assertEqual({e["return_id"]: e["stage"] for e in received}, {"R1": "received", "R2": "cancelled"})
        entries = self.app.return_worklist(stage="all", order_id=" O1 ")
        self.assertEqual([e["return_id"] for e in entries], ["R1", "R2"])
        # All new events follow the delivery event and status stays delivered.
        history = self.app.history("O1")
        self.assertEqual(history["status"], "delivered")
        self.assertEqual(
            [e["action"] for e in history["events"]],
            ["place", "ship", "confirm-delivery", "record-return", "record-return",
             "receive-return", "cancel-return"],
        )
        self.assertEqual([e["sequence"] for e in history["events"]], [1, 2, 3, 4, 5, 6, 7])

    def test_delivered_order_is_locked_from_amend_cancel_ship_pick_and_batch(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        self.app.ship("O1", "DHL", "1")
        self.app.confirm_delivery("O1", "Wang", "2026-03-01")
        with self.assertRaises(ValueError):
            self.app.amend("O1", [{"sku": "T", "quantity": 2}])
        with self.assertRaises(ValueError):
            self.app.cancel("O1")
        with self.assertRaises(ValueError):
            self.app.ship("O1", "UPS", "9")
        with self.assertRaises(ValueError):
            self.app.pick_list(["O1"])
        # A batch containing a delivered order is rejected wholesale; O2 stays placed.
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.ship_batch([
                {"order_id": "O2", "carrier": "DHL", "tracking_no": "2"},
                {"order_id": "O1", "carrier": "UPS", "tracking_no": "9"},
            ])
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.get("O2")["status"], "placed")
        # A pick list mixing placed and delivered orders is rejected as a whole.
        with self.assertRaises(ValueError):
            self.app.pick_list(["O2", "O1"])
        self.assertEqual(self.app.pick_list(["O2"])["lines"][0]["quantity"], 1)

    def test_legacy_shipped_order_without_shipment_or_history_can_confirm(self):
        order = {"order_id": "OLD", "status": "shipped",
                 "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200}],
                 "total_cents": 200}
        data = {"products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
                "inventory": {"T": {"on_hand": 0, "reserved": 0}},
                "orders": {"OLD": order}}
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        delivered = app.confirm_delivery("OLD", "Wang", "2026-03-01")
        self.assertEqual(delivered["status"], "delivered")
        self.assertNotIn("shipment", delivered)  # no shipping info is fabricated
        self.assertEqual(delivered["delivery"], {"recipient": "Wang", "delivered_on": "2026-03-01"})
        history = app.history("OLD")
        self.assertFalse(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                         [(1, "confirm-delivery")])
        self.assertEqual(history["events"][0]["result"], delivered)
        reopened_history = OrderDesk(self.root).history("OLD")
        self.assertEqual(reopened_history["status"], "delivered")
        self.assertEqual(reopened_history["events"], history["events"])

    def test_cli_confirm_success_failure_and_array_partial_failure(self):
        self.app.restock("T", 10)
        self.app.place("A", [{"sku": "T", "quantity": 1}])
        self.app.place("B", [{"sku": "T", "quantity": 1}])
        self.app.ship("A", "DHL", "A-1")
        self.app.ship("B", "DHL", "B-1")
        payload = self.root / "confirm.json"
        payload.write_text(json.dumps(
            {"order_id": " A ", "recipient": " Wang ", "delivered_on": " 2026-03-01 "}), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "confirm-delivery", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual(result["status"], "delivered")
        self.assertEqual(result["delivery"], {"recipient": "Wang", "delivered_on": "2026-03-01"})
        # Repeating fails with exit 2.
        failed = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "confirm-delivery", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2)
        self.assertIn("error", json.loads(failed.stderr))
        # A bad date is rejected with exit 2.
        payload.write_text(json.dumps(
            {"order_id": "B", "recipient": "Li", "delivered_on": "2026-02-29"}), encoding="utf-8")
        bad_date = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "confirm-delivery", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(bad_date.returncode, 2, bad_date.stdout)
        # Array input runs rows independently: B succeeds, the unknown id fails afterwards.
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([
            {"order_id": "B", "recipient": "Li", "delivered_on": "2026-03-02"},
            {"order_id": "missing", "recipient": "Li", "delivered_on": "2026-03-02"},
        ]), encoding="utf-8")
        stopped = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "confirm-delivery", str(batch)],
            text=True, capture_output=True)
        self.assertEqual(stopped.returncode, 2, stopped.stdout)
        self.assertEqual(OrderDesk(self.root).get("B")["status"], "delivered")
        self.assertEqual(OrderDesk(self.root).get("A")["status"], "delivered")


if __name__ == "__main__":
    unittest.main()
