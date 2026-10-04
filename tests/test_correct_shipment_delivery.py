import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk

class CorrectShipmentDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)

    def _deliver(self, order_id, carrier="DHL", tracking_no="X-1",
                 recipient="Wang", delivered_on="2026-03-01", lines=None):
        self.app.restock("T", 10)
        lines = lines if lines is not None else [{"sku": "T", "quantity": 1}]
        self.app.place(order_id, lines)
        self.app.ship(order_id, carrier, tracking_no)
        self.app.confirm_delivery(order_id, recipient, delivered_on)

    def test_bulk_correction_rewrites_every_matching_delivered_order(self):
        self._deliver("O2")
        self._deliver("O1", lines=[{"sku": "T", "quantity": 2}])
        self._deliver("O3", carrier="UPS", tracking_no="U-9")  # different shipment
        result = self.app.correct_shipment_delivery(
            " DHL ", " X-1 ",
            {"recipient": " Wang ", "delivered_on": " 2026-03-01 ", "note": "ignored"},
            {"recipient": "Li Si", "delivered_on": "2026-03-05", "extra": 1})
        self.assertEqual(set(result), {"carrier", "tracking_no", "orders"})
        self.assertEqual(result["carrier"], "DHL")
        self.assertEqual(result["tracking_no"], "X-1")
        # Orders come back sorted by order id, each the full get() structure.
        self.assertEqual([o["order_id"] for o in result["orders"]], ["O1", "O2"])
        for order in result["orders"]:
            self.assertEqual(order["status"], "delivered")
            self.assertEqual(order["delivery"],
                             {"recipient": "Li Si", "delivered_on": "2026-03-05"})
            self.assertEqual(order["shipment"], {"carrier": "DHL", "tracking_no": "X-1"})
            self.assertEqual(order, self.app.get(order["order_id"]))
        # The non-matching shipment keeps its original delivery.
        self.assertEqual(self.app.get("O3")["delivery"],
                         {"recipient": "Wang", "delivered_on": "2026-03-01"})
        # Persisted: a reopened desk sees the corrected state in get and history.
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.get("O1")["delivery"],
                         {"recipient": "Li Si", "delivered_on": "2026-03-05"})
        self.assertEqual(reopened.history("O1")["events"][-1]["action"], "correct-delivery")

    def test_history_events_continue_sequence_and_keep_snapshots(self):
        self._deliver("O1")
        self._deliver("O2")
        result = self.app.correct_shipment_delivery(
            "DHL", "X-1",
            {"recipient": "Wang", "delivered_on": "2026-03-01"},
            {"recipient": "Li", "delivered_on": "2026-03-02"})
        for order in result["orders"]:
            history = self.app.history(order["order_id"])
            self.assertTrue(history["complete"])
            self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                             [(1, "place"), (2, "ship"), (3, "confirm-delivery"),
                              (4, "correct-delivery")])
            event = history["events"][3]
            self.assertEqual(set(event), {"sequence", "action", "result"})
            self.assertEqual(event["result"], order)
            # Older snapshots are not rewritten.
            self.assertEqual(history["events"][2]["result"]["delivery"],
                             {"recipient": "Wang", "delivered_on": "2026-03-01"})

    def test_recipient_or_date_may_be_corrected_on_its_own(self):
        self._deliver("O1")
        result = self.app.correct_shipment_delivery(
            "DHL", "X-1",
            {"recipient": "Wang", "delivered_on": "2026-03-01"},
            {"recipient": "Li", "delivered_on": "2026-03-01"})
        self.assertEqual(result["orders"][0]["delivery"],
                         {"recipient": "Li", "delivered_on": "2026-03-01"})
        result = self.app.correct_shipment_delivery(
            "DHL", "X-1",
            {"recipient": "Li", "delivered_on": "2026-03-01"},
            {"recipient": "Li", "delivered_on": "2026-03-02"})
        self.assertEqual(result["orders"][0]["delivery"],
                         {"recipient": "Li", "delivered_on": "2026-03-02"})

    def test_same_target_succeeds_without_writing(self):
        self._deliver("O1")
        self._deliver("O2")
        delivery = {"recipient": "Wang", "delivered_on": "2026-03-01"}
        snapshot = self.app.path.read_bytes()
        result = self.app.correct_shipment_delivery("DHL", "X-1", delivery, dict(delivery))
        self.assertEqual([o["order_id"] for o in result["orders"]], ["O1", "O2"])
        self.assertEqual(self.app.path.read_bytes(), snapshot)
        self.assertEqual(len(self.app.history("O1")["events"]), 3)
        self.assertEqual(len(self.app.history("O2")["events"]), 3)

    def test_only_delivered_orders_are_selected(self):
        self._deliver("O1")
        # A shipped order under the same combination is not selected.
        self.app.restock("T", 10)
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        self.app.ship("O2", "DHL", "X-1")
        result = self.app.correct_shipment_delivery(
            "DHL", "X-1",
            {"recipient": "Wang", "delivered_on": "2026-03-01"},
            {"recipient": "Li", "delivered_on": "2026-03-02"})
        self.assertEqual([o["order_id"] for o in result["orders"]], ["O1"])
        self.assertEqual(self.app.get("O2")["status"], "shipped")
        self.assertNotIn("delivery", self.app.get("O2"))

    def test_matches_current_shipment_only_not_history(self):
        self._deliver("O1")
        self._deliver("O2", carrier="UPS", tracking_no="U-9")
        # O2's shipment is corrected onto DHL/X-1 after delivery.
        self.app.correct_shipment("O2", {"carrier": "UPS", "tracking_no": "U-9"},
                                  {"carrier": "DHL", "tracking_no": "X-1"})
        # O1 used to be DHL/X-1 but now ships under X-2.
        self.app.correct_shipment("O1", {"carrier": "DHL", "tracking_no": "X-1"},
                                  {"carrier": "DHL", "tracking_no": "X-2"})
        result = self.app.correct_shipment_delivery(
            "DHL", "X-1",
            {"recipient": "Wang", "delivered_on": "2026-03-01"},
            {"recipient": "Li", "delivered_on": "2026-03-02"})
        self.assertEqual([o["order_id"] for o in result["orders"]], ["O2"])
        self.assertEqual(self.app.get("O1")["delivery"],
                         {"recipient": "Wang", "delivered_on": "2026-03-01"})

    def test_invalid_shipments_are_skipped(self):
        self._deliver("O1")
        # Delivered orders with missing or malformed shipments never match.
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        for order_id, shipment in (("O2", None), ("O3", "DHL"),
                                   ("O4", {"carrier": "DHL"}),
                                   ("O5", {"carrier": "  ", "tracking_no": "X-1"}),
                                   ("O6", {"carrier": 7, "tracking_no": "X-1"})):
            data["orders"][order_id] = {
                "order_id": order_id, "status": "delivered",
                "lines": [{"sku": "T", "quantity": 1, "unit_price_cents": 100,
                           "subtotal_cents": 100}],
                "total_cents": 100,
                "delivery": {"recipient": "Wang", "delivered_on": "2026-03-01"}}
            if shipment is not None:
                data["orders"][order_id]["shipment"] = shipment
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        result = self.app.correct_shipment_delivery(
            "DHL", "X-1",
            {"recipient": "Wang", "delivered_on": "2026-03-01"},
            {"recipient": "Li", "delivered_on": "2026-03-02"})
        self.assertEqual([o["order_id"] for o in result["orders"]], ["O1"])

    def test_expected_mismatch_or_malformed_stored_delivery_rejects_everything(self):
        bad_deliveries = (
            {"recipient": "Li", "delivered_on": "2026-03-01"},      # different recipient
            {"recipient": "Wang", "delivered_on": "2026-03-02"},    # different date
            {"recipient": "wang", "delivered_on": "2026-03-01"},    # case differs
            "Wang",                                                  # not an object
            None,                                                    # missing
            {"recipient": "Wang"},                                   # missing date
            {"delivered_on": "2026-03-01"},                          # missing recipient
            {"recipient": "  ", "delivered_on": "2026-03-01"},       # blank text
            {"recipient": "Wang", "delivered_on": "2026-02-29"},     # invalid date
        )
        for bad in bad_deliveries:
            with self.subTest(bad=bad):
                self._deliver("O1")
                self._deliver("O2")
                # Corrupt the stored delivery directly.
                data = json.loads(self.app.path.read_text(encoding="utf-8"))
                if bad is None:
                    del data["orders"]["O1"]["delivery"]
                else:
                    data["orders"]["O1"]["delivery"] = bad
                self.app.path.write_text(json.dumps(data), encoding="utf-8")
                before = self.app.path.read_bytes()
                with self.assertRaises(ValueError):
                    self.app.correct_shipment_delivery(
                        "DHL", "X-1",
                        {"recipient": "Wang", "delivered_on": "2026-03-01"},
                        {"recipient": "Li", "delivered_on": "2026-03-02"})
                self.assertEqual(self.app.path.read_bytes(), before)
                self.assertEqual(OrderDesk(self.root).get("O2")["delivery"],
                                 {"recipient": "Wang", "delivered_on": "2026-03-01"})
                self.temp.cleanup()
                self.setUp()

    def test_expected_is_checked_even_when_target_changes_nothing(self):
        self._deliver("O1")
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.correct_shipment_delivery(
                "DHL", "X-1",
                {"recipient": "Li", "delivered_on": "2026-03-01"},
                {"recipient": "Wang", "delivered_on": "2026-03-01"})
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(len(self.app.history("O1")["events"]), 3)

    def test_no_match_and_invalid_inputs_rejected_without_side_effects(self):
        empty = self.root / "empty"
        app = OrderDesk(empty)
        with self.assertRaises(ValueError):
            app.correct_shipment_delivery(
                "DHL", "X-1",
                {"recipient": "Wang", "delivered_on": "2026-03-01"},
                {"recipient": "Li", "delivered_on": "2026-03-02"})
        self.assertFalse(empty.exists())
        self._deliver("O1")
        # A shipped order under the combination does not count as a match.
        self.app.restock("T", 10)
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        self.app.ship("O2", "UPS", "U-9")
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.correct_shipment_delivery(
                "UPS", "U-9",
                {"recipient": "Wang", "delivered_on": "2026-03-01"},
                {"recipient": "Li", "delivered_on": "2026-03-02"})
        expected = {"recipient": "Wang", "delivered_on": "2026-03-01"}
        target = {"recipient": "Li", "delivered_on": "2026-03-02"}
        for bad in (None, 123, b"DHL", ["DHL"], {"x": 1}, "", "   ", "\t"):
            with self.assertRaises(ValueError):
                self.app.correct_shipment_delivery(bad, "X-1", expected, target)
            with self.assertRaises(ValueError):
                self.app.correct_shipment_delivery("DHL", bad, expected, target)
        for bad_delivery in (None, 123, "Wang", ["x"], {},
                             {"recipient": "Wang"},
                             {"delivered_on": "2026-03-01"},
                             {"recipient": " ", "delivered_on": "2026-03-01"},
                             {"recipient": "Wang", "delivered_on": "2026-02-29"},
                             {"recipient": "Wang", "delivered_on": "2026-3-1"},
                             {"recipient": "Wang", "delivered_on": None}):
            with self.assertRaises(ValueError):
                self.app.correct_shipment_delivery("DHL", "X-1", bad_delivery, target)
            with self.assertRaises(ValueError):
                self.app.correct_shipment_delivery("DHL", "X-1", expected, bad_delivery)
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(len(self.app.history("O1")["events"]), 3)

    def test_valid_leap_day_is_accepted(self):
        self._deliver("O1")
        result = self.app.correct_shipment_delivery(
            "DHL", "X-1",
            {"recipient": "Wang", "delivered_on": "2026-03-01"},
            {"recipient": "Wang", "delivered_on": "2024-02-29"})
        self.assertEqual(result["orders"][0]["delivery"]["delivered_on"], "2024-02-29")

    def test_returns_do_not_block_and_no_stock_events_are_added(self):
        self._deliver("O1", lines=[{"sku": "T", "quantity": 4}])
        self._deliver("O2")
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        before_stock = self.app.stock("T")
        before_stock_events = self.app.stock_history("T")["events"]
        raw_before = json.loads(self.app.path.read_text(encoding="utf-8"))
        result = self.app.correct_shipment_delivery(
            "DHL", "X-1",
            {"recipient": "Wang", "delivered_on": "2026-03-01"},
            {"recipient": "Li", "delivered_on": "2026-03-02"})
        self.assertEqual([o["order_id"] for o in result["orders"]], ["O1", "O2"])
        self.assertEqual(self.app.stock("T"), before_stock)
        self.assertEqual(self.app.stock_history("T")["events"], before_stock_events)
        raw_after = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(raw_after["inventory"], raw_before["inventory"])
        self.assertEqual(raw_after["reservations"], raw_before.get("reservations", {}))
        self.assertEqual(raw_after["returns"], raw_before["returns"])
        records = self.app.get_returns("O1")
        self.assertEqual([r["return_id"] for r in records["records"]], ["R1"])

    def test_legacy_delivered_order_without_history_starts_at_one_incomplete(self):
        order = {"order_id": "OLD", "status": "delivered",
                 "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100,
                            "subtotal_cents": 200}],
                 "total_cents": 200,
                 "shipment": {"carrier": "DHL", "tracking_no": "X-1"},
                 "delivery": {"recipient": "Wang", "delivered_on": "2026-03-01"}}
        data = {"products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
                "orders": {"OLD": order}}
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        result = app.correct_shipment_delivery(
            "DHL", "X-1",
            {"recipient": "Wang", "delivered_on": "2026-03-01"},
            {"recipient": "Li", "delivered_on": "2026-03-02"})
        corrected = result["orders"][0]
        self.assertEqual(corrected["delivery"],
                         {"recipient": "Li", "delivered_on": "2026-03-02"})
        history = app.history("OLD")
        self.assertFalse(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                         [(1, "correct-delivery")])
        self.assertEqual(history["events"][0]["result"], corrected)

    def test_cli_success_failure_and_array_partial_failure(self):
        self._deliver("A")
        self._deliver("B")
        self._deliver("C", carrier="UPS", tracking_no="U-9")
        payload = self.root / "correct.json"
        payload.write_text(json.dumps(
            {"carrier": " DHL ", "tracking_no": " X-1 ",
             "expected_delivery": {"recipient": " Wang ", "delivered_on": " 2026-03-01 "},
             "delivery": {"recipient": "Li", "delivered_on": "2026-03-02"}}),
            encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "correct-shipment-delivery", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual(result["carrier"], "DHL")
        self.assertEqual([o["order_id"] for o in result["orders"]], ["A", "B"])
        self.assertEqual(result["orders"][0]["delivery"],
                         {"recipient": "Li", "delivered_on": "2026-03-02"})
        # Repeating with the corrected info as both expected and target
        # still succeeds with exit 0.
        payload.write_text(json.dumps(
            {"carrier": "DHL", "tracking_no": "X-1",
             "expected_delivery": {"recipient": "Li", "delivered_on": "2026-03-02"},
             "delivery": {"recipient": "Li", "delivered_on": "2026-03-02"}}),
            encoding="utf-8")
        again = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "correct-shipment-delivery", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(again.returncode, 0, again.stderr)
        # An unknown combination fails with exit 2 and an error object.
        payload.write_text(json.dumps(
            {"carrier": "UPS", "tracking_no": "nope",
             "expected_delivery": {"recipient": "Wang", "delivered_on": "2026-03-01"},
             "delivery": {"recipient": "Li", "delivered_on": "2026-03-02"}}),
            encoding="utf-8")
        failed = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "correct-shipment-delivery", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2)
        self.assertIn("error", json.loads(failed.stderr))
        # Array input runs rows independently: the UPS shipment is corrected,
        # the unknown combination fails afterwards.
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([
            {"carrier": "UPS", "tracking_no": "U-9",
             "expected_delivery": {"recipient": "Wang", "delivered_on": "2026-03-01"},
             "delivery": {"recipient": "Zhao", "delivered_on": "2026-03-03"}},
            {"carrier": "UPS", "tracking_no": "nope",
             "expected_delivery": {"recipient": "Wang", "delivered_on": "2026-03-01"},
             "delivery": {"recipient": "Zhao", "delivered_on": "2026-03-03"}},
        ]), encoding="utf-8")
        stopped = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "correct-shipment-delivery", str(batch)],
            text=True, capture_output=True)
        self.assertEqual(stopped.returncode, 2, stopped.stdout)
        self.assertEqual(OrderDesk(self.root).get("C")["delivery"],
                         {"recipient": "Zhao", "delivered_on": "2026-03-03"})


if __name__ == "__main__":
    unittest.main()
