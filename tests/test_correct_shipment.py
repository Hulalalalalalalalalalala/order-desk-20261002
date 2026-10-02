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
        self.app.restock("T", 5)
        self.app.place(order_id, [{"sku": "T", "quantity": 2}])
        self.app.ship(order_id, carrier, tracking_no)

    def test_correct_both_fields_returns_snapshots_and_persists(self):
        self._shipped()
        result = self.app.correct_shipment(
            "O1", {"carrier": " DHL ", "tracking_no": "X-1"},
            {"carrier": "UPS", "tracking_no": "Y-2", "extra": "ignored"},
        )
        self.assertEqual(set(result), {"order_id", "before", "after"})
        self.assertEqual(result, {"order_id": "O1",
                                  "before": {"carrier": "DHL", "tracking_no": "X-1"},
                                  "after": {"carrier": "UPS", "tracking_no": "Y-2"}})
        self.assertEqual(set(result["before"]), {"carrier", "tracking_no"})
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.get("O1")["shipment"], {"carrier": "UPS", "tracking_no": "Y-2"})
        self.assertEqual(reopened.list_orders()[0]["shipment"], {"carrier": "UPS", "tracking_no": "Y-2"})

    def test_correct_one_field_keeps_the_other(self):
        self._shipped()
        result = self.app.correct_shipment(
            "O1", {"carrier": "DHL", "tracking_no": "X-1"},
            {"carrier": "DHL", "tracking_no": "Y-2"},
        )
        self.assertEqual(result["after"], {"carrier": "DHL", "tracking_no": "Y-2"})
        result = self.app.correct_shipment(
            "O1", {"carrier": "DHL", "tracking_no": "Y-2"},
            {"carrier": "UPS", "tracking_no": "Y-2"},
        )
        self.assertEqual(result["before"], {"carrier": "DHL", "tracking_no": "Y-2"})
        self.assertEqual(result["after"], {"carrier": "UPS", "tracking_no": "Y-2"})
        self.assertEqual(self.app.get("O1")["shipment"], {"carrier": "UPS", "tracking_no": "Y-2"})

    def test_delivered_order_and_returns_are_kept(self):
        self._shipped()
        self.app.confirm_delivery("O1", "Ada", "2026-09-01")
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        order_before = self.app.get("O1")
        result = self.app.correct_shipment(
            "O1", {"carrier": "DHL", "tracking_no": "X-1"},
            {"carrier": "UPS", "tracking_no": "Y-2"},
        )
        self.assertEqual(result["after"], {"carrier": "UPS", "tracking_no": "Y-2"})
        order = self.app.get("O1")
        self.assertEqual(order["status"], "delivered")
        self.assertEqual(order["delivery"], {"recipient": "Ada", "delivered_on": "2026-09-01"})
        self.assertEqual(order["lines"], order_before["lines"])
        self.assertEqual(order["total_cents"], order_before["total_cents"])
        self.assertEqual(self.app.get_returns("O1")["records"][0]["return_id"], "R1")

    def test_matching_is_case_sensitive_and_tracking_need_not_be_unique(self):
        self._shipped("O1")
        self.app.place("O2", [{"sku": "C", "quantity": 1}])
        self.app.ship("O2", "DHL", "X-1")
        # Same carrier/tracking as another order is allowed.
        result = self.app.correct_shipment(
            "O1", {"carrier": "DHL", "tracking_no": "X-1"},
            {"carrier": "DHL", "tracking_no": "X-1"},
        )
        self.assertEqual(result["before"], result["after"])
        with self.assertRaises(ValueError):
            self.app.correct_shipment(
                "O1", {"carrier": "dhl", "tracking_no": "X-1"},
                {"carrier": "UPS", "tracking_no": "Y-2"},
            )

    def test_history_event_sequence_and_unchanged_old_snapshot(self):
        self._shipped()
        old_ship_snapshot = self.app.history("O1")["events"][1]["result"]
        self.app.correct_shipment(
            "O1", {"carrier": "DHL", "tracking_no": "X-1"},
            {"carrier": "UPS", "tracking_no": "Y-2"},
        )
        history = self.app.history("O1")
        self.assertTrue(history["complete"])
        events = history["events"]
        self.assertEqual([(e["sequence"], e["action"]) for e in events],
                         [(1, "place"), (2, "ship"), (3, "correct-shipment")])
        self.assertEqual(events[2]["result"],
                         {"order_id": "O1",
                          "before": {"carrier": "DHL", "tracking_no": "X-1"},
                          "after": {"carrier": "UPS", "tracking_no": "Y-2"}})
        self.assertEqual(set(events[2]), {"sequence", "action", "result"})
        # Old snapshots are untouched by the correction.
        self.assertEqual(old_ship_snapshot["shipment"], {"carrier": "DHL", "tracking_no": "X-1"})
        self.assertEqual(OrderDesk(self.root).history("O1")["events"][2]["action"], "correct-shipment")

    def test_legacy_order_starts_at_one_with_complete_false(self):
        order = {"order_id": "OLD", "status": "shipped",
                 "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200}],
                 "total_cents": 200, "shipment": {"carrier": "DHL", "tracking_no": "Z-9"}}
        data = {"products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
                "orders": {"OLD": order}}
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        app.correct_shipment("OLD", {"carrier": "DHL", "tracking_no": "Z-9"},
                             {"carrier": "UPS", "tracking_no": "Z-10"})
        history = app.history("OLD")
        self.assertFalse(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                         [(1, "correct-shipment")])

    def test_no_change_returns_result_but_writes_nothing(self):
        self._shipped()
        raw = self.app.path.read_bytes()
        result = self.app.correct_shipment(
            " O1 ", {"carrier": " DHL ", "tracking_no": " X-1 "},
            {"carrier": "DHL", "tracking_no": "X-1"},
        )
        self.assertEqual(result["order_id"], "O1")
        self.assertEqual(result["before"], result["after"])
        self.assertEqual(self.app.path.read_bytes(), raw)
        self.assertEqual([e["action"] for e in self.app.history("O1")["events"]], ["place", "ship"])

    def test_expected_mismatch_is_rejected_even_if_target_equals_current(self):
        self._shipped()
        raw = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.correct_shipment(
                "O1", {"carrier": "UPS", "tracking_no": "X-1"},
                {"carrier": "DHL", "tracking_no": "X-1"},
            )
        with self.assertRaises(ValueError):
            self.app.correct_shipment(
                "o1", {"carrier": "DHL", "tracking_no": "X-1"},
                {"carrier": "DHL", "tracking_no": "X-1"},
            )
        self.assertEqual(self.app.path.read_bytes(), raw)
        self.assertEqual(self.app.get("O1")["shipment"], {"carrier": "DHL", "tracking_no": "X-1"})
        self.assertEqual([e["action"] for e in self.app.history("O1")["events"]], ["place", "ship"])

    def test_invalid_arguments_and_states_are_rejected_without_changes(self):
        self._shipped()
        self.app.place("P1", [{"sku": "C", "quantity": 1}])
        self.app.place("Q1", [{"sku": "C", "quantity": 1}])
        self.app.cancel("Q1")
        info = {"carrier": "DHL", "tracking_no": "X-1"}
        for kwargs in (
            {"order_id": "missing", "expected_shipment": info, "shipment": info},
            {"order_id": "P1", "expected_shipment": info, "shipment": info},
            {"order_id": "Q1", "expected_shipment": info, "shipment": info},
            {"order_id": 3, "expected_shipment": info, "shipment": info},
            {"order_id": "  ", "expected_shipment": info, "shipment": info},
            {"order_id": None, "expected_shipment": info, "shipment": info},
            {"order_id": "O1", "expected_shipment": None, "shipment": info},
            {"order_id": "O1", "expected_shipment": [], "shipment": info},
            {"order_id": "O1", "expected_shipment": "DHL", "shipment": info},
            {"order_id": "O1", "expected_shipment": {"carrier": "DHL"}, "shipment": info},
            {"order_id": "O1", "expected_shipment": {"tracking_no": "X-1"}, "shipment": info},
            {"order_id": "O1", "expected_shipment": {"carrier": " ", "tracking_no": "X-1"}, "shipment": info},
            {"order_id": "O1", "expected_shipment": {"carrier": 5, "tracking_no": "X-1"}, "shipment": info},
            {"order_id": "O1", "expected_shipment": {"carrier": "DHL", "tracking_no": ""}, "shipment": info},
            {"order_id": "O1", "expected_shipment": info, "shipment": None},
            {"order_id": "O1", "expected_shipment": info, "shipment": []},
            {"order_id": "O1", "expected_shipment": info, "shipment": {"carrier": "UPS"}},
            {"order_id": "O1", "expected_shipment": info, "shipment": {"carrier": "UPS", "tracking_no": 9}},
            {"order_id": "O1", "expected_shipment": info, "shipment": {"carrier": "  ", "tracking_no": "Y"}},
        ):
            with self.assertRaises(ValueError):
                self.app.correct_shipment(**kwargs)
        events = self.app.history("O1")["events"]
        self.assertEqual([(e["sequence"], e["action"]) for e in events], [(1, "place"), (2, "ship")])
        self.assertEqual(self.app.get("O1")["shipment"], {"carrier": "DHL", "tracking_no": "X-1"})

    def test_missing_or_invalid_current_shipment_is_rejected(self):
        for shipment in (None, {"carrier": "DHL"}, {"carrier": " ", "tracking_no": "Z"}):
            temp = tempfile.TemporaryDirectory()
            self.addCleanup(temp.cleanup)
            root = Path(temp.name)
            order = {"order_id": "OLD", "status": "shipped",
                     "lines": [{"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100}],
                     "total_cents": 100}
            if shipment is not None:
                order["shipment"] = shipment
            data = {"products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
                    "orders": {"OLD": order}}
            root.mkdir(parents=True, exist_ok=True)
            path = OrderDesk(root).path
            path.write_text(json.dumps(data), encoding="utf-8")
            app = OrderDesk(root)
            with self.assertRaises(ValueError):
                app.correct_shipment("OLD", {"carrier": "DHL", "tracking_no": "Z"},
                                     {"carrier": "UPS", "tracking_no": "W"})

    def test_failure_creates_no_directory(self):
        empty = self.root / "empty"
        app = OrderDesk(empty)
        with self.assertRaises(ValueError):
            app.correct_shipment("ghost", {"carrier": "DHL", "tracking_no": "1"},
                                 {"carrier": "UPS", "tracking_no": "2"})
        self.assertFalse(empty.exists())

    def test_stock_reservations_and_stock_history_are_untouched(self):
        self.app.restock("T", 10)
        self.app.place("O2", [{"sku": "T", "quantity": 3}])
        self._shipped("O1")
        stock_events_before = self.app.stock_history("T")["events"]
        stock_before = self.app.stock("T")
        self.app.correct_shipment(
            "O1", {"carrier": "DHL", "tracking_no": "X-1"},
            {"carrier": "UPS", "tracking_no": "Y-2"},
        )
        self.assertEqual(self.app.stock("T"), stock_before)
        self.assertEqual(self.app.stock_history("T")["events"], stock_events_before)

    def test_ship_still_rejects_duplicate_after_correction(self):
        self._shipped()
        self.app.correct_shipment(
            "O1", {"carrier": "DHL", "tracking_no": "X-1"},
            {"carrier": "UPS", "tracking_no": "Y-2"},
        )
        with self.assertRaises(ValueError):
            self.app.ship("O1", "FedEx", "Z-3")
        self.assertEqual(self.app.get("O1")["shipment"], {"carrier": "UPS", "tracking_no": "Y-2"})

    def test_cli_success_failure_and_array_partial_failure(self):
        self._shipped("A")
        self._shipped("B")
        payload = self.root / "c.json"
        payload.write_text(json.dumps({
            "order_id": "A",
            "expected_shipment": {"carrier": "DHL", "tracking_no": "X-1"},
            "shipment": {"carrier": "UPS", "tracking_no": "A-2"},
        }), encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                             "correct-shipment", str(payload)], text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout)["after"], {"carrier": "UPS", "tracking_no": "A-2"})
        # Wrong original info fails with exit 2 and an error object on stderr.
        payload.write_text(json.dumps({
            "order_id": "A",
            "expected_shipment": {"carrier": "WRONG", "tracking_no": "X-1"},
            "shipment": {"carrier": "FedEx", "tracking_no": "A-3"},
        }), encoding="utf-8")
        failed = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                                 "correct-shipment", str(payload)], text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2, failed.stdout)
        self.assertIn("error", json.loads(failed.stderr))
        # Array input runs in order and stops at the first error; later rows never run.
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([
            {"order_id": "B",
             "expected_shipment": {"carrier": "DHL", "tracking_no": "X-1"},
             "shipment": {"carrier": "FedEx", "tracking_no": "B-2"}},
            {"order_id": "missing",
             "expected_shipment": {"carrier": "DHL", "tracking_no": "X-1"},
             "shipment": {"carrier": "FedEx", "tracking_no": "M"}},
            {"order_id": "B",
             "expected_shipment": {"carrier": "FedEx", "tracking_no": "B-2"},
             "shipment": {"carrier": "XXX", "tracking_no": "B-3"}},
        ]), encoding="utf-8")
        stopped = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                                  "correct-shipment", str(batch)], text=True, capture_output=True)
        self.assertEqual(stopped.returncode, 2, stopped.stdout)
        self.assertIn("error", json.loads(stopped.stderr))
        app = OrderDesk(self.root)
        self.assertEqual(app.get("B")["shipment"], {"carrier": "FedEx", "tracking_no": "B-2"})
        # The row after the failing one did not execute.
        self.assertEqual([e["action"] for e in app.history("B")["events"]].count("correct-shipment"), 1)

if __name__ == "__main__":
    unittest.main()
