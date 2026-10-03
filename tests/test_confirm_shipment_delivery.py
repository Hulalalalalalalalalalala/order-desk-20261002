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

    def _ship(self, order_id, lines, carrier="DHL", tracking_no="X-1"):
        self.app.place(order_id, lines)
        self.app.ship(order_id, carrier, tracking_no)

    def _write_raw(self, data):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "data.json").write_text(json.dumps(data), encoding="utf-8")

    def test_signs_every_matching_shipped_order_and_returns_get_shape(self):
        self.app.restock("T", 20)
        self.app.restock("C", 20)
        self._ship("B", [{"sku": "T", "quantity": 2}], "DHL", "SAME")
        self._ship("A", [{"sku": "C", "quantity": 1}, {"sku": "T", "quantity": 3}],
                   " DHL ", " SAME ")
        # A third order on a different shipment stays shipped.
        self._ship("Z", [{"sku": "T", "quantity": 1}], "UPS", "OTHER")
        result = self.app.confirm_shipment_delivery(
            "  DHL  ", "  SAME  ", "  Wang Wu ", " 2026-03-01 ")
        self.assertEqual(set(result), {"carrier", "tracking_no", "orders"})
        self.assertEqual(result["carrier"], "DHL")
        self.assertEqual(result["tracking_no"], "SAME")
        self.assertEqual([o["order_id"] for o in result["orders"]], ["A", "B"])
        for order in result["orders"]:
            self.assertEqual(order, self.app.get(order["order_id"]))
            self.assertEqual(order["status"], "delivered")
            self.assertEqual(order["delivery"],
                             {"recipient": "Wang Wu", "delivered_on": "2026-03-01"})
            self.assertEqual(set(order),
                             {"order_id", "status", "lines", "total_cents",
                              "shipment", "delivery"})
        # The other shipment is untouched.
        self.assertEqual(self.app.get("Z")["status"], "shipped")
        self.assertNotIn("delivery", self.app.get("Z"))

    def test_invalid_text_inputs_are_rejected(self):
        self.app.restock("T", 10)
        self._ship("O1", [{"sku": "T", "quantity": 1}])
        before = self.app.path.read_bytes()
        for bad in (None, 123, 1.5, b"DHL", ["DHL"], {"x": 1}, "   ", "\t\n"):
            with self.assertRaises(ValueError):
                self.app.confirm_shipment_delivery(bad, "X-1", "Wang", "2026-03-01")
            with self.assertRaises(ValueError):
                self.app.confirm_shipment_delivery("DHL", bad, "Wang", "2026-03-01")
            with self.assertRaises(ValueError):
                self.app.confirm_shipment_delivery("DHL", "X-1", bad, "2026-03-01")
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_invalid_dates_are_rejected(self):
        self.app.restock("T", 10)
        self._ship("O1", [{"sku": "T", "quantity": 1}])
        before = self.app.path.read_bytes()
        bad_dates = (
            None, 20260301, 2026.0, b"2026-03-01", ["2026-03-01"], {"x": 1},
            "", "   ", "\t\n",
            "2026/03/01", "2026-3-1", "03-01-2026", "20260301",
            "2026-13-01", "2026-10-32", "2026-02-29", "2026-03-01T00:00:00",
        )
        for bad in bad_dates:
            with self.assertRaises(ValueError):
                self.app.confirm_shipment_delivery("DHL", "X-1", "Wang", bad)
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.get("O1")["status"], "shipped")

    def test_valid_leap_day_is_accepted(self):
        self.app.restock("T", 10)
        self._ship("O1", [{"sku": "T", "quantity": 1}])
        result = self.app.confirm_shipment_delivery("DHL", "X-1", "Wang", "2024-02-29")
        self.assertEqual(result["orders"][0]["delivery"]["delivered_on"], "2024-02-29")

    def test_no_match_raises_even_for_valid_inputs(self):
        self.app.restock("T", 10)
        self._ship("O1", [{"sku": "T", "quantity": 1}], "DHL", "X-1")
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.confirm_shipment_delivery("DHL", "nope", "Wang", "2026-03-01")
        with self.assertRaises(ValueError):
            self.app.confirm_shipment_delivery("UPS", "X-1", "Wang", "2026-03-01")
        with self.assertRaises(ValueError):
            self.app.confirm_shipment_delivery("dhl", "X-1", "Wang", "2026-03-01")
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.get("O1")["status"], "shipped")

    def test_empty_root_raises_and_creates_no_directory(self):
        empty = self.root / "empty"
        app = OrderDesk(empty)
        with self.assertRaises(ValueError):
            app.confirm_shipment_delivery("DHL", "X-1", "Wang", "2026-03-01")
        self.assertFalse(empty.exists())

    def test_invalid_inputs_create_no_directory(self):
        empty = self.root / "empty"
        app = OrderDesk(empty)
        with self.assertRaises(ValueError):
            app.confirm_shipment_delivery(None, "X-1", "Wang", "2026-03-01")
        with self.assertRaises(ValueError):
            app.confirm_shipment_delivery("DHL", "X-1", "Wang", "2026-02-29")
        self.assertFalse(empty.exists())

    def test_only_current_shipment_matches_never_history(self):
        self.app.restock("T", 10)
        self._ship("O1", [{"sku": "T", "quantity": 1}], "DHL", "OLD-1")
        self.app.correct_shipment(
            "O1", {"carrier": "DHL", "tracking_no": "OLD-1"},
            {"carrier": "UPS", "tracking_no": "NEW-2"})
        with self.assertRaises(ValueError):
            self.app.confirm_shipment_delivery("DHL", "OLD-1", "Wang", "2026-03-01")
        self.assertEqual(self.app.get("O1")["status"], "shipped")
        result = self.app.confirm_shipment_delivery("UPS", "NEW-2", "Wang", "2026-03-01")
        self.assertEqual([o["order_id"] for o in result["orders"]], ["O1"])
        self.assertEqual(result["orders"][0]["status"], "delivered")

    def test_only_shipped_or_delivered_orders_can_match(self):
        self.app.restock("T", 20)
        self.app.place("P1", [{"sku": "T", "quantity": 1}])
        self.app.place("P2", [{"sku": "T", "quantity": 1}])
        self.app.ship("P2", "DHL", "X-1")
        self.app.cancel("P1")
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        # Legacy placed/cancelled orders carrying a shipment-shaped field.
        data["orders"]["P1"]["shipment"] = {"carrier": "DHL", "tracking_no": "X-1"}
        data["orders"]["P3"] = {
            "order_id": "P3", "status": "placed",
            "lines": [{"sku": "T", "quantity": 1, "unit_price_cents": 100,
                       "subtotal_cents": 100}],
            "total_cents": 100,
            "shipment": {"carrier": "DHL", "tracking_no": "X-1"}}
        self._write_raw(data)
        app = OrderDesk(self.root)
        result = app.confirm_shipment_delivery("DHL", "X-1", "Wang", "2026-03-01")
        self.assertEqual([o["order_id"] for o in result["orders"]], ["P2"])
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.get("P1")["status"], "cancelled")
        self.assertEqual(reopened.get("P3")["status"], "placed")

    def test_legacy_bad_shipments_are_skipped(self):
        self.app.restock("T", 20)
        self._ship("GOOD", [{"sku": "T", "quantity": 1}], "DHL", "X-1")
        base = {"order_id": "BAD", "status": "shipped",
                "lines": [{"sku": "T", "quantity": 5, "unit_price_cents": 100,
                           "subtotal_cents": 500}],
                "total_cents": 500}
        variants = [
            None, "DHL X-1", {}, {"carrier": "DHL"}, {"tracking_no": "X-1"},
            {"carrier": 5, "tracking_no": "X-1"},
            {"carrier": "DHL", "tracking_no": None},
            {"carrier": "   ", "tracking_no": "X-1"},
            {"carrier": "DHL", "tracking_no": "\t"},
        ]
        for index, shipment in enumerate(variants):
            order = json.loads(json.dumps(base))
            order["order_id"] = "BAD" + str(index)
            if shipment is not None:
                order["shipment"] = shipment
            data = json.loads(self.app.path.read_text(encoding="utf-8"))
            data["orders"][order["order_id"]] = order
            self._write_raw(data)
        result = OrderDesk(self.root).confirm_shipment_delivery(
            "DHL", "X-1", "Wang", "2026-03-01")
        self.assertEqual([o["order_id"] for o in result["orders"]], ["GOOD"])

    def test_already_delivered_same_normalized_info_is_idempotent_without_write(self):
        self.app.restock("T", 10)
        self._ship("A", [{"sku": "T", "quantity": 1}], "DHL", "SAME")
        self._ship("B", [{"sku": "T", "quantity": 1}], "DHL", "SAME")
        first = self.app.confirm_shipment_delivery("DHL", "SAME", "Wang", "2026-03-01")
        raw_after_first = self.app.path.read_bytes()
        histories = {oid: self.app.history(oid) for oid in ("A", "B")}
        # Repeating with padded text still normalizes to the same information.
        second = self.app.confirm_shipment_delivery(
            " DHL ", " SAME ", "  Wang  ", " 2026-03-01 ")
        self.assertEqual(second, first)
        self.assertEqual(self.app.path.read_bytes(), raw_after_first)
        for oid in ("A", "B"):
            self.assertEqual(self.app.history(oid), histories[oid])
            self.assertEqual([(e["sequence"], e["action"])
                              for e in self.app.history(oid)["events"]],
                             [(1, "place"), (2, "ship"), (3, "confirm-delivery")])

    def test_legacy_padded_delivery_normalizes_for_comparison_but_is_kept(self):
        order = {"order_id": "OLD", "status": "delivered",
                 "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100,
                            "subtotal_cents": 200}],
                 "total_cents": 200,
                 "shipment": {"carrier": "DHL", "tracking_no": "X-1"},
                 "delivery": {"recipient": "  Wang  ", "delivered_on": " 2026-03-01 "}}
        self._write_raw({"orders": {"OLD": order}})
        raw_before = (self.root / "data.json").read_bytes()
        result = OrderDesk(self.root).confirm_shipment_delivery(
            "DHL", "X-1", "Wang", "2026-03-01")
        self.assertEqual([o["order_id"] for o in result["orders"]], ["OLD"])
        # No write happened: the padded legacy record is preserved verbatim.
        self.assertEqual((self.root / "data.json").read_bytes(), raw_before)

    def test_mixed_state_only_pending_orders_get_events(self):
        self.app.restock("T", 20)
        self._ship("A", [{"sku": "T", "quantity": 1}], "DHL", "SAME")
        self._ship("B", [{"sku": "T", "quantity": 1}], "DHL", "SAME")
        self._ship("C", [{"sku": "T", "quantity": 1}], "DHL", "SAME")
        self.app.confirm_delivery("B", "Wang", "2026-03-01")
        # Give B a padded legacy-style delivery: matching is normalized, but
        # the stored record must be left untouched for already delivered orders.
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        raw["orders"]["B"]["delivery"] = {"recipient": "  Wang  ",
                                          "delivered_on": " 2026-03-01 "}
        self._write_raw(raw)
        result = self.app.confirm_shipment_delivery(
            "DHL", "SAME", "Wang", "2026-03-01")
        self.assertEqual([o["order_id"] for o in result["orders"]], ["A", "B", "C"])
        # Newly signed orders carry the normalized delivery; B keeps its record.
        for order_id in ("A", "C"):
            order = next(o for o in result["orders"] if o["order_id"] == order_id)
            self.assertEqual(order["status"], "delivered")
            self.assertEqual(order["delivery"],
                             {"recipient": "Wang", "delivered_on": "2026-03-01"})
        b_view = next(o for o in result["orders"] if o["order_id"] == "B")
        self.assertEqual(b_view["status"], "delivered")
        self.assertEqual(b_view["delivery"],
                         {"recipient": "  Wang  ", "delivered_on": " 2026-03-01 "})
        # A and C gained a confirm-delivery event; B kept its single one.
        self.assertEqual([(e["sequence"], e["action"])
                          for e in self.app.history("A")["events"]],
                         [(1, "place"), (2, "ship"), (3, "confirm-delivery")])
        self.assertEqual([(e["sequence"], e["action"])
                          for e in self.app.history("B")["events"]],
                         [(1, "place"), (2, "ship"), (3, "confirm-delivery")])
        self.assertEqual([(e["sequence"], e["action"])
                          for e in self.app.history("C")["events"]],
                         [(1, "place"), (2, "ship"), (3, "confirm-delivery")])
        # B's padded stored delivery is not replaced; its existing event
        # snapshot is likewise left as it was recorded.
        raw_after = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(raw_after["orders"]["B"]["delivery"],
                         {"recipient": "  Wang  ", "delivered_on": " 2026-03-01 "})
        self.assertEqual(self.app.history("B")["events"][2]["result"]["delivery"],
                         {"recipient": "Wang", "delivered_on": "2026-03-01"})

    def test_recipient_mismatch_rejects_whole_request(self):
        self.app.restock("T", 20)
        self._ship("A", [{"sku": "T", "quantity": 1}], "DHL", "SAME")
        self._ship("B", [{"sku": "T", "quantity": 1}], "DHL", "SAME")
        self.app.confirm_delivery("A", "Wang", "2026-03-01")
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.confirm_shipment_delivery("DHL", "SAME", "Li", "2026-03-01")
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.get("A")["delivery"]["recipient"], "Wang")
        self.assertEqual(self.app.get("B")["status"], "shipped")
        self.assertNotIn("delivery", self.app.get("B"))
        self.assertEqual([(e["sequence"], e["action"])
                          for e in self.app.history("B")["events"]],
                         [(1, "place"), (2, "ship")])

    def test_date_mismatch_rejects_whole_request(self):
        self.app.restock("T", 10)
        self._ship("A", [{"sku": "T", "quantity": 1}], "DHL", "SAME")
        self.app.confirm_delivery("A", "Wang", "2026-03-01")
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.confirm_shipment_delivery("DHL", "SAME", "Wang", "2026-03-02")
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_bad_stored_delivery_rejects_whole_request(self):
        self.app.restock("T", 20)
        self._ship("GOOD", [{"sku": "T", "quantity": 1}], "DHL", "SAME")
        base = {"order_id": "BAD", "status": "delivered",
                "lines": [{"sku": "T", "quantity": 1, "unit_price_cents": 100,
                           "subtotal_cents": 100}],
                "total_cents": 100,
                "shipment": {"carrier": "DHL", "tracking_no": "SAME"}}
        variants = [
            None, "sealed", {},
            {"recipient": "Wang"},
            {"delivered_on": "2026-03-01"},
            {"recipient": "   ", "delivered_on": "2026-03-01"},
            {"recipient": 5, "delivered_on": "2026-03-01"},
            {"recipient": "Wang", "delivered_on": None},
            {"recipient": "Wang", "delivered_on": "2026-02-29"},
            {"recipient": "Wang", "delivered_on": "2026/03/01"},
        ]
        for index, delivery in enumerate(variants):
            data = json.loads(self.app.path.read_text(encoding="utf-8"))
            order = json.loads(json.dumps(base))
            order["order_id"] = "BAD" + str(index)
            if delivery is not None:
                order["delivery"] = delivery
            data["orders"][order["order_id"]] = order
            self._write_raw(data)
            raw = (self.root / "data.json").read_bytes()
            with self.assertRaises(ValueError):
                OrderDesk(self.root).confirm_shipment_delivery(
                    "DHL", "SAME", "Wang", "2026-03-01")
            # The shipped order on the same shipment is not signed for.
            self.assertEqual((self.root / "data.json").read_bytes(), raw)
            self.assertEqual(OrderDesk(self.root).get("GOOD")["status"], "shipped")

    def test_bad_delivery_on_another_shipment_does_not_block(self):
        self.app.restock("T", 10)
        self._ship("A", [{"sku": "T", "quantity": 1}], "DHL", "SAME")
        order = {"order_id": "OLD", "status": "delivered",
                 "lines": [{"sku": "T", "quantity": 1, "unit_price_cents": 100,
                            "subtotal_cents": 100}],
                 "total_cents": 100,
                 "shipment": {"carrier": "UPS", "tracking_no": "OTHER"},
                 "delivery": "sealed"}
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        data["orders"]["OLD"] = order
        self._write_raw(data)
        result = OrderDesk(self.root).confirm_shipment_delivery(
            "DHL", "SAME", "Wang", "2026-03-01")
        self.assertEqual([o["order_id"] for o in result["orders"]], ["A"])

    def test_history_events_keep_complete_and_legacy_orders_start_at_one(self):
        self.app.restock("T", 20)
        self._ship("NEW", [{"sku": "T", "quantity": 1}], "DHL", "SAME")
        legacy = {"order_id": "OLD", "status": "shipped",
                  "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100,
                             "subtotal_cents": 200}],
                  "total_cents": 200,
                  "shipment": {"carrier": "DHL", "tracking_no": "SAME"}}
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        data["orders"]["OLD"] = legacy
        self._write_raw(data)
        app = OrderDesk(self.root)
        app.confirm_shipment_delivery("DHL", "SAME", "Wang", "2026-03-01")
        new_history = app.history("NEW")
        self.assertTrue(new_history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in new_history["events"]],
                         [(1, "place"), (2, "ship"), (3, "confirm-delivery")])
        self.assertEqual(new_history["events"][2]["result"], app.get("NEW"))
        old_history = app.history("OLD")
        self.assertFalse(old_history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in old_history["events"]],
                         [(1, "confirm-delivery")])
        self.assertEqual(old_history["events"][0]["result"], app.get("OLD"))

    def test_persistence_get_history_and_worklist_after_reopen(self):
        self.app.restock("T", 10)
        self._ship("O1", [{"sku": "T", "quantity": 1}], "DHL", "SAME")
        self.app.confirm_shipment_delivery("DHL", "SAME", "Wang", "2026-03-01")
        reopened = OrderDesk(self.root)
        order = reopened.get("O1")
        self.assertEqual(order["status"], "delivered")
        self.assertEqual(order["delivery"],
                         {"recipient": "Wang", "delivered_on": "2026-03-01"})
        self.assertEqual(reopened.history("O1")["status"], "delivered")
        self.assertEqual(reopened.history("O1")["events"][-1]["action"],
                         "confirm-delivery")
        # No more deliver task on the fulfillment worklist.
        entries = reopened.order_worklist(stage="all")
        entry = next(e for e in entries if e["order_id"] == "O1")
        self.assertEqual(entry["tasks"], [])

    def test_no_stock_or_side_effects_and_returns_do_not_block(self):
        self.app.restock("T", 20)
        self._ship("O1", [{"sku": "T", "quantity": 4}], "DHL", "SAME")
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        raw_before = json.loads(self.app.path.read_text(encoding="utf-8"))
        stock_events_before = self.app.stock_history("T")["events"]
        self.app.confirm_shipment_delivery("DHL", "SAME", "Wang", "2026-03-01")
        raw_after = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(raw_after["inventory"], raw_before["inventory"])
        self.assertEqual(raw_after.get("reservations", {}),
                         raw_before.get("reservations", {}))
        self.assertEqual(raw_after["returns"], raw_before["returns"])
        self.assertEqual(self.app.stock_history("T")["events"], stock_events_before)
        records = self.app.get_returns("O1")
        self.assertEqual([r["return_id"] for r in records["records"]], ["R1"])
        self.assertEqual(records["remaining"][0]["quantity"], 3)

    def test_single_confirm_delivery_still_rejects_repeats(self):
        self.app.restock("T", 10)
        self._ship("O1", [{"sku": "T", "quantity": 1}], "DHL", "X-1")
        self.app.confirm_delivery("O1", "Wang", "2026-03-01")
        for args in (("O1", "Wang", "2026-03-01"), ("O1", "Li", "2026-03-02")):
            with self.assertRaises(ValueError):
                self.app.confirm_delivery(*args)

    def test_cli_success_failure_and_independent_array(self):
        self.app.restock("T", 20)
        self._ship("A", [{"sku": "T", "quantity": 1}], "DHL", "SAME")
        self._ship("B", [{"sku": "T", "quantity": 1}], "DHL", "SAME")
        self._ship("C", [{"sku": "T", "quantity": 1}], "UPS", "OTHER")
        payload = self.root / "p.json"
        payload.write_text(json.dumps({"carrier": " DHL ", "tracking_no": " SAME ",
                                       "recipient": " Wang ",
                                       "delivered_on": " 2026-03-01 "}),
                           encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "confirm-shipment-delivery", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual(set(result), {"carrier", "tracking_no", "orders"})
        self.assertEqual([o["order_id"] for o in result["orders"]], ["A", "B"])
        # Failures print an error object to stderr and exit 2.
        for bad in (
            {"carrier": 123, "tracking_no": "SAME", "recipient": "Wang",
             "delivered_on": "2026-03-01"},
            {"carrier": "DHL", "tracking_no": "SAME", "recipient": "Wang",
             "delivered_on": "2026-02-29"},
            {"carrier": "UPS", "tracking_no": "MISSING", "recipient": "Wang",
             "delivered_on": "2026-03-01"},
        ):
            payload.write_text(json.dumps(bad), encoding="utf-8")
            failed = subprocess.run(
                [sys.executable, "-m", "order_desk", "--root", str(self.root),
                 "confirm-shipment-delivery", str(payload)],
                text=True, capture_output=True)
            self.assertEqual(failed.returncode, 2, failed.stdout)
            self.assertIn("error", json.loads(failed.stderr))
        # Outer array rows run independently: C signs, the unknown shipment fails
        # afterwards without rolling C back.
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([
            {"carrier": "UPS", "tracking_no": "OTHER", "recipient": "Li",
             "delivered_on": "2026-03-02"},
            {"carrier": "UPS", "tracking_no": "NONE", "recipient": "Li",
             "delivered_on": "2026-03-02"},
        ]), encoding="utf-8")
        run = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "confirm-shipment-delivery", str(batch)],
            text=True, capture_output=True)
        self.assertEqual(run.returncode, 2, run.stdout)
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.get("A")["status"], "delivered")
        self.assertEqual(reopened.get("B")["status"], "delivered")
        self.assertEqual(reopened.get("C")["status"], "delivered")
        self.assertEqual(reopened.get("C")["delivery"],
                         {"recipient": "Li", "delivered_on": "2026-03-02"})


if __name__ == "__main__":
    unittest.main()
