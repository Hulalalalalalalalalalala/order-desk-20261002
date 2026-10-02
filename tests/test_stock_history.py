import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk

class StockHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)

    def test_unmanaged_product_has_complete_empty_history(self):
        self.assertEqual(self.app.stock_history("T"),
                         {"sku": "T", "complete": True, "events": []})

    def test_first_restock_starts_complete_history_from_unmanaged_view(self):
        self.app.restock("T", 10)
        history = self.app.stock_history("T")
        self.assertEqual(set(history), {"sku", "complete", "events"})
        self.assertTrue(history["complete"])
        self.assertEqual(len(history["events"]), 1)
        event = history["events"][0]
        self.assertEqual(set(event), {"sequence", "action", "reference_id", "before", "after"})
        self.assertEqual(event["sequence"], 1)
        self.assertEqual(event["action"], "restock")
        self.assertIsNone(event["reference_id"])
        self.assertEqual(event["before"], {"sku": "T", "on_hand": None, "reserved": 0, "available": None})
        self.assertEqual(event["after"], {"sku": "T", "on_hand": 10, "reserved": 0, "available": 10})

    def test_place_amend_cancel_ship_events_chain_snapshots(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.amend("O1", [{"sku": "T", "quantity": 5}])
        self.app.cancel("O1")
        self.app.place("O2", [{"sku": "T", "quantity": 3}])
        self.app.ship("O2", "DHL", "X-1")
        events = self.app.stock_history("T")["events"]
        self.assertEqual([e["sequence"] for e in events], [1, 2, 3, 4, 5, 6])
        self.assertEqual([e["action"] for e in events],
                         ["restock", "place", "amend", "cancel", "place", "ship"])
        self.assertEqual([e["reference_id"] for e in events],
                         [None, "O1", "O1", "O1", "O2", "O2"])
        # Snapshots chain: each after equals the next before.
        for previous, following in zip(events, events[1:]):
            self.assertEqual(previous["after"], following["before"])
        self.assertEqual(events[1]["after"], {"sku": "T", "on_hand": 10, "reserved": 2, "available": 8})
        # Amend records only the net change (2 -> 5), not release/re-reserve.
        self.assertEqual(events[2]["before"]["reserved"], 2)
        self.assertEqual(events[2]["after"]["reserved"], 5)
        self.assertEqual(events[3]["after"]["reserved"], 0)
        # Ship deducts both on_hand and reserved.
        self.assertEqual(events[5]["before"], {"sku": "T", "on_hand": 10, "reserved": 3, "available": 7})
        self.assertEqual(events[5]["after"], {"sku": "T", "on_hand": 7, "reserved": 0, "available": 7})

    def test_net_zero_amend_adds_no_event(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        # Same merged quantity: reservation is released and re-reserved.
        self.app.amend("O1", [{"sku": "T", "quantity": 1}, {"sku": "T", "quantity": 1}])
        events = self.app.stock_history("T")["events"]
        self.assertEqual([e["action"] for e in events], ["restock", "place"])

    def test_cancel_or_ship_without_reservation_adds_no_event(self):
        # C is never restocked, so orders hold no actual reservation.
        self.app.place("O1", [{"sku": "C", "quantity": 1}])
        self.app.place("O2", [{"sku": "C", "quantity": 1}])
        self.app.cancel("O1")
        self.app.ship("O2", "DHL", "1")
        self.assertEqual(self.app.stock_history("C"),
                         {"sku": "C", "complete": True, "events": []})

    def test_receive_return_and_count_stock_events(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.ship("O1", "DHL", "1")
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        self.app.receive_return("R1")
        self.app.count_stock("SC1", [{"sku": "T", "on_hand": 4}])
        events = self.app.stock_history("T")["events"]
        self.assertEqual([e["action"] for e in events],
                         ["restock", "place", "ship", "receive-return", "count-stock"])
        self.assertEqual(events[3]["reference_id"], "R1")
        self.assertEqual(events[3]["after"]["on_hand"], 5)
        self.assertEqual(events[4]["reference_id"], "SC1")
        self.assertEqual(events[4]["before"]["on_hand"], 5)
        self.assertEqual(events[4]["after"]["on_hand"], 4)

    def test_same_value_count_adds_no_event(self):
        self.app.restock("T", 5)
        self.app.count_stock("SC1", [{"sku": "T", "on_hand": 5}])
        events = self.app.stock_history("T")["events"]
        self.assertEqual([e["action"] for e in events], ["restock"])

    def test_checkout_cart_uses_place_action(self):
        self.app.restock("T", 5)
        self.app.save_cart("K1", [{"sku": "T", "quantity": 2}])
        self.app.checkout_cart("K1", "O1")
        events = self.app.stock_history("T")["events"]
        self.assertEqual(events[-1]["action"], "place")
        self.assertEqual(events[-1]["reference_id"], "O1")

    def test_ship_batch_records_per_order_in_request_order_and_chains(self):
        self.app.restock("T", 10)
        self.app.place("A", [{"sku": "T", "quantity": 2}])
        self.app.place("B", [{"sku": "T", "quantity": 3}])
        self.app.ship_batch([
            {"order_id": "B", "carrier": "DHL", "tracking_no": "1"},
            {"order_id": "A", "carrier": "UPS", "tracking_no": "2"},
        ])
        events = self.app.stock_history("T")["events"]
        ships = [e for e in events if e["action"] == "ship"]
        # Request order B then A, not sorted order.
        self.assertEqual([e["reference_id"] for e in ships], ["B", "A"])
        self.assertEqual(ships[0]["before"], {"sku": "T", "on_hand": 10, "reserved": 5, "available": 5})
        self.assertEqual(ships[0]["after"], ships[1]["before"])
        self.assertEqual(ships[1]["after"], {"sku": "T", "on_hand": 5, "reserved": 0, "available": 5})

    def test_rejected_ship_batch_leaves_no_events(self):
        self.app.restock("T", 10)
        self.app.place("A", [{"sku": "T", "quantity": 2}])
        self.app.place("B", [{"sku": "T", "quantity": 3}])
        self.app.cancel("B")
        with self.assertRaises(ValueError):
            self.app.ship_batch([
                {"order_id": "A", "carrier": "DHL", "tracking_no": "1"},
                {"order_id": "B", "carrier": "UPS", "tracking_no": "2"},
            ])
        events = self.app.stock_history("T")["events"]
        self.assertEqual([e["action"] for e in events], ["restock", "place", "place", "cancel"])

    def test_failed_operations_add_no_event_and_consume_no_sequence(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        failures = (
            lambda: self.app.restock("T", 0),
            lambda: self.app.restock("missing", 1),
            lambda: self.app.place("O2", [{"sku": "T", "quantity": 99}]),
            lambda: self.app.amend("O1", [{"sku": "T", "quantity": 99}]),
            lambda: self.app.ship("missing", "DHL", "1"),
            lambda: self.app.count_stock("SC1", [{"sku": "T", "on_hand": 0}]),
        )
        for action in failures:
            with self.assertRaises(ValueError):
                action()
        events = self.app.stock_history("T")["events"]
        self.assertEqual([(e["sequence"], e["action"]) for e in events],
                         [(1, "restock"), (2, "place")])

    def test_paused_product_still_records_events(self):
        self.app.restock("T", 5)
        self.app.set_product_enabled("T", False)
        self.app.restock("T", 3)
        events = self.app.stock_history("T")["events"]
        self.assertEqual([e["action"] for e in events], ["restock", "restock"])
        self.assertEqual(events[1]["after"]["on_hand"], 8)

    def test_history_persists_across_reopen_and_snapshots_stay_frozen(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        before = OrderDesk(self.root).stock_history("T")
        self.app.restock("T", 5)
        self.app.ship("O1", "DHL", "1")
        after = OrderDesk(self.root).stock_history("T")
        self.assertEqual([e["sequence"] for e in after["events"]], [1, 2, 3, 4])
        # The first two events are byte-identical to the earlier read.
        self.assertEqual(after["events"][:2], before["events"])
        self.assertEqual(after["events"][0]["after"]["on_hand"], 10)

    def test_legacy_managed_product_without_history(self):
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "inventory": {"T": {"on_hand": 7, "reserved": 0}},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        self.assertEqual(app.stock_history("T"),
                         {"sku": "T", "complete": False, "events": []})
        # New events start at 1 and the history stays incomplete; nothing is
        # backfilled from the pre-existing stock level.
        app.restock("T", 1)
        history = app.stock_history("T")
        self.assertFalse(history["complete"])
        self.assertEqual(len(history["events"]), 1)
        self.assertEqual(history["events"][0]["sequence"], 1)
        self.assertEqual(history["events"][0]["before"]["on_hand"], 7)

    def test_stock_history_input_validation(self):
        for bad in (None, 123, 1.5, b"T", ["T"], {"x": 1}, "   ", "\t\n"):
            with self.assertRaises(ValueError):
                self.app.stock_history(bad)
        with self.assertRaises(ValueError):
            self.app.stock_history("unknown")
        # Surrounding whitespace is trimmed; skus are case sensitive.
        self.assertEqual(self.app.stock_history("  T  ")["sku"], "T")
        with self.assertRaises(ValueError):
            self.app.stock_history("t")

    def test_stock_history_query_does_not_write(self):
        self.app.restock("T", 5)
        raw = self.app.path.read_bytes()
        self.app.stock_history("T")
        self.app.stock_history("  T  ")
        self.assertEqual(self.app.path.read_bytes(), raw)

    def test_stock_history_query_creates_no_file_or_directory(self):
        empty = self.root / "empty"
        app = OrderDesk(empty)
        with self.assertRaises(ValueError):
            app.stock_history("ghost")
        self.assertFalse(empty.exists())

    def test_cli_stock_history_success_and_failure(self):
        self.app.restock("T", 5)
        payload = self.root / "s.json"
        payload.write_text(json.dumps({"sku": " T "}), encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "stock-history", str(payload)],
                            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual(set(result), {"sku", "complete", "events"})
        self.assertEqual(len(result["events"]), 1)
        for bad in ("unknown", "   "):
            payload.write_text(json.dumps({"sku": bad}), encoding="utf-8")
            failed = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "stock-history", str(payload)],
                                    text=True, capture_output=True)
            self.assertEqual(failed.returncode, 2, failed.stdout)
            self.assertIn("error", json.loads(failed.stderr))

if __name__ == "__main__":
    unittest.main()
