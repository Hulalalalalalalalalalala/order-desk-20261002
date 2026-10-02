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
        self.app.add_product("U", "Unmanaged", 0)

    def _stock(self, sku, on_hand, reserved):
        return {"sku": sku, "on_hand": on_hand, "reserved": reserved,
                "available": None if on_hand is None else on_hand - reserved}

    def test_full_lifecycle_events_and_snapshots(self):
        self.app.restock("T", 10)
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 4}])
        self.app.ship("O1", "DHL", "1")
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.receive_return("R1")
        self.app.count_stock("K1", [{"sku": "T", "on_hand": 9}])
        history = self.app.stock_history("T")
        self.assertEqual(set(history), {"sku", "complete", "events"})
        self.assertEqual(history["sku"], "T")
        self.assertTrue(history["complete"])
        events = history["events"]
        self.assertEqual([e["sequence"] for e in events], [1, 2, 3, 4, 5, 6])
        for event in events:
            self.assertEqual(set(event), {"sequence", "action", "reference_id", "before", "after"})
        self.assertEqual([e["action"] for e in events],
                         ["restock", "restock", "place", "ship", "receive-return", "count-stock"])
        self.assertEqual([e["reference_id"] for e in events],
                         [None, None, "O1", "O1", "R1", "K1"])
        # First restock: unmanaged -> managed.
        self.assertEqual(events[0]["before"], self._stock("T", None, 0))
        self.assertEqual(events[0]["after"], self._stock("T", 10, 0))
        # Second restock chains from the first.
        self.assertEqual(events[1]["before"], self._stock("T", 10, 0))
        self.assertEqual(events[1]["after"], self._stock("T", 15, 0))
        # Place reserves; ship deducts on_hand and reserved together.
        self.assertEqual(events[2]["before"], self._stock("T", 15, 0))
        self.assertEqual(events[2]["after"], self._stock("T", 15, 4))
        self.assertEqual(events[3]["before"], self._stock("T", 15, 4))
        self.assertEqual(events[3]["after"], self._stock("T", 11, 0))
        # Return receipt puts stock back.
        self.assertEqual(events[4]["before"], self._stock("T", 11, 0))
        self.assertEqual(events[4]["after"], self._stock("T", 12, 0))
        # Count calibrates on_hand.
        self.assertEqual(events[5]["before"], self._stock("T", 12, 0))
        self.assertEqual(events[5]["after"], self._stock("T", 9, 0))

    def test_unmanaged_product_has_complete_empty_history(self):
        self.assertEqual(self.app.stock_history("U"),
                         {"sku": "U", "complete": True, "events": []})

    def test_validation_and_unknown_product(self):
        for bad in (None, 123, 1.5, b"T", ["T"], {"x": 1}, "   ", "\t\n"):
            with self.assertRaises(ValueError):
                self.app.stock_history(bad)
        with self.assertRaises(ValueError):
            self.app.stock_history("ghost")
        # Surrounding whitespace is trimmed; skus are case sensitive.
        self.app.restock("T", 1)
        self.assertEqual(self.app.stock_history("  T  ")["sku"], "T")
        with self.assertRaises(ValueError):
            self.app.stock_history("t")

    def test_place_events_per_sku_sorted_and_unmanaged_skipped(self):
        self.app.restock("T", 10)
        self.app.restock("C", 10)
        self.app.place("O1", [
            {"sku": "C", "quantity": 2},
            {"sku": "T", "quantity": 3},
            {"sku": "U", "quantity": 9},
        ])
        actions = [(e["action"], e["reference_id"]) for e in self.app.stock_history("C")["events"]]
        self.assertEqual(actions, [("restock", None), ("place", "O1")])
        events_t = self.app.stock_history("T")["events"]
        self.assertEqual([e["reference_id"] for e in events_t], [None, "O1"])
        self.assertEqual(events_t[1]["after"], self._stock("T", 10, 3))
        # The unmanaged line reserved nothing and left no event.
        self.assertEqual(self.app.stock_history("U"),
                         {"sku": "U", "complete": True, "events": []})

    def test_cancel_records_release_only_for_real_reservations(self):
        self.app.restock("T", 10)
        # Unmanaged-only order: cancellation changes no reserved quantity.
        self.app.place("O0", [{"sku": "U", "quantity": 5}])
        self.app.cancel("O0")
        self.assertEqual(self.app.stock_history("U")["events"], [])
        # Mixed order: only the managed sku gets place and cancel events.
        self.app.place("O1", [{"sku": "U", "quantity": 5}, {"sku": "T", "quantity": 2}])
        self.app.cancel("O1")
        events = self.app.stock_history("T")["events"]
        self.assertEqual([e["action"] for e in events], ["restock", "place", "cancel"])
        self.assertEqual([e["reference_id"] for e in events], [None, "O1", "O1"])
        self.assertEqual(events[2]["before"], self._stock("T", 10, 2))
        self.assertEqual(events[2]["after"], self._stock("T", 10, 0))

    def test_ship_without_reservation_leaves_no_event(self):
        self.app.restock("T", 10)
        self.app.place("O0", [{"sku": "U", "quantity": 3}])
        self.app.ship("O0", "DHL", "1")
        self.assertEqual(self.app.stock_history("U")["events"], [])
        # Order history is still recorded.
        self.assertEqual([e["action"] for e in self.app.history("O0")["events"]],
                         ["place", "ship"])

    def test_amend_records_only_final_net_change(self):
        self.app.restock("T", 10)
        self.app.restock("C", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 2}])
        # Shrink T and drop C entirely: one amend event per changed sku, no
        # temporary release/reservation events.
        self.app.amend("O1", [{"sku": "T", "quantity": 5}])
        events_t = self.app.stock_history("T")["events"]
        self.assertEqual([(e["sequence"], e["action"]) for e in events_t],
                         [(1, "restock"), (2, "place"), (3, "amend")])
        self.assertEqual(events_t[2]["before"], self._stock("T", 10, 3))
        self.assertEqual(events_t[2]["after"], self._stock("T", 10, 5))
        events_c = self.app.stock_history("C")["events"]
        self.assertEqual([(e["sequence"], e["action"]) for e in events_c],
                         [(1, "restock"), (2, "place"), (3, "amend")])
        self.assertEqual(events_c[2]["before"], self._stock("C", 10, 2))
        self.assertEqual(events_c[2]["after"], self._stock("C", 10, 0))
        # A net-zero amend adds an order-history event but no stock event.
        self.app.amend("O1", [{"sku": "T", "quantity": 5}])
        self.assertEqual(len(self.app.stock_history("T")["events"]), 3)
        self.assertEqual([e["action"] for e in self.app.history("O1")["events"]],
                         ["place", "amend", "amend"])
        # Sequences stay contiguous after the skipped amend.
        self.app.amend("O1", [{"sku": "T", "quantity": 4}])
        events_t = self.app.stock_history("T")["events"]
        self.assertEqual([e["sequence"] for e in events_t], [1, 2, 3, 4])
        self.assertEqual(events_t[3]["before"], self._stock("T", 10, 5))
        self.assertEqual(events_t[3]["after"], self._stock("T", 10, 4))

    def test_equal_value_count_adds_no_event(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.count_stock("K1", [{"sku": "T", "on_hand": 10}])
        self.assertEqual([e["action"] for e in self.app.stock_history("T")["events"]],
                         ["restock", "place"])
        # A mixed count records only the line that actually moved.
        self.app.restock("C", 4)
        self.app.count_stock("K2", [
            {"sku": "T", "on_hand": 10},
            {"sku": "C", "on_hand": 7},
        ])
        self.assertEqual([e["action"] for e in self.app.stock_history("C")["events"]],
                         ["restock", "count-stock"])

    def test_ship_batch_orders_recorded_in_request_order_with_chained_snapshots(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.place("O2", [{"sku": "T", "quantity": 4}])
        result = self.app.ship_batch([
            {"order_id": "O2", "carrier": "DHL", "tracking_no": "b"},
            {"order_id": "O1", "carrier": "UPS", "tracking_no": "a"},
        ])
        self.assertEqual([o["order_id"] for o in result], ["O1", "O2"])
        events = self.app.stock_history("T")["events"]
        self.assertEqual([e["action"] for e in events],
                         ["restock", "place", "place", "ship", "ship"])
        self.assertEqual([e["reference_id"] for e in events],
                         [None, "O1", "O2", "O2", "O1"])
        # Snapshots chain across orders in request order, not sorted order.
        self.assertEqual(events[3]["before"], self._stock("T", 10, 7))
        self.assertEqual(events[3]["after"], self._stock("T", 6, 3))
        self.assertEqual(events[4]["before"], self._stock("T", 6, 3))
        self.assertEqual(events[4]["after"], self._stock("T", 3, 0))

    def test_rejected_batch_leaves_no_stock_events(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.place("O2", [{"sku": "T", "quantity": 4}])
        with self.assertRaises(ValueError):
            self.app.ship_batch([
                {"order_id": "O2", "carrier": "DHL", "tracking_no": "b"},
                {"order_id": "missing", "carrier": "UPS", "tracking_no": "x"},
            ])
        events = self.app.stock_history("T")["events"]
        self.assertEqual([e["action"] for e in events], ["restock", "place", "place"])
        self.assertEqual(self.app.stock("T"), self._stock("T", 10, 7))

    def test_failed_operations_consume_no_sequence(self):
        self.app.restock("T", 10)
        with self.assertRaises(ValueError):
            self.app.place("O1", [{"sku": "T", "quantity": 99}])
        with self.assertRaises(ValueError):
            self.app.restock("ghost", 1)
        with self.assertRaises(ValueError):
            self.app.count_stock("K1", [{"sku": "T", "on_hand": -1}])
        with self.assertRaises(ValueError):
            self.app.receive_return("missing")
        self.assertEqual([e["sequence"] for e in self.app.stock_history("T")["events"]], [1])
        self.assertEqual(self.app.stock_history("T")["events"][0]["action"], "restock")

    def test_persists_across_reopen_and_old_snapshots_are_kept(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        history = OrderDesk(self.root).stock_history("T")
        self.assertTrue(history["complete"])
        self.assertEqual([(e["sequence"], e["action"], e["reference_id"]) for e in history["events"]],
                         [(1, "restock", None), (2, "place", "O1")])
        self.assertEqual(history["events"][0]["before"], self._stock("T", None, 0))
        # Later operations must not mutate snapshots already returned/disk-stored.
        self.app.ship("O1", "DHL", "1")
        again = OrderDesk(self.root).stock_history("T")
        self.assertEqual(again["events"][0]["after"], self._stock("T", 10, 0))
        self.assertEqual(again["events"][1]["before"], self._stock("T", 10, 0))
        self.assertEqual(again["events"][1]["after"], self._stock("T", 10, 2))
        self.assertEqual(again["events"][2]["before"], self._stock("T", 10, 2))

    def test_query_does_not_write_or_create_directory(self):
        empty = self.root / "empty"
        app = OrderDesk(empty)
        with self.assertRaises(ValueError):
            app.stock_history("ghost")
        self.assertFalse(empty.exists())
        self.app.restock("T", 5)
        raw = self.app.path.read_bytes()
        self.app.stock_history("T")
        self.app.stock_history("  T  ")
        self.assertEqual(self.app.path.read_bytes(), raw)

    def _legacy_managed_data(self):
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "inventory": {"T": {"on_hand": 7, "reserved": 2}},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "data.json").write_text(json.dumps(data), encoding="utf-8")

    def test_legacy_managed_product_is_incomplete_empty_and_never_completes(self):
        self._legacy_managed_data()
        app = OrderDesk(self.root)
        self.assertEqual(app.stock_history("T"),
                         {"sku": "T", "complete": False, "events": []})
        # Querying did not backfill anything.
        self.assertEqual(OrderDesk(self.root).stock_history("T")["events"], [])
        # A later change starts at sequence 1 and stays incomplete.
        app.restock("T", 3)
        history = OrderDesk(self.root).stock_history("T")
        self.assertFalse(history["complete"])
        self.assertEqual(len(history["events"]), 1)
        self.assertEqual(history["events"][0]["sequence"], 1)
        self.assertEqual(history["events"][0]["action"], "restock")
        self.assertEqual(history["events"][0]["before"], self._stock("T", 7, 2))
        self.assertEqual(history["events"][0]["after"], self._stock("T", 10, 2))

    def test_cart_checkout_records_place_action(self):
        self.app.restock("T", 10)
        self.app.save_cart("cart1", [{"sku": "T", "quantity": 2}])
        self.app.checkout_cart("cart1", "O9")
        events = self.app.stock_history("T")["events"]
        self.assertEqual([(e["action"], e["reference_id"]) for e in events],
                         [("restock", None), ("place", "O9")])

    def test_receive_return_events(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.ship("O1", "DHL", "1")
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        self.app.receive_return("R1")
        event = self.app.stock_history("T")["events"][-1]
        self.assertEqual(event["action"], "receive-return")
        self.assertEqual(event["reference_id"], "R1")
        self.assertEqual(event["before"], self._stock("T", 7, 0))
        self.assertEqual(event["after"], self._stock("T", 9, 0))

    def test_paused_sales_still_record_inventory_events(self):
        self.app.set_product_enabled("T", False)
        self.app.restock("T", 10)
        events = self.app.stock_history("T")["events"]
        self.assertEqual(events[0]["action"], "restock")
        self.assertEqual(events[0]["after"], self._stock("T", 10, 0))

    def test_cli_stock_history_success_and_failure(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        payload = self.root / "h.json"
        payload.write_text(json.dumps({"sku": " T "}), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "stock-history", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual(set(result), {"sku", "complete", "events"})
        self.assertEqual([e["action"] for e in result["events"]], ["restock", "place"])
        self.assertIsNone(result["events"][0]["reference_id"])
        for bad in ("ghost", "   "):
            payload.write_text(json.dumps({"sku": bad}), encoding="utf-8")
            failed = subprocess.run(
                [sys.executable, "-m", "order_desk", "--root", str(self.root),
                 "stock-history", str(payload)],
                text=True, capture_output=True)
            self.assertEqual(failed.returncode, 2, failed.stdout)
            self.assertIn("error", json.loads(failed.stderr))


if __name__ == "__main__":
    unittest.main()
