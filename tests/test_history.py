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
        self.app.restock("T", 10)
        self.app.restock("C", 10)

    def place_ship(self, order_id="O1", lines=None):
        order = self.app.place(order_id, lines or [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])
        shipped = self.app.ship(order_id, "DHL", "X-1")
        return order, shipped

    def test_full_life_events_in_order_with_snapshots(self):
        order, shipped = self.place_ship()
        r1 = self.app.record_return("O1", "R2", [{"sku": "C", "quantity": 1}])
        r2 = self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}, {"sku": "T", "quantity": 1}])
        history = self.app.history("O1")
        self.assertEqual(history["order_id"], "O1")
        self.assertEqual(history["status"], "shipped")
        self.assertTrue(history["complete"])
        actions = [e["action"] for e in history["events"]]
        self.assertEqual(actions, ["place", "ship", "record-return", "record-return"])
        self.assertEqual([e["sequence"] for e in history["events"]], [1, 2, 3, 4])
        place_event = history["events"][0]
        self.assertEqual(set(place_event), {"sequence", "action", "result"})
        self.assertEqual(place_event["result"]["lines"], order["lines"])
        self.assertEqual(place_event["result"]["total_cents"], 400)
        self.assertEqual(place_event["result"]["status"], "placed")
        self.assertNotIn("shipment", place_event["result"])
        ship_event = history["events"][1]
        self.assertEqual(ship_event["result"]["shipment"], shipped["shipment"])
        self.assertEqual(ship_event["result"]["status"], "shipped")
        # Returns appear in registration order even though ids were submitted reversed.
        self.assertEqual(history["events"][2]["result"], r1)
        self.assertEqual(history["events"][3]["result"], r2)
        self.assertEqual(r2["lines"], [{"sku": "T", "quantity": 2}])

    def test_cancel_event(self):
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        cancelled = self.app.cancel("O1")
        history = self.app.history("O1")
        self.assertEqual(history["status"], "cancelled")
        self.assertTrue(history["complete"])
        self.assertEqual([e["action"] for e in history["events"]], ["place", "cancel"])
        self.assertEqual(history["events"][1]["result"]["status"], "cancelled")
        self.assertEqual(cancelled["status"], "cancelled")

    def test_failures_add_no_events_and_do_not_consume_sequence(self):
        self.place_ship()
        # Duplicate place, ship twice, return over quota, return for cancelled order.
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        for bad in (
            lambda: self.app.place("O1", [{"sku": "T", "quantity": 1}]),
            lambda: self.app.ship("O1", "DHL", "X-2"),
            lambda: self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 99}]),
            lambda: self.app.record_return("O1", "R1", [{"sku": "Z", "quantity": 1}]),
            lambda: self.app.cancel("O1"),
        ):
            with self.assertRaises(ValueError):
                bad()
        self.app.cancel("O2")  # O2 was still placed
        h1 = self.app.history("O1")
        self.assertEqual([e["sequence"] for e in h1["events"]], [1, 2])
        h2 = self.app.history("O2")
        self.assertEqual([e["sequence"] for e in h2["events"]], [1, 2])

    def test_snapshots_are_not_rewritten_by_later_operations(self):
        self.place_ship()
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        reopened = OrderDesk(self.root)
        history = reopened.history("O1")
        # Mutating returned data must not change stored history.
        history["events"][0]["result"]["total_cents"] = -1
        history["events"][1]["result"]["shipment"]["carrier"] = "HACK"
        history["events"].append({"sequence": 99, "action": "cancel", "result": {}})
        again = OrderDesk(self.root).history("O1")
        self.assertEqual(again["events"][0]["result"]["total_cents"], 400)
        self.assertEqual(again["events"][1]["result"]["shipment"]["carrier"], "DHL")
        self.assertEqual(len(again["events"]), 3)

    def test_orders_have_independent_sequences(self):
        self.place_ship("O1")
        self.place_ship("O2")
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O2", "R2", [{"sku": "T", "quantity": 1}])
        self.assertEqual([e["sequence"] for e in self.app.history("O1")["events"]], [1, 2, 3])
        self.assertEqual([e["sequence"] for e in self.app.history("O2")["events"]], [1, 2, 3])

    def test_persistence_across_reopen(self):
        self.place_ship()
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        reopened = OrderDesk(self.root)
        history = reopened.history("O1")
        self.assertEqual(history["status"], "shipped")
        self.assertEqual([e["action"] for e in history["events"]], ["place", "ship", "record-return"])
        self.assertEqual(history["events"][2]["result"]["return_id"], "R1")

    def test_legacy_order_without_history_has_empty_events(self):
        # Hand-craft an old data.json: shipped order with returns, no history section.
        legacy = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "orders": {"OLD": {"order_id": "OLD", "status": "shipped",
                               "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200}],
                               "total_cents": 200,
                               "shipment": {"carrier": "DHL", "tracking_no": "9"}}},
            "returns": {"OLD": [{"order_id": "OLD", "return_id": "RX",
                                 "lines": [{"sku": "T", "quantity": 1}]}]},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "data.json").write_text(json.dumps(legacy), encoding="utf-8")
        app = OrderDesk(self.root)
        history = app.history("OLD")
        self.assertEqual(history["status"], "shipped")
        self.assertFalse(history["complete"])
        self.assertEqual(history["events"], [])

    def test_legacy_order_new_events_start_at_one_and_remain_incomplete(self):
        legacy = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "orders": {"OLD": {"order_id": "OLD", "status": "placed",
                               "lines": [{"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100}],
                               "total_cents": 100}},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "data.json").write_text(json.dumps(legacy), encoding="utf-8")
        app = OrderDesk(self.root)
        app.cancel("OLD")
        history = app.history("OLD")
        self.assertFalse(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]], [(1, "cancel")])

        legacy2 = dict(legacy)
        legacy2["orders"] = {"OLD2": json.loads(json.dumps(legacy["orders"]["OLD"]))}
        legacy2["orders"]["OLD2"]["order_id"] = "OLD2"
        (self.root / "data.json").write_text(json.dumps(legacy2), encoding="utf-8")
        app = OrderDesk(self.root)
        app.ship("OLD2", "DHL", "1")
        app.record_return("OLD2", "R1", [{"sku": "T", "quantity": 1}])
        history = app.history("OLD2")
        self.assertFalse(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                         [(1, "ship"), (2, "record-return")])

    def test_history_validation(self):
        self.place_ship()
        for bad in (None, 123, 1.5, b"O1", ["O1"], {"x": 1}, True, "", "   ", "\t\n"):
            with self.assertRaises(ValueError):
                self.app.history(bad)
        with self.assertRaises(ValueError):
            self.app.history("unknown")
        # Case sensitive.
        with self.assertRaises(ValueError):
            self.app.history("o1")
        # Surrounding whitespace is accepted on lookup (trimmed like other ids).
        self.assertEqual(self.app.history("  O1  ")["order_id"], "O1")

    def test_history_query_creates_nothing(self):
        empty = self.root / "empty"
        app = OrderDesk(empty)
        with self.assertRaises(ValueError):
            app.history("O1")
        self.assertFalse(empty.exists())

    def test_history_document_shape_only_contains_expected_keys(self):
        self.place_ship()
        history = self.app.history("O1")
        self.assertEqual(set(history), {"order_id", "status", "complete", "events"})
        for event in history["events"]:
            self.assertEqual(set(event), {"sequence", "action", "result"})

    def test_array_batch_failure_keeps_events_of_prior_successes(self):
        self.app.add_product("U", "Unrestocked", 50)
        input_path = self.root / "batch.json"
        input_path.write_text(json.dumps([
            {"order_id": "B1", "lines": [{"sku": "U", "quantity": 1}]},
            {"order_id": "B2", "lines": [{"sku": "U", "quantity": 1}]},
            {"order_id": "B1", "lines": [{"sku": "U", "quantity": 1}]},
        ]), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "place", str(input_path)],
            cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("error", json.loads(proc.stderr))
        # The duplicate (third item) failed and stopped the batch; first two kept their events.
        self.assertEqual([e["action"] for e in self.app.history("B1")["events"]], ["place"])
        self.assertEqual([e["action"] for e in self.app.history("B2")["events"]], ["place"])

    def test_cli_history_success_and_failure(self):
        self.place_ship()
        query = self.root / "q.json"
        query.write_text(json.dumps({"order_id": "O1"}), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "history", str(query)],
            cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        value = json.loads(proc.stdout)
        self.assertEqual([e["action"] for e in value["events"]], ["place", "ship"])
        self.assertTrue(value["complete"])

        query.write_text(json.dumps({"order_id": "nope"}), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "history", str(query)],
            cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("error", json.loads(proc.stderr))


if __name__ == "__main__":
    unittest.main()
