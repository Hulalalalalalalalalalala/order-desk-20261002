import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk

class RevokeShipmentDeliveryTests(unittest.TestCase):
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

    def _delivered(self, order_id, carrier="DHL", tracking_no="X-1",
                   recipient="Wang Wu", delivered_on="2026-03-01", lines=None):
        self._ship(order_id, carrier, tracking_no, lines)
        self.app.confirm_delivery(order_id, recipient, delivered_on)

    def _expected(self, recipient="Wang Wu", delivered_on="2026-03-01"):
        return {"recipient": recipient, "delivered_on": delivered_on}

    def test_revokes_every_matching_delivered_order_and_persists(self):
        self._delivered("O2")
        self._delivered("O1", lines=[{"sku": "T", "quantity": 2}])
        self._delivered("O3", carrier="UPS", tracking_no="U-9")  # different shipment
        result = self.app.revoke_shipment_delivery(
            " DHL ", " X-1 ",
            {"recipient": " Wang Wu ", "delivered_on": " 2026-03-01 "},
        )
        self.assertEqual(set(result), {"carrier", "tracking_no", "orders"})
        self.assertEqual(result["carrier"], "DHL")
        self.assertEqual(result["tracking_no"], "X-1")
        # Only delivered orders under the combination come back, sorted by id,
        # each the full get() structure after the revoke.
        self.assertEqual([o["order_id"] for o in result["orders"]], ["O1", "O2"])
        for order in result["orders"]:
            self.assertEqual(order["status"], "shipped")
            self.assertNotIn("delivery", order)
            self.assertEqual(order["shipment"], {"carrier": "DHL", "tracking_no": "X-1"})
            self.assertEqual(order, self.app.get(order["order_id"]))
        # The non-matching delivered order is untouched.
        self.assertEqual(self.app.get("O3")["status"], "delivered")
        self.assertEqual(self.app.get("O3")["delivery"],
                         {"recipient": "Wang Wu", "delivered_on": "2026-03-01"})
        # Persisted: a reopened desk sees the revoked state and history.
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.get("O1")["status"], "shipped")
        self.assertNotIn("delivery", reopened.get("O1"))
        self.assertEqual(reopened.history("O1")["events"][-1]["action"], "revoke-delivery")

    def test_shipped_orders_under_the_combination_are_not_selected(self):
        self._delivered("O1")
        self._ship("O2")  # same shipment, still shipped
        result = self.app.revoke_shipment_delivery(
            "DHL", "X-1", self._expected())
        self.assertEqual([o["order_id"] for o in result["orders"]], ["O1"])
        self.assertEqual(self.app.get("O1")["status"], "shipped")
        self.assertNotIn("delivery", self.app.get("O1"))
        # The already shipped order is neither selected nor changed.
        o2 = self.app.get("O2")
        self.assertEqual(o2["status"], "shipped")
        self.assertNotIn("delivery", o2)
        deliver = {e["order_id"] for e in self.app.order_worklist(stage="deliver")}
        self.assertEqual(deliver, {"O1", "O2"})

    def test_other_statuses_under_the_combination_are_not_selected(self):
        self._delivered("O1", carrier="UPS", tracking_no="U-9")
        self.app.place("O2", [{"sku": "T", "quantity": 1}])  # placed
        self._ship("O3")
        self.app.record_return("O3", "R1", [{"sku": "T", "quantity": 1}])
        # No delivered order under DHL/X-1: placed, shipped and a delivered
        # order under another combination do not count.
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.revoke_shipment_delivery("DHL", "X-1", self._expected())
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.get("O1")["status"], "delivered")

    def test_history_events_continue_sequence_and_keep_old_snapshots(self):
        self._delivered("O1")
        self._delivered("O2")
        result = self.app.revoke_shipment_delivery("DHL", "X-1", self._expected())
        for order in result["orders"]:
            history = self.app.history(order["order_id"])
            self.assertTrue(history["complete"])
            self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                             [(1, "place"), (2, "ship"),
                              (3, "confirm-delivery"), (4, "revoke-delivery")])
            event = history["events"][3]
            self.assertEqual(set(event), {"sequence", "action", "result"})
            self.assertEqual(event["result"], order)
            # Older snapshots are not rewritten.
            self.assertEqual(history["events"][2]["result"]["delivery"],
                             {"recipient": "Wang Wu", "delivered_on": "2026-03-01"})
            self.assertEqual(history["events"][2]["result"]["status"], "delivered")
            self.assertEqual(history["events"][1]["result"]["status"], "shipped")

    def test_worklists_list_revoked_orders_and_they_can_be_signed_again(self):
        self._delivered("O1")
        self._delivered("O2")
        self._delivered("O3", carrier="UPS", tracking_no="U-9")
        self.app.revoke_shipment_delivery("DHL", "X-1", self._expected())
        # The fulfillment worklist asks to deliver the revoked orders again.
        deliver = {e["order_id"] for e in self.app.order_worklist(stage="deliver")}
        self.assertEqual(deliver, {"O1", "O2"})
        # The shipment checklist lists the revoked ids as pending; O3 stays
        # signed under its own combination.
        groups = {(g["carrier"], g["tracking_no"]): g
                  for g in self.app.shipment_worklist(stage="all")}
        self.assertEqual(groups[("DHL", "X-1")]["pending_ids"], ["O1", "O2"])
        self.assertEqual(groups[("DHL", "X-1")]["deliveries"], [])
        self.assertFalse(groups[("DHL", "X-1")]["conflict"])
        self.assertEqual(groups[("UPS", "U-9")]["pending_ids"], [])
        # The original single-order entry can sign a revoked order again.
        self.app.confirm_delivery("O1", "Li Si", "2026-03-09")
        self.assertEqual(self.app.get("O1")["status"], "delivered")
        # The bulk entry signs the rest.
        again = self.app.confirm_shipment_delivery("DHL", "X-1", "Li Si", "2026-03-09")
        self.assertEqual([o["order_id"] for o in again["orders"]], ["O1", "O2"])
        self.assertEqual([e["action"] for e in self.app.history("O2")["events"]],
                         ["place", "ship", "confirm-delivery",
                          "revoke-delivery", "confirm-delivery"])

    def test_repeat_after_full_revoke_fails_without_side_effects(self):
        self._delivered("O1")
        self.app.revoke_shipment_delivery("DHL", "X-1", self._expected())
        snapshot = self.app.path.read_bytes()
        events = self.app.history("O1")["events"]
        # No delivered order remains under the combination: the repeat is a
        # failure, not a no-op success.
        with self.assertRaises(ValueError):
            self.app.revoke_shipment_delivery("DHL", "X-1", self._expected())
        self.assertEqual(self.app.path.read_bytes(), snapshot)
        self.assertEqual(self.app.history("O1")["events"], events)
        self.assertEqual(self.app.get("O1")["status"], "shipped")

    def test_expected_mismatch_is_rejected_and_changes_nothing(self):
        self._delivered("O1")
        self._delivered("O2")
        raw = self.app.path.read_bytes()
        # Wrong recipient.
        with self.assertRaises(ValueError):
            self.app.revoke_shipment_delivery(
                "DHL", "X-1", self._expected(recipient="Someone Else"))
        # Wrong date.
        with self.assertRaises(ValueError):
            self.app.revoke_shipment_delivery(
                "DHL", "X-1", self._expected(delivered_on="2026-03-02"))
        # Matching is case sensitive on shipment and recipient.
        with self.assertRaises(ValueError):
            self.app.revoke_shipment_delivery("dhl", "X-1", self._expected())
        with self.assertRaises(ValueError):
            self.app.revoke_shipment_delivery("DHL", "x-1", self._expected())
        with self.assertRaises(ValueError):
            self.app.revoke_shipment_delivery(
                "DHL", "X-1", self._expected(recipient="wang wu"))
        self.assertEqual(self.app.path.read_bytes(), raw)
        for order_id in ("O1", "O2"):
            order = self.app.get(order_id)
            self.assertEqual(order["status"], "delivered")
            self.assertEqual(order["delivery"],
                             {"recipient": "Wang Wu", "delivered_on": "2026-03-01"})
            self.assertEqual([e["action"] for e in self.app.history(order_id)["events"]],
                             ["place", "ship", "confirm-delivery"])

    def test_one_conflicting_or_malformed_delivery_rejects_everything(self):
        bad_deliveries = (
            {"recipient": "Li", "delivered_on": "2026-03-01"},      # different recipient
            {"recipient": "Wang Wu", "delivered_on": "2026-03-02"},  # different date
            "Wang Wu",                                               # not an object
            {"recipient": "Wang Wu"},                                # missing date
            {"delivered_on": "2026-03-01"},                          # missing recipient
            {"recipient": "  ", "delivered_on": "2026-03-01"},       # blank text
            {"recipient": 5, "delivered_on": "2026-03-01"},          # non-string recipient
            {"recipient": "Wang Wu", "delivered_on": "2026-02-29"},  # invalid date
            {"recipient": "Wang Wu", "delivered_on": "2026/03/01"},  # other format
        )
        for bad in bad_deliveries:
            with self.subTest(bad=bad):
                self._delivered("O1")
                self._delivered("O2")
                data = json.loads(self.app.path.read_text(encoding="utf-8"))
                data["orders"]["O1"]["delivery"] = bad
                self.app.path.write_text(json.dumps(data), encoding="utf-8")
                before = self.app.path.read_bytes()
                with self.assertRaises(ValueError):
                    self.app.revoke_shipment_delivery("DHL", "X-1", self._expected())
                self.assertEqual(self.app.path.read_bytes(), before)
                self.assertEqual(OrderDesk(self.root).get("O2")["status"], "delivered")
                self.temp.cleanup()
                self.setUp()

    def test_missing_current_delivery_rejects_everything(self):
        self._delivered("O1")
        self._delivered("O2")
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        del data["orders"]["O1"]["delivery"]
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.revoke_shipment_delivery("DHL", "X-1", self._expected())
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertNotIn("delivery", OrderDesk(self.root).get("O1"))
        self.assertEqual(OrderDesk(self.root).get("O2")["status"], "delivered")

    def test_no_selected_order_rejected_without_side_effects(self):
        empty = self.root / "empty"
        app = OrderDesk(empty)
        with self.assertRaises(ValueError):
            app.revoke_shipment_delivery("DHL", "X-1", self._expected())
        self.assertFalse(empty.exists())
        # A shipped match is not selected; a cancelled order never is.
        self._ship("O1")
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        self.app.cancel("O2")
        # A delivered order under a different combination does not count.
        self._delivered("O3", carrier="UPS", tracking_no="U-9")
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.revoke_shipment_delivery("DHL", "X-1", self._expected())
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.get("O1")["status"], "shipped")
        self.assertNotIn("delivery", self.app.get("O1"))
        self.assertEqual(self.app.get("O3")["delivery"],
                         {"recipient": "Wang Wu", "delivered_on": "2026-03-01"})
        self.assertEqual(len(self.app.history("O1")["events"]), 2)

    def test_invalid_inputs_rejected_without_side_effects(self):
        self._delivered("O1")
        before = self.app.path.read_bytes()
        for bad in (None, 123, b"DHL", ["DHL"], {"x": 1}, "", "   ", "\t"):
            with self.assertRaises(ValueError):
                self.app.revoke_shipment_delivery(bad, "X-1", self._expected())
            with self.assertRaises(ValueError):
                self.app.revoke_shipment_delivery("DHL", bad, self._expected())
        for bad in (None, [], "Wang", 42, {"recipient": "Wang Wu"},
                    {"delivered_on": "2026-03-01"},
                    {"recipient": "  ", "delivered_on": "2026-03-01"},
                    {"recipient": "Wang Wu", "delivered_on": "2026-02-29"},
                    {"recipient": "Wang Wu", "delivered_on": "2026-3-1"}):
            with self.assertRaises(ValueError):
                self.app.revoke_shipment_delivery("DHL", "X-1", bad)
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.get("O1")["delivery"], self._expected())

    def test_valid_leap_day_is_accepted(self):
        self._delivered("O1", delivered_on="2024-02-29")
        result = self.app.revoke_shipment_delivery(
            "DHL", "X-1", self._expected(delivered_on="2024-02-29"))
        self.assertEqual(result["orders"][0]["status"], "shipped")
        self.assertNotIn("delivery", result["orders"][0])

    def test_extra_fields_in_expected_delivery_are_ignored(self):
        self._delivered("O1")
        result = self.app.revoke_shipment_delivery(
            "DHL", "X-1",
            {"recipient": "Wang Wu", "delivered_on": "2026-03-01", "signed_by": "x"},
        )
        self.assertEqual(result["orders"][0]["status"], "shipped")
        self.assertNotIn("delivery", result["orders"][0])

    def test_matches_current_shipment_only_not_history(self):
        self._delivered("O1", carrier="UPS", tracking_no="U-9")
        self._delivered("O2", carrier="UPS", tracking_no="U-9")
        # O2 used to ship under UPS/U-9 and was corrected to DHL/X-1; O1
        # currently ships DHL/X-2 after moving off DHL/X-1.
        self.app.correct_shipment("O2", {"carrier": "UPS", "tracking_no": "U-9"},
                                  {"carrier": "DHL", "tracking_no": "X-1"})
        self.app.correct_shipment("O1", {"carrier": "UPS", "tracking_no": "U-9"},
                                  {"carrier": "DHL", "tracking_no": "X-2"})
        result = self.app.revoke_shipment_delivery("DHL", "X-1", self._expected())
        self.assertEqual([o["order_id"] for o in result["orders"]], ["O2"])
        self.assertEqual(self.app.get("O1")["status"], "delivered")
        self.assertEqual(self.app.get("O1")["delivery"],
                         {"recipient": "Wang Wu", "delivered_on": "2026-03-01"})
        self.assertEqual(self.app.get("O2")["status"], "shipped")
        self.assertNotIn("delivery", self.app.get("O2"))

    def test_returns_do_not_block_and_business_records_stay_intact(self):
        self._delivered("O1", lines=[{"sku": "T", "quantity": 4}])
        self._delivered("O2")
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.receive_return("R1")
        before_stock = self.app.stock("T")
        before_stock_events = self.app.stock_history("T")["events"]
        before_summary = self.app.fulfillment_summary()
        raw_before = json.loads(self.app.path.read_text(encoding="utf-8"))
        result = self.app.revoke_shipment_delivery("DHL", "X-1", self._expected())
        self.assertEqual([o["order_id"] for o in result["orders"]], ["O1", "O2"])
        # Stock, stock events and fulfillment money/quantities never change.
        self.assertEqual(self.app.stock("T"), before_stock)
        self.assertEqual(self.app.stock_history("T")["events"], before_stock_events)
        self.assertEqual(self.app.fulfillment_summary(), before_summary)
        raw_after = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(raw_after["inventory"], raw_before["inventory"])
        self.assertEqual(raw_after["reservations"], raw_before.get("reservations", {}))
        self.assertEqual(raw_after.get("carts", {}), raw_before.get("carts", {}))
        self.assertEqual(raw_after["returns"], raw_before["returns"])
        self.assertEqual(raw_after["return_receipts"], raw_before["return_receipts"])
        # Status flips to shipped; lines, amounts and shipment are preserved.
        order = self.app.get("O1")
        self.assertEqual(order["status"], "shipped")
        self.assertEqual(order["lines"], raw_before["orders"]["O1"]["lines"])
        self.assertEqual(order["total_cents"], raw_before["orders"]["O1"]["total_cents"])
        self.assertEqual(order["shipment"], {"carrier": "DHL", "tracking_no": "X-1"})
        self.assertEqual([r["return_id"] for r in self.app.get_returns("O1")["records"]], ["R1"])
        self.assertEqual(self.app.get_return_receipt("R1"),
                         raw_before["return_receipts"]["R1"])
        # Fulfillment progress keeps counting the pending/received return.
        progress = self.app.order_progress("O1")
        self.assertEqual(progress["order"]["status"], "shipped")
        self.assertEqual(progress["lines"][0]["received"], 1)
        self.assertEqual(progress["lines"][0]["shipped"], 4)

    def test_legacy_delivered_order_without_history_starts_at_one_incomplete(self):
        order = {"order_id": "OLD", "status": "delivered",
                 "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200}],
                 "total_cents": 200,
                 "shipment": {"carrier": "DHL", "tracking_no": "X-1"},
                 "delivery": {"recipient": "Wang", "delivered_on": "2026-03-01"}}
        data = {"products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
                "orders": {"OLD": order}}
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        result = app.revoke_shipment_delivery(
            "DHL", "X-1", self._expected(recipient="Wang"))
        revoked = result["orders"][0]
        self.assertEqual(revoked["status"], "shipped")
        self.assertNotIn("delivery", revoked)
        history = app.history("OLD")
        self.assertFalse(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                         [(1, "revoke-delivery")])
        self.assertEqual(history["events"][0]["result"], revoked)

    def test_cli_success_failure_and_array_partial_failure(self):
        self._delivered("A")
        self._delivered("B")
        self._delivered("C", carrier="UPS", tracking_no="U-9")
        payload = self.root / "revoke.json"
        payload.write_text(json.dumps(
            {"carrier": " DHL ", "tracking_no": " X-1 ",
             "expected_delivery": {"recipient": " Wang Wu ", "delivered_on": " 2026-03-01 "}}),
            encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "revoke-shipment-delivery", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual(result["carrier"], "DHL")
        self.assertEqual(result["tracking_no"], "X-1")
        self.assertEqual([o["order_id"] for o in result["orders"]], ["A", "B"])
        for order in result["orders"]:
            self.assertEqual(order["status"], "shipped")
            self.assertNotIn("delivery", order)
        # Repeating now fails with exit 2: no delivered order remains.
        failed = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "revoke-shipment-delivery", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2)
        self.assertIn("error", json.loads(failed.stderr))
        # A bad expected date fails with exit 2 and an error object.
        bad_payload = self.root / "bad.json"
        bad_payload.write_text(json.dumps(
            {"carrier": "UPS", "tracking_no": "U-9",
             "expected_delivery": {"recipient": "Wang Wu", "delivered_on": "2026-02-29"}}),
            encoding="utf-8")
        bad = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "revoke-shipment-delivery", str(bad_payload)],
            text=True, capture_output=True)
        self.assertEqual(bad.returncode, 2)
        self.assertIn("error", json.loads(bad.stderr))
        # Array input runs rows independently: the UPS revoke succeeds and the
        # following unknown combination fails afterwards.
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([
            {"carrier": "UPS", "tracking_no": "U-9",
             "expected_delivery": {"recipient": "Wang Wu", "delivered_on": "2026-03-01"}},
            {"carrier": "UPS", "tracking_no": "nope",
             "expected_delivery": {"recipient": "Wang Wu", "delivered_on": "2026-03-01"}},
        ]), encoding="utf-8")
        stopped = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "revoke-shipment-delivery", str(batch)],
            text=True, capture_output=True)
        self.assertEqual(stopped.returncode, 2, stopped.stdout)
        self.assertEqual(OrderDesk(self.root).get("C")["status"], "shipped")
        self.assertNotIn("delivery", OrderDesk(self.root).get("C"))


if __name__ == "__main__":
    unittest.main()
