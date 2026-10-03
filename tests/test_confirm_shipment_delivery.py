import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk

class ConfirmShipmentDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)

    def _ship(self, order_id, carrier="DHL", tracking_no="X-1", lines=None):
        self.app.restock("T", 10)
        lines = lines if lines is not None else [{"sku": "T", "quantity": 1}]
        self.app.place(order_id, lines)
        self.app.ship(order_id, carrier, tracking_no)

    def test_bulk_confirm_signs_every_matching_shipped_order(self):
        self._ship("O2")
        self._ship("O1", lines=[{"sku": "T", "quantity": 2}])
        self._ship("O3", carrier="UPS", tracking_no="U-9")  # different shipment
        result = self.app.confirm_shipment_delivery(" DHL ", " X-1 ", " Wang Wu ", " 2026-03-01 ")
        self.assertEqual(set(result), {"carrier", "tracking_no", "orders"})
        self.assertEqual(result["carrier"], "DHL")
        self.assertEqual(result["tracking_no"], "X-1")
        # Orders come back sorted by order id, each the full get() structure.
        self.assertEqual([o["order_id"] for o in result["orders"]], ["O1", "O2"])
        for order in result["orders"]:
            self.assertEqual(order["status"], "delivered")
            self.assertEqual(order["delivery"],
                             {"recipient": "Wang Wu", "delivered_on": "2026-03-01"})
            self.assertEqual(order["shipment"], {"carrier": "DHL", "tracking_no": "X-1"})
            self.assertEqual(order, self.app.get(order["order_id"]))
        # The non-matching shipment is untouched.
        self.assertEqual(self.app.get("O3")["status"], "shipped")
        self.assertNotIn("delivery", self.app.get("O3"))
        # Persisted: a reopened desk sees the new state in get and history.
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.get("O1")["status"], "delivered")
        self.assertEqual(reopened.history("O1")["events"][-1]["action"], "confirm-delivery")
        # The fulfillment worklist no longer asks to deliver O1/O2.
        deliver = {e["order_id"] for e in reopened.order_worklist(stage="deliver")}
        self.assertEqual(deliver, {"O3"})

    def test_history_events_continue_sequence_and_keep_snapshots(self):
        self._ship("O1")
        self._ship("O2")
        result = self.app.confirm_shipment_delivery("DHL", "X-1", "Wang", "2026-03-01")
        for order in result["orders"]:
            history = self.app.history(order["order_id"])
            self.assertTrue(history["complete"])
            self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                             [(1, "place"), (2, "ship"), (3, "confirm-delivery")])
            event = history["events"][2]
            self.assertEqual(set(event), {"sequence", "action", "result"})
            self.assertEqual(event["result"], order)
            # Older snapshots are not rewritten.
            self.assertEqual(history["events"][1]["result"]["status"], "shipped")

    def test_repeat_with_same_info_succeeds_without_writing(self):
        self._ship("O1")
        self._ship("O2")
        first = self.app.confirm_shipment_delivery("DHL", "X-1", "Wang", "2026-03-01")
        snapshot = self.app.path.read_bytes()
        again = self.app.confirm_shipment_delivery("DHL", "X-1", "Wang", "2026-03-01")
        self.assertEqual(again, first)
        self.assertEqual(self.app.path.read_bytes(), snapshot)
        self.assertEqual(len(self.app.history("O1")["events"]), 3)
        self.assertEqual(len(self.app.history("O2")["events"]), 3)

    def test_mixed_state_signs_only_the_pending_orders(self):
        self._ship("O1")
        self._ship("O2")
        self.app.confirm_delivery("O1", "Wang", "2026-03-01")
        snapshot_o1_events = self.app.history("O1")["events"]
        result = self.app.confirm_shipment_delivery("DHL", "X-1", "Wang", "2026-03-01")
        self.assertEqual([o["order_id"] for o in result["orders"]], ["O1", "O2"])
        # O1 kept its original record and gained no new event.
        self.assertEqual(self.app.history("O1")["events"], snapshot_o1_events)
        # O2 was signed by the bulk request.
        o2_events = self.app.history("O2")["events"]
        self.assertEqual(o2_events[-1]["action"], "confirm-delivery")
        self.assertEqual(o2_events[-1]["result"]["delivery"],
                         {"recipient": "Wang", "delivered_on": "2026-03-01"})

    def test_conflicting_or_malformed_stored_delivery_rejects_everything(self):
        bad_deliveries = (
            {"recipient": "Li", "delivered_on": "2026-03-01"},      # different recipient
            {"recipient": "Wang", "delivered_on": "2026-03-02"},    # different date
            "Wang",                                                  # not an object
            {"recipient": "Wang"},                                   # missing date
            {"delivered_on": "2026-03-01"},                          # missing recipient
            {"recipient": "  ", "delivered_on": "2026-03-01"},       # blank text
            {"recipient": "Wang", "delivered_on": "2026-02-29"},     # invalid date
        )
        for bad in bad_deliveries:
            with self.subTest(bad=bad):
                self._ship("O1")
                self._ship("O2")
                self.app.confirm_delivery("O1", "Wang", "2026-03-01")
                # Corrupt the stored delivery directly.
                data = json.loads(self.app.path.read_text(encoding="utf-8"))
                data["orders"]["O1"]["delivery"] = bad
                self.app.path.write_text(json.dumps(data), encoding="utf-8")
                before = self.app.path.read_bytes()
                with self.assertRaises(ValueError):
                    self.app.confirm_shipment_delivery("DHL", "X-1", "Wang", "2026-03-01")
                self.assertEqual(self.app.path.read_bytes(), before)
                self.assertEqual(OrderDesk(self.root).get("O2")["status"], "shipped")
                self.temp.cleanup()
                self.setUp()

    def test_no_match_and_invalid_inputs_rejected_without_side_effects(self):
        empty = self.root / "empty"
        app = OrderDesk(empty)
        with self.assertRaises(ValueError):
            app.confirm_shipment_delivery("DHL", "X-1", "Wang", "2026-03-01")
        self.assertFalse(empty.exists())
        self._ship("O1")
        # A placed order under the combination does not count as a match.
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.confirm_shipment_delivery("UPS", "U-9", "Wang", "2026-03-01")
        for bad in (None, 123, b"DHL", ["DHL"], {"x": 1}, "", "   ", "\t"):
            with self.assertRaises(ValueError):
                self.app.confirm_shipment_delivery(bad, "X-1", "Wang", "2026-03-01")
            with self.assertRaises(ValueError):
                self.app.confirm_shipment_delivery("DHL", bad, "Wang", "2026-03-01")
            with self.assertRaises(ValueError):
                self.app.confirm_shipment_delivery("DHL", "X-1", bad, "2026-03-01")
        for bad_date in (None, 20260301, "", "  ", "2026/03/01", "2026-3-1",
                         "2026-13-01", "2026-02-29", "2026-03-01T00:00:00"):
            with self.assertRaises(ValueError):
                self.app.confirm_shipment_delivery("DHL", "X-1", "Wang", bad_date)
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.get("O1")["status"], "shipped")
        self.assertEqual(len(self.app.history("O1")["events"]), 2)

    def test_valid_leap_day_is_accepted(self):
        self._ship("O1")
        result = self.app.confirm_shipment_delivery("DHL", "X-1", "Wang", "2024-02-29")
        self.assertEqual(result["orders"][0]["delivery"]["delivered_on"], "2024-02-29")

    def test_matches_current_shipment_only_not_history(self):
        self._ship("O1")
        self._ship("O2", carrier="UPS", tracking_no="U-9")
        # O2 used to ship under DHL/X-1 before a correction.
        self.app.correct_shipment("O2", {"carrier": "UPS", "tracking_no": "U-9"},
                                  {"carrier": "DHL", "tracking_no": "X-1"})
        self.app.correct_shipment("O1", {"carrier": "DHL", "tracking_no": "X-1"},
                                  {"carrier": "DHL", "tracking_no": "X-2"})
        result = self.app.confirm_shipment_delivery("DHL", "X-1", "Wang", "2026-03-01")
        self.assertEqual([o["order_id"] for o in result["orders"]], ["O2"])
        self.assertEqual(self.app.get("O1")["status"], "shipped")

    def test_returns_do_not_block_and_no_stock_events_are_added(self):
        self._ship("O1", lines=[{"sku": "T", "quantity": 4}])
        self._ship("O2")
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        before_stock = self.app.stock("T")
        before_stock_events = self.app.stock_history("T")["events"]
        raw_before = json.loads(self.app.path.read_text(encoding="utf-8"))
        result = self.app.confirm_shipment_delivery("DHL", "X-1", "Wang", "2026-03-01")
        self.assertEqual([o["order_id"] for o in result["orders"]], ["O1", "O2"])
        self.assertEqual(self.app.stock("T"), before_stock)
        self.assertEqual(self.app.stock_history("T")["events"], before_stock_events)
        raw_after = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(raw_after["inventory"], raw_before["inventory"])
        self.assertEqual(raw_after["reservations"], raw_before.get("reservations", {}))
        self.assertEqual(raw_after["returns"], raw_before["returns"])
        records = self.app.get_returns("O1")
        self.assertEqual([r["return_id"] for r in records["records"]], ["R1"])

    def test_legacy_shipped_order_without_history_starts_at_one_incomplete(self):
        order = {"order_id": "OLD", "status": "shipped",
                 "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200}],
                 "total_cents": 200,
                 "shipment": {"carrier": "DHL", "tracking_no": "X-1"}}
        data = {"products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
                "orders": {"OLD": order}}
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        result = app.confirm_shipment_delivery("DHL", "X-1", "Wang", "2026-03-01")
        delivered = result["orders"][0]
        self.assertEqual(delivered["status"], "delivered")
        history = app.history("OLD")
        self.assertFalse(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                         [(1, "confirm-delivery")])
        self.assertEqual(history["events"][0]["result"], delivered)

    def test_single_order_confirm_still_rejects_repeats(self):
        self._ship("O1")
        self._ship("O2")
        self.app.confirm_shipment_delivery("DHL", "X-1", "Wang", "2026-03-01")
        with self.assertRaises(ValueError):
            self.app.confirm_delivery("O1", "Wang", "2026-03-01")
        with self.assertRaises(ValueError):
            self.app.confirm_delivery("O2", "Li", "2026-03-02")

    def test_cli_success_failure_and_array_partial_failure(self):
        self._ship("A")
        self._ship("B")
        self._ship("C", carrier="UPS", tracking_no="U-9")
        payload = self.root / "confirm.json"
        payload.write_text(json.dumps(
            {"carrier": " DHL ", "tracking_no": " X-1 ",
             "recipient": " Wang ", "delivered_on": " 2026-03-01 "}), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "confirm-shipment-delivery", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual(result["carrier"], "DHL")
        self.assertEqual([o["order_id"] for o in result["orders"]], ["A", "B"])
        # Repeating with the same info still succeeds with exit 0.
        again = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "confirm-shipment-delivery", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(again.returncode, 0, again.stderr)
        # An unknown combination fails with exit 2 and an error object.
        payload.write_text(json.dumps(
            {"carrier": "UPS", "tracking_no": "nope",
             "recipient": "Wang", "delivered_on": "2026-03-01"}), encoding="utf-8")
        failed = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "confirm-shipment-delivery", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2)
        self.assertIn("error", json.loads(failed.stderr))
        # Array input runs rows independently: the UPS shipment signs, the
        # unknown combination fails afterwards.
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([
            {"carrier": "UPS", "tracking_no": "U-9",
             "recipient": "Li", "delivered_on": "2026-03-02"},
            {"carrier": "UPS", "tracking_no": "nope",
             "recipient": "Li", "delivered_on": "2026-03-02"},
        ]), encoding="utf-8")
        stopped = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "confirm-shipment-delivery", str(batch)],
            text=True, capture_output=True)
        self.assertEqual(stopped.returncode, 2, stopped.stdout)
        self.assertEqual(OrderDesk(self.root).get("C")["status"], "delivered")


if __name__ == "__main__":
    unittest.main()
