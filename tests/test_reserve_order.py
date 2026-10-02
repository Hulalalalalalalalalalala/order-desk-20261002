import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk

class ReserveOrderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)

    def test_top_up_after_restock_matches_spec_example(self):
        # Ordered while unmanaged: three units, no reservation. Restock five,
        # then the top-up reserves three, leaving two available.
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.restock("T", 5)
        result = self.app.reserve_order("O1")
        self.assertEqual(set(result), {"order_id", "lines"})
        self.assertEqual(result["order_id"], "O1")
        self.assertEqual(result["lines"], [
            {"sku": "T", "quantity": 3, "added": 3, "reserved": 3},
        ])
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 5, "reserved": 3, "available": 2})

    def test_duplicate_skus_merge_and_lines_cover_all_skus_sorted(self):
        self.app.place("O1", [
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 2},
            {"sku": "C", "quantity": 2},
        ])
        self.app.restock("T", 10)
        self.app.restock("C", 10)
        result = self.app.reserve_order("O1")
        self.assertEqual([line["sku"] for line in result["lines"]], ["C", "T"])
        for line in result["lines"]:
            self.assertEqual(set(line), {"sku", "quantity", "added", "reserved"})
        self.assertEqual(result["lines"][0],
                         {"sku": "C", "quantity": 3, "added": 3, "reserved": 3})
        self.assertEqual(result["lines"][1],
                         {"sku": "T", "quantity": 2, "added": 2, "reserved": 2})

    def test_deal_lines_prices_amounts_and_status_are_kept(self):
        order = self.app.place("O1", [
            {"sku": "T", "quantity": 2},
            {"sku": "T", "quantity": 1},
        ])
        self.app.set_product_enabled("T", False)
        self.app.restock("T", 10)
        self.app.reserve_order("O1")
        kept = self.app.get("O1")
        self.assertEqual(kept, order)
        self.assertEqual(kept["status"], "placed")
        self.assertEqual([line["quantity"] for line in kept["lines"]], [2, 1])
        self.assertEqual(kept["total_cents"], 300)

    def test_paused_product_does_not_block_top_up(self):
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.set_product_enabled("T", False)
        self.app.restock("T", 5)
        result = self.app.reserve_order("O1")
        self.assertEqual(result["lines"], [
            {"sku": "T", "quantity": 2, "added": 2, "reserved": 2},
        ])
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 5, "reserved": 2, "available": 3})

    def test_existing_reservation_reduces_addition(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.amend("O1", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 3}])
        self.app.restock("C", 4)
        result = self.app.reserve_order("O1")
        self.assertEqual(result["lines"], [
            {"sku": "C", "quantity": 3, "added": 3, "reserved": 3},
            {"sku": "T", "quantity": 2, "added": 0, "reserved": 2},
        ])
        self.assertEqual(self.app.stock("C"),
                         {"sku": "C", "on_hand": 4, "reserved": 3, "available": 1})

    def test_unmanaged_lines_stay_zero_and_stock_of_others_untouched(self):
        self.app.place("O2", [{"sku": "C", "quantity": 9}, {"sku": "T", "quantity": 1}])
        self.app.restock("T", 8)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        result = self.app.reserve_order("O2")
        self.assertEqual(result["lines"], [
            {"sku": "C", "quantity": 9, "added": 0, "reserved": 0},
            {"sku": "T", "quantity": 1, "added": 1, "reserved": 1},
        ])
        # O1's reservation and the unmanaged C stock view are unchanged.
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 8, "reserved": 3, "available": 5})
        self.assertEqual(self.app.stock("C"),
                         {"sku": "C", "on_hand": None, "reserved": 0, "available": None})

    def test_insufficient_availability_rejects_whole_top_up(self):
        self.app.place("O2", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 3}])
        self.app.restock("T", 5)
        self.app.restock("C", 2)
        raw = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.reserve_order("O2")
        # Nothing was topped up: no reservation, no events, no write.
        self.assertEqual(self.app.path.read_bytes(), raw)
        self.assertEqual(self.app.stock("T")["reserved"], 0)
        self.assertEqual(self.app.stock("C")["reserved"], 0)
        self.assertEqual([e["action"] for e in self.app.history("O2")["events"]], ["place"])
        self.assertEqual([e["action"] for e in self.app.stock_history("T")["events"]],
                         ["restock"])
        self.assertEqual([e["action"] for e in self.app.stock_history("C")["events"]],
                         ["restock"])

    def test_input_validation_and_status_rules(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        self.app.cancel("O2")
        for bad in (None, 123, 1.5, b"O1", ["O1"], {"x": 1}, "   ", "\t\n"):
            with self.assertRaises(ValueError):
                self.app.reserve_order(bad)
        with self.assertRaises(ValueError):
            self.app.reserve_order("unknown")
        with self.assertRaises(ValueError):
            self.app.reserve_order("O2")
        self.app.ship("O1", "DHL", "1")
        with self.assertRaises(ValueError):
            self.app.reserve_order("O1")
        # Ids are trimmed and case sensitive.
        self.app.place("O3", [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.reserve_order("o3")
        result = self.app.reserve_order("  O3  ")
        self.assertEqual(result["order_id"], "O3")

    def test_order_line_with_unknown_product_is_rejected(self):
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        data["orders"]["O1"]["lines"].append(
            {"sku": "GONE", "quantity": 1, "unit_price_cents": 50, "subtotal_cents": 50})
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        raw = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            OrderDesk(self.root).reserve_order("O1")
        self.assertEqual(self.app.path.read_bytes(), raw)

    def test_history_and_stock_history_events(self):
        self.app.place("O1", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])
        self.app.restock("T", 5)
        self.app.restock("C", 4)
        result = self.app.reserve_order("O1")
        history = self.app.history("O1")
        self.assertTrue(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                         [(1, "place"), (2, "reserve-order")])
        self.assertEqual(set(history["events"][1]), {"sequence", "action", "result"})
        self.assertEqual(history["events"][1]["result"], result)
        for sku in ("C", "T"):
            events = self.app.stock_history(sku)["events"]
            self.assertEqual([e["action"] for e in events], ["restock", "reserve-order"])
            self.assertEqual(events[1]["reference_id"], "O1")
        event = self.app.stock_history("T")["events"][1]
        self.assertEqual(event["before"], {"sku": "T", "on_hand": 5, "reserved": 0, "available": 5})
        self.assertEqual(event["after"], {"sku": "T", "on_hand": 5, "reserved": 2, "available": 3})
        event = self.app.stock_history("C")["events"][1]
        self.assertEqual(event["before"], {"sku": "C", "on_hand": 4, "reserved": 0, "available": 4})
        self.assertEqual(event["after"], {"sku": "C", "on_hand": 4, "reserved": 1, "available": 3})

    def test_stock_events_chain_with_later_operations(self):
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.restock("T", 5)
        self.app.reserve_order("O1")
        self.app.ship("O1", "DHL", "1")
        events = self.app.stock_history("T")["events"]
        self.assertEqual([e["action"] for e in events],
                         ["restock", "reserve-order", "ship"])
        self.assertEqual([e["sequence"] for e in events], [1, 2, 3])
        for previous, following in zip(events, events[1:]):
            self.assertEqual(previous["after"], following["before"])
        self.assertEqual(events[2]["after"],
                         {"sku": "T", "on_hand": 3, "reserved": 0, "available": 3})

    def test_no_addition_returns_result_without_writing_or_events(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])
        raw = self.app.path.read_bytes()
        result = self.app.reserve_order("O1")
        self.assertEqual(result["lines"], [
            {"sku": "C", "quantity": 1, "added": 0, "reserved": 0},
            {"sku": "T", "quantity": 2, "added": 0, "reserved": 2},
        ])
        self.assertEqual(self.app.path.read_bytes(), raw)
        self.assertEqual([e["action"] for e in self.app.history("O1")["events"]], ["place"])
        self.assertEqual([e["action"] for e in self.app.stock_history("T")["events"]],
                         ["restock", "place"])

    def test_failure_creates_no_file_or_directory(self):
        empty = self.root / "empty"
        app = OrderDesk(empty)
        with self.assertRaises(ValueError):
            app.reserve_order("ghost")
        self.assertFalse(empty.exists())

    def test_persists_across_reopen(self):
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.restock("T", 5)
        result = self.app.reserve_order("O1")
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.stock("T"),
                         {"sku": "T", "on_hand": 5, "reserved": 3, "available": 2})
        self.assertEqual([e["action"] for e in reopened.history("O1")["events"]],
                         ["place", "reserve-order"])
        self.assertEqual(reopened.history("O1")["events"][1]["result"], result)
        self.assertEqual([e["action"] for e in reopened.stock_history("T")["events"]],
                         ["restock", "reserve-order"])

    def test_topped_up_reservations_drive_pick_amend_cancel_and_ship(self):
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.restock("T", 5)
        self.app.reserve_order("O1")
        pick = self.app.pick_list(["O1"])
        self.assertEqual(pick["lines"][0]["reserved"], 2)
        self.assertEqual(pick["lines"][0]["orders"],
                         [{"order_id": "O1", "quantity": 2, "reserved": 2}])
        # Amend sees the topped-up reservation as this order's own allowance.
        self.app.amend("O1", [{"sku": "T", "quantity": 5}])
        self.assertEqual(self.app.stock("T")["reserved"], 5)
        self.app.cancel("O1")
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 5, "reserved": 0, "available": 5})
        # Ship deducts the topped-up reservation.
        self.app.place("O2", [{"sku": "T", "quantity": 2}])
        self.app.reserve_order("O2")
        self.app.ship("O2", "DHL", "1")
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 3, "reserved": 0, "available": 3})

    def test_ship_batch_uses_topped_up_reservations(self):
        self.app.place("A", [{"sku": "T", "quantity": 1}])
        self.app.place("B", [{"sku": "T", "quantity": 2}])
        self.app.restock("T", 5)
        self.app.reserve_order("A")
        self.app.reserve_order("B")
        self.app.ship_batch([
            {"order_id": "A", "carrier": "DHL", "tracking_no": "1"},
            {"order_id": "B", "carrier": "UPS", "tracking_no": "2"},
        ])
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 2, "reserved": 0, "available": 2})

    def _legacy_data(self):
        order = {"order_id": "OLD", "status": "placed",
                 "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100,
                            "subtotal_cents": 200}],
                 "total_cents": 200}
        data = {"products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
                "inventory": {"T": {"on_hand": 7, "reserved": 0}},
                "orders": {"OLD": order}}
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")

    def test_legacy_order_and_stock_start_history_at_one_incomplete(self):
        self._legacy_data()
        app = OrderDesk(self.root)
        result = app.reserve_order("OLD")
        self.assertEqual(result["lines"], [
            {"sku": "T", "quantity": 2, "added": 2, "reserved": 2},
        ])
        history = app.history("OLD")
        self.assertFalse(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                         [(1, "reserve-order")])
        stock = app.stock_history("T")
        self.assertFalse(stock["complete"])
        self.assertEqual(len(stock["events"]), 1)
        self.assertEqual(stock["events"][0]["sequence"], 1)
        self.assertEqual(stock["events"][0]["action"], "reserve-order")
        self.assertEqual(stock["events"][0]["before"],
                         {"sku": "T", "on_hand": 7, "reserved": 0, "available": 7})
        self.assertEqual(stock["events"][0]["after"],
                         {"sku": "T", "on_hand": 7, "reserved": 2, "available": 5})

    def test_cli_reserve_order_success_and_failure(self):
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.restock("T", 5)
        payload = self.root / "r.json"
        payload.write_text(json.dumps({"order_id": " O1 "}), encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                             "reserve-order", str(payload)],
                            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual(result, {"order_id": "O1", "lines": [
            {"sku": "T", "quantity": 2, "added": 2, "reserved": 2},
        ]})
        for bad in ("unknown", "   "):
            payload.write_text(json.dumps({"order_id": bad}), encoding="utf-8")
            failed = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                                     "reserve-order", str(payload)],
                                    text=True, capture_output=True)
            self.assertEqual(failed.returncode, 2, failed.stdout)
            self.assertIn("error", json.loads(failed.stderr))

    def test_cli_array_runs_item_by_item(self):
        self.app.place("A", [{"sku": "T", "quantity": 1}])
        self.app.place("B", [{"sku": "T", "quantity": 1}])
        self.app.restock("T", 5)
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([
            {"order_id": "A"},
            {"order_id": "missing"},
            {"order_id": "B"},
        ]), encoding="utf-8")
        run = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                              "reserve-order", str(batch)],
                             text=True, capture_output=True)
        self.assertEqual(run.returncode, 2, run.stdout)
        reopened = OrderDesk(self.root)
        self.assertEqual([e["action"] for e in reopened.history("A")["events"]],
                         ["place", "reserve-order"])
        self.assertEqual([e["action"] for e in reopened.history("B")["events"]], ["place"])
        self.assertEqual(reopened.stock("T")["reserved"], 1)

if __name__ == "__main__":
    unittest.main()
