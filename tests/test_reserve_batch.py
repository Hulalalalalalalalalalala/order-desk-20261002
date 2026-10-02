import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk


class ReserveBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)
        self.app.add_product("U", "Unrestocked", 50)

    def _line(self, result, order_id, sku):
        order = next(item for item in result if item["order_id"] == order_id)
        return next(line for line in order["lines"] if line["sku"] == sku)

    def test_priority_order_decides_who_is_topped_up(self):
        # Both orders are placed while the products are unmanaged, so neither
        # holds a reservation; restocking afterwards creates the shared margin.
        self.app.place("O1", [{"sku": "T", "quantity": 4}, {"sku": "C", "quantity": 5}])
        self.app.place("O2", [{"sku": "T", "quantity": 7}])
        self.app.restock("T", 10)
        self.app.restock("C", 5)
        result = self.app.reserve_batch(["O2", "O1"])
        self.assertEqual([item["order_id"] for item in result], ["O2", "O1"])
        for item in result:
            self.assertEqual(set(item), {"order_id", "can_reserve", "lines"})
        o2, o1 = result
        self.assertTrue(o2["can_reserve"])
        self.assertEqual(o2["lines"], [
            {"sku": "T", "quantity": 7, "added": 7, "reserved": 7, "shortfall": 0},
        ])
        self.assertFalse(o1["can_reserve"])
        # O2 consumed 7 of T; O1 sees 3 and falls short by 1, so it is skipped
        # whole: every line keeps added zero and its own reservation.
        self.assertEqual(o1["lines"], [
            {"sku": "C", "quantity": 5, "added": 0, "reserved": 0, "shortfall": 0},
            {"sku": "T", "quantity": 4, "added": 0, "reserved": 0, "shortfall": 1},
        ])
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 10, "reserved": 7, "available": 3})
        self.assertEqual(self.app.stock("C"),
                         {"sku": "C", "on_hand": 5, "reserved": 0, "available": 5})
        # Re-running with the reversed priority: O2 is already fully reserved,
        # O1 still does not fit the 3 remaining units and is skipped again.
        rerun = self.app.reserve_batch(["O1", "O2"])
        self.assertFalse(rerun[0]["can_reserve"])
        self.assertTrue(rerun[1]["can_reserve"])
        self.assertEqual(rerun[1]["lines"][0]["added"], 0)

    def test_skipped_order_consumes_nothing_and_later_orders_continue(self):
        self.app.place("O1", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 10}])
        self.app.place("O2", [{"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 5}])
        self.app.restock("T", 5)
        self.app.restock("C", 5)
        result = self.app.reserve_batch(["O1", "O2"])
        self.assertFalse(result[0]["can_reserve"])
        self.assertEqual(self._line(result, "O1", "T"),
                         {"sku": "T", "quantity": 2, "added": 0, "reserved": 0, "shortfall": 0})
        self.assertEqual(self._line(result, "O1", "C"),
                         {"sku": "C", "quantity": 10, "added": 0, "reserved": 0, "shortfall": 5})
        # O1 consumed neither product: O2 completes against the full margin.
        self.assertTrue(result[1]["can_reserve"])
        self.assertEqual(self._line(result, "O2", "T")["added"], 3)
        self.assertEqual(self._line(result, "O2", "C")["added"], 5)
        self.assertEqual(self.app.stock("T")["reserved"], 3)
        self.assertEqual(self.app.stock("C")["reserved"], 5)

    def test_existing_reservations_reduce_additions_and_seed_margin(self):
        # B was ordered while T was unmanaged and holds no reservation; A was
        # placed against managed stock and holds 2.
        self.app.place("B", [{"sku": "T", "quantity": 3}])
        self.app.restock("T", 5)
        self.app.place("A", [{"sku": "T", "quantity": 2}])
        self.app.restock("T", 5)
        # On hand 10, A holds 2: the shared margin for new demand is 8.
        result = self.app.reserve_batch(["A", "B"])
        a, b = result
        # A is already fully reserved: can_reserve is true with zero additions.
        self.assertTrue(a["can_reserve"])
        self.assertEqual(a["lines"], [
            {"sku": "T", "quantity": 2, "added": 0, "reserved": 2, "shortfall": 0},
        ])
        self.assertTrue(b["can_reserve"])
        self.assertEqual(b["lines"], [
            {"sku": "T", "quantity": 3, "added": 3, "reserved": 3, "shortfall": 0},
        ])
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 10, "reserved": 5, "available": 5})

    def test_duplicate_skus_merge_and_lines_cover_all_skus_sorted(self):
        self.app.place("O1", [
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 2},
            {"sku": "C", "quantity": 2},
        ])
        self.app.restock("T", 10)
        self.app.restock("C", 10)
        result = self.app.reserve_batch(["O1"])[0]
        self.assertEqual([line["sku"] for line in result["lines"]], ["C", "T"])
        for line in result["lines"]:
            self.assertEqual(set(line), {"sku", "quantity", "added", "reserved", "shortfall"})
        self.assertEqual(result["lines"][0],
                         {"sku": "C", "quantity": 3, "added": 3, "reserved": 3, "shortfall": 0})
        self.assertEqual(result["lines"][1],
                         {"sku": "T", "quantity": 2, "added": 2, "reserved": 2, "shortfall": 0})

    def test_unmanaged_lines_stay_zero_and_consume_no_margin(self):
        self.app.place("O1", [{"sku": "U", "quantity": 1000}, {"sku": "T", "quantity": 6}])
        self.app.place("O2", [{"sku": "T", "quantity": 5}])
        self.app.restock("T", 5)
        result = self.app.reserve_batch(["O1", "O2"])
        # O1 fails on T, despite the unlimited unmanaged U being coverable.
        self.assertFalse(result[0]["can_reserve"])
        self.assertEqual(self._line(result, "O1", "U"),
                         {"sku": "U", "quantity": 1000, "added": 0, "reserved": 0, "shortfall": 0})
        self.assertEqual(self._line(result, "O1", "T")["shortfall"], 1)
        # The failed O1 consumed nothing, so O2 takes the full T margin.
        self.assertTrue(result[1]["can_reserve"])
        self.assertEqual(self._line(result, "O2", "T"),
                         {"sku": "T", "quantity": 5, "added": 5, "reserved": 5, "shortfall": 0})
        # U was never auto-managed.
        self.assertEqual(self.app.stock("U"),
                         {"sku": "U", "on_hand": None, "reserved": 0, "available": None})

    def test_paused_sales_do_not_block_batch_top_up(self):
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.restock("T", 5)
        self.app.set_product_enabled("T", False)
        result = self.app.reserve_batch(["O1"])
        self.assertTrue(result[0]["can_reserve"])
        self.assertEqual(result[0]["lines"][0]["added"], 2)
        self.assertEqual(self.app.stock("T")["reserved"], 2)

    def test_deal_lines_prices_amounts_and_status_are_kept(self):
        order = self.app.place("O1", [
            {"sku": "T", "quantity": 2},
            {"sku": "T", "quantity": 1},
        ])
        self.app.restock("T", 10)
        self.app.reserve_batch(["O1"])
        kept = self.app.get("O1")
        self.assertEqual(kept, order)
        self.assertEqual(kept["status"], "placed")
        self.assertEqual([line["quantity"] for line in kept["lines"]], [2, 1])
        self.assertEqual(kept["total_cents"], 300)

    def test_invalid_input_rejects_whole_batch_without_touching_anything(self):
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        self.app.cancel("O2")
        self.app.restock("T", 5)
        raw = self.app.path.read_bytes()
        bad_inputs = [
            None, 123, 1.5, b"O1", "O1", {}, {"x": 1}, [],
            [42], [None], [""], ["   "], ["\t\n"], [["O1"]], [{"x": 1}],
            ["O1", " O1 "],            # duplicate after trimming
            ["O1", "O1"],
            ["missing"],
            ["O2"],                    # cancelled, not placed
            ["O1", "missing"],         # unknown after a valid id
            ["O1", "O2"],              # invalid last item still rejects all
        ]
        for payload in bad_inputs:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    self.app.reserve_batch(payload)
        # Case sensitive: the unknown casing raises rather than topping up O1.
        with self.assertRaises(ValueError):
            self.app.reserve_batch(["O1", "o1"])
        # Nothing was topped up: no reservations, no events, no write.
        self.assertEqual(self.app.path.read_bytes(), raw)
        self.assertEqual(self.app.stock("T")["reserved"], 0)
        self.assertEqual([e["action"] for e in self.app.history("O1")["events"]], ["place"])

    def test_order_line_with_unknown_product_rejects_whole_batch(self):
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        self.app.restock("T", 5)
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        data["orders"]["O2"]["lines"].append(
            {"sku": "GONE", "quantity": 1, "unit_price_cents": 50, "subtotal_cents": 50})
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        raw = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            OrderDesk(self.root).reserve_batch(["O1", "O2"])
        self.assertEqual(self.app.path.read_bytes(), raw)
        self.assertEqual(self.app.stock("T")["reserved"], 0)

    def test_failure_creates_no_file_or_directory(self):
        empty = self.root / "empty"
        app = OrderDesk(empty)
        with self.assertRaises(ValueError):
            app.reserve_batch(["ghost"])
        self.assertFalse(empty.exists())

    def test_no_additions_returns_result_without_writing_or_events(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])
        raw = self.app.path.read_bytes()
        result = self.app.reserve_batch(["O1"])
        self.assertEqual(result, [{"order_id": "O1", "can_reserve": True, "lines": [
            {"sku": "C", "quantity": 1, "added": 0, "reserved": 0, "shortfall": 0},
            {"sku": "T", "quantity": 2, "added": 0, "reserved": 2, "shortfall": 0},
        ]}])
        self.assertEqual(self.app.path.read_bytes(), raw)
        self.assertEqual([e["action"] for e in self.app.history("O1")["events"]], ["place"])
        self.assertEqual([e["action"] for e in self.app.stock_history("T")["events"]],
                         ["restock", "place"])

    def test_history_and_stock_history_events(self):
        self.app.place("O1", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])
        self.app.place("O2", [{"sku": "T", "quantity": 3}])
        self.app.restock("T", 5)
        self.app.restock("C", 4)
        result = self.app.reserve_batch(["O1", "O2"])
        # Order events keep the single top-up result structure.
        history = self.app.history("O1")
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                         [(1, "place"), (2, "reserve-order")])
        self.assertEqual(history["events"][1]["result"], {
            "order_id": "O1",
            "lines": [
                {"sku": "C", "quantity": 1, "added": 1, "reserved": 1},
                {"sku": "T", "quantity": 2, "added": 2, "reserved": 2},
            ],
        })
        history = self.app.history("O2")
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                         [(1, "place"), (2, "reserve-order")])
        self.assertEqual(history["events"][1]["result"], {
            "order_id": "O2",
            "lines": [{"sku": "T", "quantity": 3, "added": 3, "reserved": 3}],
        })
        # Stock events reference the order id and chain in request order.
        events = self.app.stock_history("T")["events"]
        self.assertEqual([e["action"] for e in events],
                         ["restock", "reserve-order", "reserve-order"])
        self.assertEqual([e["sequence"] for e in events], [1, 2, 3])
        self.assertEqual([e["reference_id"] for e in events], [None, "O1", "O2"])
        self.assertEqual(events[1]["before"],
                         {"sku": "T", "on_hand": 5, "reserved": 0, "available": 5})
        self.assertEqual(events[1]["after"],
                         {"sku": "T", "on_hand": 5, "reserved": 2, "available": 3})
        self.assertEqual(events[2]["before"], events[1]["after"])
        self.assertEqual(events[2]["after"],
                         {"sku": "T", "on_hand": 5, "reserved": 5, "available": 0})
        events = self.app.stock_history("C")["events"]
        self.assertEqual([(e["action"], e["reference_id"]) for e in events],
                         [("restock", None), ("reserve-order", "O1")])
        # The batch result lines carry shortfall; the persisted event result
        # does not.
        self.assertEqual(result[0]["lines"][0],
                         {"sku": "C", "quantity": 1, "added": 1, "reserved": 1, "shortfall": 0})

    def test_skipped_order_gets_no_events(self):
        self.app.place("O1", [{"sku": "T", "quantity": 9}])
        self.app.place("O2", [{"sku": "T", "quantity": 2}])
        self.app.restock("T", 5)
        result = self.app.reserve_batch(["O1", "O2"])
        self.assertFalse(result[0]["can_reserve"])
        self.assertTrue(result[1]["can_reserve"])
        self.assertEqual([e["action"] for e in self.app.history("O1")["events"]], ["place"])
        self.assertEqual([e["action"] for e in self.app.history("O2")["events"]],
                         ["place", "reserve-order"])
        self.assertEqual([e["reference_id"] for e in self.app.stock_history("T")["events"]],
                         [None, "O2"])

    def test_persists_across_reopen(self):
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.place("O2", [{"sku": "T", "quantity": 2}])
        self.app.restock("T", 5)
        result = self.app.reserve_batch(["O1", "O2"])
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.stock("T"),
                         {"sku": "T", "on_hand": 5, "reserved": 5, "available": 0})
        for order_id in ("O1", "O2"):
            self.assertEqual([e["action"] for e in reopened.history(order_id)["events"]],
                             ["place", "reserve-order"])
        self.assertEqual([e["action"] for e in reopened.stock_history("T")["events"]],
                         ["restock", "reserve-order", "reserve-order"])
        # Both orders are now fully reserved: a rerun adds nothing and writes
        # nothing, but still reports can_reserve for every order.
        raw = reopened.path.read_bytes()
        rerun = reopened.reserve_batch(["O1", "O2"])
        self.assertEqual([item["can_reserve"] for item in rerun], [True, True])
        self.assertEqual([line["added"] for item in rerun for line in item["lines"]],
                         [0, 0])
        self.assertEqual(reopened.path.read_bytes(), raw)

    def test_topped_up_reservations_drive_pick_amend_cancel_and_ship(self):
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.place("O2", [{"sku": "T", "quantity": 3}])
        self.app.restock("T", 10)
        self.app.reserve_batch(["O1", "O2"])
        pick = self.app.pick_list(["O1", "O2"])
        self.assertEqual(pick["lines"][0]["reserved"], 5)
        self.assertEqual(pick["lines"][0]["orders"], [
            {"order_id": "O1", "quantity": 2, "reserved": 2},
            {"order_id": "O2", "quantity": 3, "reserved": 3},
        ])
        # Amend sees the topped-up reservation as this order's own allowance.
        self.app.amend("O1", [{"sku": "T", "quantity": 4}])
        self.assertEqual(self.app.stock("T")["reserved"], 7)
        self.app.cancel("O2")
        self.assertEqual(self.app.stock("T")["reserved"], 4)
        # Ship deducts the topped-up reservation.
        self.app.ship("O1", "DHL", "1")
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 6, "reserved": 0, "available": 6})

    def test_ship_batch_uses_topped_up_reservations(self):
        self.app.place("A", [{"sku": "T", "quantity": 1}])
        self.app.place("B", [{"sku": "T", "quantity": 2}])
        self.app.restock("T", 5)
        self.app.reserve_batch(["A", "B"])
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
        result = app.reserve_batch(["OLD"])
        self.assertEqual(result, [{"order_id": "OLD", "can_reserve": True, "lines": [
            {"sku": "T", "quantity": 2, "added": 2, "reserved": 2, "shortfall": 0},
        ]}])
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

    def test_other_orders_reservations_are_not_released_or_transferred(self):
        # O1 is ordered while unmanaged and holds no reservation; KEEPER is
        # placed later against managed stock and reserves 8 of 10.
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.restock("T", 10)
        self.app.place("KEEPER", [{"sku": "T", "quantity": 8}])
        result = self.app.reserve_batch(["O1"])
        # Only 2 units remain free, so O1 is skipped; the keeper's 8 units
        # stay reserved and are never offered to O1.
        self.assertFalse(result[0]["can_reserve"])
        self.assertEqual(result[0]["lines"][0],
                         {"sku": "T", "quantity": 3, "added": 0, "reserved": 0, "shortfall": 1})
        self.assertEqual(self.app.stock("T")["reserved"], 8)

    def test_cli_reserve_batch_success_and_failure(self):
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        self.app.restock("T", 5)
        payload = self.root / "b.json"
        payload.write_text(json.dumps({"order_ids": [" O1 ", "O2"]}), encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                             "reserve-batch", str(payload)],
                            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual(result, [
            {"order_id": "O1", "can_reserve": True, "lines": [
                {"sku": "T", "quantity": 2, "added": 2, "reserved": 2, "shortfall": 0},
            ]},
            {"order_id": "O2", "can_reserve": True, "lines": [
                {"sku": "T", "quantity": 1, "added": 1, "reserved": 1, "shortfall": 0},
            ]},
        ])
        before = (self.root / "data.json").read_bytes()
        for bad in (["O1", "O1"], ["unknown"], [], ["O1", "   "]):
            payload.write_text(json.dumps({"order_ids": bad}), encoding="utf-8")
            failed = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                                     "reserve-batch", str(payload)],
                                    text=True, capture_output=True)
            self.assertEqual(failed.returncode, 2, failed.stdout)
            self.assertEqual(failed.stdout, "")
            self.assertIn("error", json.loads(failed.stderr))
        self.assertEqual((self.root / "data.json").read_bytes(), before)

    def test_cli_array_runs_item_by_item(self):
        self.app.place("A", [{"sku": "T", "quantity": 1}])
        self.app.place("B", [{"sku": "T", "quantity": 1}])
        self.app.restock("T", 5)
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([
            {"order_ids": ["A"]},
            {"order_ids": ["missing"]},
            {"order_ids": ["B"]},
        ]), encoding="utf-8")
        run = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                              "reserve-batch", str(batch)],
                             text=True, capture_output=True)
        self.assertEqual(run.returncode, 2, run.stdout)
        reopened = OrderDesk(self.root)
        self.assertEqual([e["action"] for e in reopened.history("A")["events"]],
                         ["place", "reserve-order"])
        self.assertEqual([e["action"] for e in reopened.history("B")["events"]], ["place"])
        self.assertEqual(reopened.stock("T")["reserved"], 1)


if __name__ == "__main__":
    unittest.main()
