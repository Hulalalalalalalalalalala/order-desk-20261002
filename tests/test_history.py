import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk

class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)

    def _full_lifecycle(self):
        self.app.restock("T", 10)
        order = self.app.place("O1", [{"sku": "T", "quantity": 2}, {"sku": "T", "quantity": 1}])
        self.app.ship("O1", " DHL ", " X-1 ")
        record2 = self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        record1 = self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        return order, record2, record1

    def test_history_full_lifecycle_order_and_snapshots(self):
        order, record2, record1 = self._full_lifecycle()
        history = self.app.history("O1")
        self.assertEqual(set(history), {"order_id", "status", "complete", "events"})
        self.assertEqual(history["order_id"], "O1")
        self.assertEqual(history["status"], "shipped")
        self.assertTrue(history["complete"])
        events = history["events"]
        self.assertEqual([e["sequence"] for e in events], [1, 2, 3, 4])
        self.assertEqual([e["action"] for e in events], ["place", "ship", "record-return", "record-return"])
        for event in events:
            self.assertEqual(set(event), {"sequence", "action", "result"})
        # Creation snapshot keeps deal lines and amount.
        self.assertEqual(events[0]["result"], order)
        # Ship snapshot keeps the shipment.
        self.assertEqual(events[1]["result"]["shipment"], {"carrier": "DHL", "tracking_no": "X-1"})
        self.assertEqual(events[1]["result"]["status"], "shipped")
        # Return snapshots are the merged records, in registration order even
        # though R2 was registered before R1.
        self.assertEqual(events[2]["result"], record2)
        self.assertEqual(events[3]["result"], record1)

    def test_snapshots_are_not_mutated_by_later_operations(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.ship("O1", "DHL", "1")
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        history = self.app.history("O1")
        place_result = history["events"][0]["result"]
        self.assertEqual(place_result["status"], "placed")
        self.assertNotIn("shipment", place_result)

    def test_failed_operations_add_no_event_and_consume_no_sequence(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.app.ship("O1", "DHL", "1")
        failures = (
            lambda: self.app.ship("O1", "DHL", "1"),
            lambda: self.app.ship("missing", "DHL", "1"),
            lambda: self.app.cancel("O1"),
            lambda: self.app.record_return("O1", "R1", [{"sku": "X", "quantity": 1}]),
            lambda: self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 99}]),
            lambda: self.app.record_return("missing", "R9", [{"sku": "T", "quantity": 1}]),
        )
        for action in failures:
            with self.assertRaises(ValueError):
                action()
        events = self.app.history("O1")["events"]
        self.assertEqual([(e["sequence"], e["action"]) for e in events],
                         [(1, "place"), (2, "ship")])

    def test_cancel_event(self):
        self.app.place("O1", [{"sku": "C", "quantity": 1}])
        cancelled = self.app.cancel("O1")
        history = self.app.history("O1")
        self.assertEqual(history["status"], "cancelled")
        self.assertEqual([e["action"] for e in history["events"]], ["place", "cancel"])
        self.assertEqual(history["events"][1]["result"], cancelled)

    def test_each_order_counts_its_own_sequences(self):
        self.app.restock("T", 10)
        self.app.place("A", [{"sku": "T", "quantity": 1}])
        self.app.place("B", [{"sku": "T", "quantity": 1}])
        self.app.ship("A", "DHL", "1")
        self.app.ship("B", "DHL", "2")
        self.app.record_return("A", "RA", [{"sku": "T", "quantity": 1}])
        self.assertEqual([e["sequence"] for e in self.app.history("A")["events"]], [1, 2, 3])
        self.assertEqual([e["sequence"] for e in self.app.history("B")["events"]], [1, 2])

    def test_history_persists_across_reopen(self):
        self._full_lifecycle()
        history = OrderDesk(self.root).history("O1")
        self.assertEqual([e["action"] for e in history["events"]],
                         ["place", "ship", "record-return", "record-return"])
        self.assertEqual([e["sequence"] for e in history["events"]], [1, 2, 3, 4])
        self.assertTrue(history["complete"])

    def test_history_input_validation(self):
        self.app.place("O1", [{"sku": "C", "quantity": 1}])
        for bad in (None, 123, 1.5, b"O1", ["O1"], {"x": 1}, "   ", "\t\n"):
            with self.assertRaises(ValueError):
                self.app.history(bad)
        with self.assertRaises(ValueError):
            self.app.history("unknown")
        # Surrounding whitespace is trimmed; ids are case sensitive.
        self.assertEqual(self.app.history("  O1  ")["order_id"], "O1")
        with self.assertRaises(ValueError):
            self.app.history("o1")

    def test_history_query_does_not_write(self):
        self.app.place("O1", [{"sku": "C", "quantity": 1}])
        raw = self.app.path.read_bytes()
        self.app.history("O1")
        self.app.history("  O1  ")
        self.assertEqual(self.app.path.read_bytes(), raw)

    def test_history_query_creates_no_file_for_unknown_order(self):
        empty = self.root / "empty"
        app = OrderDesk(empty)
        with self.assertRaises(ValueError):
            app.history("ghost")
        self.assertFalse(empty.exists())

    def _legacy_data(self, status, returns=None):
        order = {"order_id": "OLD", "status": status,
                 "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200}],
                 "total_cents": 200}
        if status == "shipped":
            order["shipment"] = {"carrier": "DHL", "tracking_no": "Z-9"}
        data = {"products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
                "orders": {"OLD": order}}
        if returns:
            data["returns"] = {"OLD": [
                {"order_id": "OLD", "return_id": rid, "lines": [{"sku": "T", "quantity": qty}]}
                for rid, qty in returns]}
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")

    def test_legacy_order_history_is_empty_and_not_fabricated(self):
        self._legacy_data("shipped", returns=[("L1", 1), ("L2", 1)])
        app = OrderDesk(self.root)
        history = app.history("OLD")
        self.assertEqual(history, {"order_id": "OLD", "status": "shipped", "complete": False, "events": []})
        # Querying changed nothing on disk.
        history_again = OrderDesk(self.root).history("OLD")
        self.assertEqual(history_again["events"], [])

    def test_legacy_placed_order_cancel_starts_history_at_one(self):
        self._legacy_data("placed")
        app = OrderDesk(self.root)
        app.cancel("OLD")
        history = app.history("OLD")
        self.assertFalse(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]], [(1, "cancel")])

    def test_legacy_shipped_order_return_starts_history_at_one(self):
        self._legacy_data("shipped", returns=[("L1", 1)])
        app = OrderDesk(self.root)
        record = app.record_return("OLD", "L2", [{"sku": "T", "quantity": 1}])
        history = app.history("OLD")
        self.assertFalse(history["complete"])
        self.assertEqual(len(history["events"]), 1)
        self.assertEqual(history["events"][0]["sequence"], 1)
        self.assertEqual(history["events"][0]["action"], "record-return")
        self.assertEqual(history["events"][0]["result"], record)
        # Pre-existing returns were not backfilled after reopening.
        self.assertEqual(OrderDesk(self.root).history("OLD")["events"][0]["result"]["return_id"], "L2")

    def test_cli_history_success_and_failure(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        payload = self.root / "h.json"
        payload.write_text(json.dumps({"order_id": " O1 "}), encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "history", str(payload)],
                            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual(set(result), {"order_id", "status", "complete", "events"})
        self.assertEqual(len(result["events"]), 1)
        for bad in ("unknown", "   "):
            payload.write_text(json.dumps({"order_id": bad}), encoding="utf-8")
            failed = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "history", str(payload)],
                                    text=True, capture_output=True)
            self.assertEqual(failed.returncode, 2, failed.stdout)
            self.assertIn("error", json.loads(failed.stderr))

    def test_cli_array_failure_keeps_earlier_events(self):
        self.app.restock("T", 5)
        self.app.place("A", [{"sku": "T", "quantity": 1}])
        self.app.place("B", [{"sku": "T", "quantity": 1}])
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([
            {"order_id": "A", "carrier": "DHL", "tracking_no": "1"},
            {"order_id": "missing", "carrier": "DHL", "tracking_no": "X"},
            {"order_id": "B", "carrier": "UPS", "tracking_no": "2"},
        ]), encoding="utf-8")
        run = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "ship", str(batch)],
                             text=True, capture_output=True)
        self.assertEqual(run.returncode, 2, run.stdout)
        self.assertEqual([e["action"] for e in OrderDesk(self.root).history("A")["events"]], ["place", "ship"])
        self.assertEqual([e["action"] for e in OrderDesk(self.root).history("B")["events"]], ["place"])

if __name__ == "__main__":
    unittest.main()
