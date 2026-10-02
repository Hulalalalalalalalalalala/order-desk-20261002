import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk

class CorrectShipmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)

    def _shipped(self, order_id="O1", carrier="DHL", tracking_no="X-1"):
        self.app.restock("T", 10)
        self.app.place(order_id, [{"sku": "T", "quantity": 2}])
        return self.app.ship(order_id, carrier, tracking_no)

    def test_correct_returns_before_after_and_updates_views(self):
        self._shipped()
        result = self.app.correct_shipment(
            " O1 ",
            {"carrier": " DHL ", "tracking_no": " X-1 ", "note": "ignored"},
            {"carrier": " UPS ", "tracking_no": " U-9 ", "extra": 1},
        )
        self.assertEqual(result, {
            "order_id": "O1",
            "before": {"carrier": "DHL", "tracking_no": "X-1"},
            "after": {"carrier": "UPS", "tracking_no": "U-9"},
        })
        # get and list show the latest shipment; everything else is preserved.
        order = self.app.get("O1")
        self.assertEqual(order["shipment"], {"carrier": "UPS", "tracking_no": "U-9"})
        self.assertEqual(order["status"], "shipped")
        self.assertEqual(self.app.list_orders()[0]["shipment"], order["shipment"])
        # Persisted together with the history event.
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.get("O1")["shipment"], {"carrier": "UPS", "tracking_no": "U-9"})
        history = reopened.history("O1")
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                         [(1, "place"), (2, "ship"), (3, "correct-shipment")])
        event = history["events"][2]
        self.assertEqual(set(event), {"sequence", "action", "result"})
        self.assertEqual(event["result"], result)
        self.assertTrue(history["complete"])
        # Older snapshots are not rewritten.
        self.assertEqual(history["events"][1]["result"]["shipment"],
                         {"carrier": "DHL", "tracking_no": "X-1"})

    def test_partial_change_of_only_one_field(self):
        self._shipped()
        result = self.app.correct_shipment(
            "O1",
            {"carrier": "DHL", "tracking_no": "X-1"},
            {"carrier": "DHL", "tracking_no": "X-2"},
        )
        self.assertEqual(result["after"], {"carrier": "DHL", "tracking_no": "X-2"})
        self.assertEqual(self.app.get("O1")["shipment"], {"carrier": "DHL", "tracking_no": "X-2"})

    def test_delivered_order_and_existing_returns_do_not_block(self):
        self._shipped()
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.confirm_delivery("O1", "Wang", "2026-03-01")
        result = self.app.correct_shipment(
            "O1",
            {"carrier": "DHL", "tracking_no": "X-1"},
            {"carrier": "FedEx", "tracking_no": "F-1"},
        )
        self.assertEqual(result["after"], {"carrier": "FedEx", "tracking_no": "F-1"})
        order = self.app.get("O1")
        self.assertEqual(order["status"], "delivered")
        self.assertEqual(order["delivery"], {"recipient": "Wang", "delivered_on": "2026-03-01"})
        self.assertEqual(order["shipment"], {"carrier": "FedEx", "tracking_no": "F-1"})
        self.assertEqual([r["return_id"] for r in self.app.get_returns("O1")["records"]], ["R1"])
        self.assertEqual([e["action"] for e in self.app.history("O1")["events"]],
                         ["place", "ship", "record-return", "confirm-delivery", "correct-shipment"])

    def test_unchanged_target_returns_result_without_writing(self):
        self._shipped()
        before = self.app.path.read_bytes()
        result = self.app.correct_shipment(
            "O1",
            {"carrier": "DHL", "tracking_no": "X-1"},
            {"carrier": " DHL ", "tracking_no": " X-1 "},
        )
        self.assertEqual(result, {
            "order_id": "O1",
            "before": {"carrier": "DHL", "tracking_no": "X-1"},
            "after": {"carrier": "DHL", "tracking_no": "X-1"},
        })
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual([e["action"] for e in self.app.history("O1")["events"]],
                         ["place", "ship"])

    def test_correction_changes_no_stock_or_stock_history(self):
        self._shipped()
        before_stock = self.app.stock("T")
        before_events = self.app.stock_history("T")["events"]
        raw_before = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.app.correct_shipment(
            "O1",
            {"carrier": "DHL", "tracking_no": "X-1"},
            {"carrier": "UPS", "tracking_no": "U-1"},
        )
        self.assertEqual(self.app.stock("T"), before_stock)
        self.assertEqual(self.app.stock_history("T")["events"], before_events)
        raw_after = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(raw_after["inventory"], raw_before["inventory"])
        self.assertEqual(raw_after.get("reservations", {}), raw_before.get("reservations", {}))
        self.assertEqual(raw_after.get("stock_history", {}), raw_before.get("stock_history", {}))

    def test_expected_mismatch_is_rejected_even_when_target_matches(self):
        self._shipped()
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.correct_shipment(
                "O1",
                {"carrier": "UPS", "tracking_no": "X-1"},
                {"carrier": "DHL", "tracking_no": "X-1"},
            )
        with self.assertRaises(ValueError):
            self.app.correct_shipment(
                "O1",
                {"carrier": "DHL", "tracking_no": "x-1"},  # case sensitive
                {"carrier": "UPS", "tracking_no": "U-1"},
            )
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.get("O1")["shipment"], {"carrier": "DHL", "tracking_no": "X-1"})
        self.assertEqual([e["action"] for e in self.app.history("O1")["events"]],
                         ["place", "ship"])

    def test_invalid_inputs_are_rejected_without_consuming_sequence(self):
        self._shipped()
        self.app.place("O2", [{"sku": "C", "quantity": 1}])
        self.app.place("O3", [{"sku": "C", "quantity": 1}])
        self.app.cancel("O3")
        before = self.app.path.read_bytes()
        good_expected = {"carrier": "DHL", "tracking_no": "X-1"}
        good_target = {"carrier": "UPS", "tracking_no": "U-1"}
        # Bad identifiers and bad info objects.
        for bad in (None, 123, b"O1", ["O1"], {"x": 1}, "", "   "):
            with self.assertRaises(ValueError):
                self.app.correct_shipment(bad, good_expected, good_target)
        for bad in (None, 123, "DHL", ["carrier"], {"carrier": "DHL"},
                    {"tracking_no": "X-1"}, {"carrier": "", "tracking_no": "X-1"},
                    {"carrier": "DHL", "tracking_no": "  "},
                    {"carrier": 1, "tracking_no": "X-1"},
                    {"carrier": "DHL", "tracking_no": None}):
            with self.assertRaises(ValueError):
                self.app.correct_shipment("O1", bad, good_target)
            with self.assertRaises(ValueError):
                self.app.correct_shipment("O1", good_expected, bad)
        # Unknown order and wrong status.
        with self.assertRaises(ValueError):
            self.app.correct_shipment("missing", good_expected, good_target)
        with self.assertRaises(ValueError):
            self.app.correct_shipment("O2", good_expected, good_target)  # placed
        with self.assertRaises(ValueError):
            self.app.correct_shipment("O3", good_expected, good_target)  # cancelled
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual([e["action"] for e in self.app.history("O1")["events"]],
                         ["place", "ship"])

    def test_legacy_shipped_order_without_shipment_is_rejected(self):
        order = {"order_id": "OLD", "status": "shipped",
                 "lines": [{"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100}],
                 "total_cents": 100}
        broken = {"order_id": "BAD", "status": "shipped",
                  "lines": [{"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100}],
                  "total_cents": 100, "shipment": {"carrier": "  ", "tracking_no": "X"}}
        data = {"products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
                "orders": {"OLD": order, "BAD": broken}}
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        before = app.path.read_bytes()
        with self.assertRaises(ValueError):
            app.correct_shipment("OLD", {"carrier": "DHL", "tracking_no": "X"},
                                 {"carrier": "UPS", "tracking_no": "U"})
        with self.assertRaises(ValueError):
            app.correct_shipment("BAD", {"carrier": "DHL", "tracking_no": "X"},
                                 {"carrier": "UPS", "tracking_no": "U"})
        self.assertEqual(app.path.read_bytes(), before)

    def test_legacy_order_with_history_gap_starts_at_one_and_incomplete(self):
        order = {"order_id": "OLD", "status": "shipped",
                 "lines": [{"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100}],
                 "total_cents": 100,
                 "shipment": {"carrier": "DHL", "tracking_no": "X-1"}}
        data = {"products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
                "orders": {"OLD": order}}
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        result = app.correct_shipment("OLD", {"carrier": "DHL", "tracking_no": "X-1"},
                                      {"carrier": "UPS", "tracking_no": "U-1"})
        history = app.history("OLD")
        self.assertFalse(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                         [(1, "correct-shipment")])
        self.assertEqual(history["events"][0]["result"], result)
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.get("OLD")["shipment"], {"carrier": "UPS", "tracking_no": "U-1"})
        self.assertEqual(reopened.history("OLD")["events"], history["events"])

    def test_tracking_numbers_need_not_be_unique_across_orders(self):
        self._shipped("O1")
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        self.app.ship("O2", "DHL", "X-1")  # same tracking number as O1
        result = self.app.correct_shipment(
            "O2",
            {"carrier": "DHL", "tracking_no": "X-1"},
            {"carrier": "UPS", "tracking_no": "X-1"},
        )
        self.assertEqual(result["after"], {"carrier": "UPS", "tracking_no": "X-1"})
        self.assertEqual(self.app.get("O1")["shipment"], {"carrier": "DHL", "tracking_no": "X-1"})

    def test_corrected_order_still_rejects_reship_and_accepts_returns(self):
        self._shipped()
        self.app.correct_shipment("O1", {"carrier": "DHL", "tracking_no": "X-1"},
                                  {"carrier": "UPS", "tracking_no": "U-1"})
        with self.assertRaises(ValueError):
            self.app.ship("O1", "DHL", "9")
        with self.assertRaises(ValueError):
            self.app.ship_batch([{"order_id": "O1", "carrier": "DHL", "tracking_no": "9"}])
        record = self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.assertEqual(record["return_id"], "R1")

    def test_cli_correct_success_failure_and_array_partial_failure(self):
        self.app.restock("T", 10)
        self.app.place("A", [{"sku": "T", "quantity": 1}])
        self.app.place("B", [{"sku": "T", "quantity": 1}])
        self.app.ship("A", "DHL", "A-1")
        self.app.ship("B", "DHL", "B-1")
        payload = self.root / "correct.json"
        payload.write_text(json.dumps({
            "order_id": " A ",
            "expected_shipment": {"carrier": " DHL ", "tracking_no": " A-1 "},
            "shipment": {"carrier": "UPS", "tracking_no": "A-2"},
        }), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "correct-shipment", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual(result, {
            "order_id": "A",
            "before": {"carrier": "DHL", "tracking_no": "A-1"},
            "after": {"carrier": "UPS", "tracking_no": "A-2"},
        })
        # Repeating with the old expected values now fails with exit 2.
        failed = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "correct-shipment", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2)
        self.assertIn("error", json.loads(failed.stderr))
        # Array input runs rows independently: B succeeds, the bad row stops the rest.
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([
            {"order_id": "B",
             "expected_shipment": {"carrier": "DHL", "tracking_no": "B-1"},
             "shipment": {"carrier": "UPS", "tracking_no": "B-2"}},
            {"order_id": "missing",
             "expected_shipment": {"carrier": "DHL", "tracking_no": "X"},
             "shipment": {"carrier": "UPS", "tracking_no": "Y"}},
            {"order_id": "A",
             "expected_shipment": {"carrier": "UPS", "tracking_no": "A-2"},
             "shipment": {"carrier": "FedEx", "tracking_no": "A-3"}},
        ]), encoding="utf-8")
        stopped = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "correct-shipment", str(batch)],
            text=True, capture_output=True)
        self.assertEqual(stopped.returncode, 2, stopped.stdout)
        self.assertIn("error", json.loads(stopped.stderr))
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.get("B")["shipment"], {"carrier": "UPS", "tracking_no": "B-2"})
        # The row after the failure never ran.
        self.assertEqual(reopened.get("A")["shipment"], {"carrier": "UPS", "tracking_no": "A-2"})

if __name__ == "__main__":
    unittest.main()
