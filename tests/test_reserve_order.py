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

    def test_unmanaged_order_then_restock_then_reserve(self):
        # The scenario from the spec: order three while unmanaged, restock
        # five, then fill -> on hand five, reserved three, available two.
        order = self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": None, "reserved": 0, "available": None})
        self.app.restock("T", 5)
        result = self.app.reserve_order(" O1 ")
        self.assertEqual(result, {"order_id": "O1", "lines": [
            {"sku": "T", "quantity": 3, "added": 3, "reserved": 3},
        ]})
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 5, "reserved": 3, "available": 2})
        # The deal content is untouched.
        self.assertEqual(self.app.get("O1"), order)
        self.assertEqual(order["status"], "placed")
        self.assertEqual(order["lines"], [
            {"sku": "T", "quantity": 3, "unit_price_cents": 100, "subtotal_cents": 300},
        ])
        self.assertEqual(order["total_cents"], 300)

    def test_result_merges_duplicate_skus_and_sorts_lines(self):
        self.app.place("O1", [
            {"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200},
            {"sku": "C", "quantity": 1, "unit_price_cents": 200, "subtotal_cents": 200},
            {"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100},
        ])
        self.app.restock("C", 4)
        self.app.restock("T", 4)
        result = self.app.reserve_order("O1")
        self.assertEqual(set(result), {"order_id", "lines"})
        self.assertEqual(result["lines"], [
            {"sku": "C", "quantity": 1, "added": 1, "reserved": 1},
            {"sku": "T", "quantity": 3, "added": 3, "reserved": 3},
        ])
        for line in result["lines"]:
            self.assertEqual(set(line), {"sku", "quantity", "added", "reserved"})
        # Duplicate lines, order and prices are preserved on the order.
        self.assertEqual(self.app.get("O1")["lines"], [
            {"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200},
            {"sku": "C", "quantity": 1, "unit_price_cents": 200, "subtotal_cents": 200},
            {"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100},
        ])

    def test_paused_sales_do_not_block_fill(self):
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.set_product_enabled("T", False)
        self.app.restock("T", 2)
        result = self.app.reserve_order("O1")
        self.assertEqual(result["lines"], [{"sku": "T", "quantity": 2, "added": 2, "reserved": 2}])
        self.assertEqual(self.app.stock("T")["available"], 0)

    def test_unmanaged_lines_stay_zero_and_do_not_auto_manage(self):
        self.app.place("O1", [{"sku": "T", "quantity": 99}, {"sku": "C", "quantity": 7}])
        self.app.restock("T", 100)
        result = self.app.reserve_order("O1")
        self.assertEqual(result["lines"], [
            {"sku": "C", "quantity": 7, "added": 0, "reserved": 0},
            {"sku": "T", "quantity": 99, "added": 99, "reserved": 99},
        ])
        self.assertEqual(self.app.stock("C"), {"sku": "C", "on_hand": None, "reserved": 0, "available": None})
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 100, "reserved": 99, "available": 1})
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(data["reservations"]["O1"], {"T": 99})

    def test_partial_existing_reservation_gets_only_shortage(self):
        # Legacy state: the order merged demand is 5 but it actually holds 2,
        # while stock currently has three units available.
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "orders": {"O1": {"order_id": "O1", "status": "placed",
                              "lines": [{"sku": "T", "quantity": 5, "unit_price_cents": 100, "subtotal_cents": 500}],
                              "total_cents": 500}},
            "inventory": {"T": {"on_hand": 10, "reserved": 2}},
            "reservations": {"O1": {"T": 2}},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        result = app.reserve_order("O1")
        self.assertEqual(result["lines"], [{"sku": "T", "quantity": 5, "added": 3, "reserved": 5}])
        self.assertEqual(app.stock("T"), {"sku": "T", "on_hand": 10, "reserved": 5, "available": 5})

    def test_missing_reservation_record_counts_as_zero(self):
        # Legacy order with no reservations key at all; stock is managed.
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "orders": {"OLD": {"order_id": "OLD", "status": "placed",
                               "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200}],
                               "total_cents": 200}},
            "inventory": {"T": {"on_hand": 4, "reserved": 0}},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        result = app.reserve_order("OLD")
        self.assertEqual(result["lines"], [{"sku": "T", "quantity": 2, "added": 2, "reserved": 2}])
        self.assertEqual(json.loads(app.path.read_text(encoding="utf-8"))["reservations"], {"OLD": {"T": 2}})

    def test_insufficient_stock_is_atomic_and_touches_no_order(self):
        # O1 was placed while both products were unmanaged; only then does
        # stock arrive and another order consume most of T.
        self.app.place("O1", [{"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 2}])
        self.app.restock("T", 5)
        self.app.place("O2", [{"sku": "T", "quantity": 4}])
        self.app.restock("C", 10)
        # O1 needs 3 T but only one unit is available; C has plenty.
        raw = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.reserve_order("O1")
        self.assertEqual(self.app.path.read_bytes(), raw)
        # Nothing was filled for O1 and the other order's reservation stands.
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 5, "reserved": 4, "available": 1})
        self.assertEqual(self.app.stock("C"), {"sku": "C", "on_hand": 10, "reserved": 0, "available": 10})
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(data["reservations"], {"O2": {"T": 4}})
        self.assertNotIn("reserve-order", self.app.path.read_text(encoding="utf-8"))

    def test_exact_availability_succeeds(self):
        self.app.place("O1", [{"sku": "T", "quantity": 4}])
        self.app.place("O2", [{"sku": "T", "quantity": 1}])  # other order, unmanaged at place time
        self.app.restock("T", 4)
        result = self.app.reserve_order("O1")
        self.assertEqual(result["lines"], [{"sku": "T", "quantity": 4, "added": 4, "reserved": 4}])
        self.assertEqual(self.app.stock("T")["available"], 0)

    def test_already_fully_reserved_is_noop(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        raw = self.app.path.read_bytes()
        result = self.app.reserve_order("O1")
        self.assertEqual(result["lines"], [{"sku": "T", "quantity": 3, "added": 0, "reserved": 3}])
        self.assertEqual(self.app.path.read_bytes(), raw)
        self.assertEqual([e["action"] for e in self.app.history("O1")["events"]], ["place"])
        self.assertEqual([e["action"] for e in self.app.stock_history("T")["events"]], ["restock", "place"])

    def test_all_unmanaged_is_noop_and_creates_nothing(self):
        empty = self.root / "empty"
        empty.mkdir()
        app = OrderDesk(empty)
        app.add_product("T", "Tea", 100)
        app.place("O1", [{"sku": "T", "quantity": 3}])
        raw = app.path.read_bytes()
        result = app.reserve_order("O1")
        self.assertEqual(result["lines"], [{"sku": "T", "quantity": 3, "added": 0, "reserved": 0}])
        self.assertEqual(app.path.read_bytes(), raw)

    def test_repeated_fill_is_idempotent(self):
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.restock("T", 5)
        first = self.app.reserve_order("O1")
        self.assertEqual(first["lines"], [{"sku": "T", "quantity": 3, "added": 3, "reserved": 3}])
        raw = self.app.path.read_bytes()
        second = self.app.reserve_order("O1")
        # The second call reports nothing new and writes no file or events.
        self.assertEqual(second["lines"], [{"sku": "T", "quantity": 3, "added": 0, "reserved": 3}])
        self.assertEqual(self.app.path.read_bytes(), raw)
        self.assertEqual(self.app.stock("T")["reserved"], 3)
        self.assertEqual([e["action"] for e in self.app.history("O1")["events"]], ["place", "reserve-order"])

    def test_validation_errors(self):
        self.app.restock("T", 2)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.app.cancel("O1")
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        self.app.ship("O2", "DHL", "1")
        for bad in (None, 123, 1.5, b"O1", ["O1"], {"x": 1}, "   ", "\t\n"):
            with self.assertRaises(ValueError, msg=bad):
                self.app.reserve_order(bad)
        with self.assertRaises(ValueError):
            self.app.reserve_order("missing")
        with self.assertRaises(ValueError):
            self.app.reserve_order("O1")  # cancelled
        with self.assertRaises(ValueError):
            self.app.reserve_order("O2")  # shipped
        with self.assertRaises(ValueError):
            self.app.reserve_order("o1")  # case sensitive

    def test_order_product_missing_from_catalog_rejected(self):
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.app.restock("T", 1)
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        del data["products"]["T"]
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        raw = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            OrderDesk(self.root).reserve_order("O1")
        self.assertEqual(self.app.path.read_bytes(), raw)

    def test_failure_creates_no_root(self):
        empty = self.root / "empty"
        app = OrderDesk(empty)
        with self.assertRaises(ValueError):
            app.reserve_order("ghost")
        self.assertFalse(empty.exists())

    def test_order_history_event(self):
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.restock("T", 2)
        result = self.app.reserve_order("O1")
        history = self.app.history("O1")
        self.assertTrue(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                         [(1, "place"), (2, "reserve-order")])
        self.assertEqual(set(history["events"][1]), {"sequence", "action", "result"})
        self.assertEqual(history["events"][1]["result"], result)
        reopened = OrderDesk(self.root).history("O1")
        self.assertEqual(reopened, history)

    def test_stock_history_event_chains_snapshots(self):
        self.app.place("O1", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])
        self.app.restock("T", 5)
        self.app.restock("C", 5)
        self.app.reserve_order("O1")
        events = self.app.stock_history("T")["events"]
        self.assertEqual([(e["sequence"], e["action"], e["reference_id"]) for e in events],
                         [(1, "restock", None), (2, "reserve-order", "O1")])
        self.assertEqual(events[1]["before"], {"sku": "T", "on_hand": 5, "reserved": 0, "available": 5})
        self.assertEqual(events[1]["after"], {"sku": "T", "on_hand": 5, "reserved": 2, "available": 3})
        c_events = self.app.stock_history("C")["events"]
        self.assertEqual(c_events[1]["before"], {"sku": "C", "on_hand": 5, "reserved": 0, "available": 5})
        self.assertEqual(c_events[1]["after"], {"sku": "C", "on_hand": 5, "reserved": 1, "available": 4})

    def test_legacy_histories_start_at_one_and_incomplete(self):
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "orders": {"OLD": {"order_id": "OLD", "status": "placed",
                               "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200}],
                               "total_cents": 200}},
            "inventory": {"T": {"on_hand": 4, "reserved": 0}},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        app.reserve_order("OLD")
        order_history = app.history("OLD")
        self.assertFalse(order_history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in order_history["events"]],
                         [(1, "reserve-order")])
        stock_history = app.stock_history("T")
        self.assertFalse(stock_history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in stock_history["events"]],
                         [(1, "reserve-order")])

    def test_downstream_pick_cancel_ship_amend_use_actual_reservations(self):
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.restock("T", 5)
        self.app.reserve_order("O1")
        pick = self.app.pick_list(["O1"])
        self.assertEqual(pick["lines"][0]["reserved"], 3)
        self.assertEqual(pick["lines"][0]["shortfall"], 0)
        self.app.cancel("O1")
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 5, "reserved": 0, "available": 5})

        self.app.place("O2", [{"sku": "T", "quantity": 3}])
        self.app.reserve_order("O2")
        self.app.ship("O2", "DHL", "1")
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 2, "reserved": 0, "available": 2})

        # Amend after a fill counts the now-actual reservation as own stock.
        self.app.place("O3", [{"sku": "T", "quantity": 1}])
        self.app.reserve_order("O3")
        amended = self.app.amend("O3", [{"sku": "T", "quantity": 2}])
        self.assertEqual(amended["lines"][0]["quantity"], 2)
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 2, "reserved": 2, "available": 0})

    def test_other_orders_reservations_unchanged(self):
        # O1 dates from before T was managed; O2 is placed against real stock.
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.restock("T", 10)
        self.app.place("O2", [{"sku": "T", "quantity": 2}])
        self.app.reserve_order("O1")
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(data["reservations"], {"O2": {"T": 2}, "O1": {"T": 3}})
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 10, "reserved": 5, "available": 5})

    def test_cli_success_and_failure(self):
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.restock("T", 2)
        payload = self.root / "r.json"
        payload.write_text(json.dumps({"order_id": " O1 "}), encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "reserve-order", str(payload)],
                            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout), {"order_id": "O1", "lines": [
            {"sku": "T", "quantity": 2, "added": 2, "reserved": 2},
        ]})
        for bad in ("missing", "   "):
            payload.write_text(json.dumps({"order_id": bad}), encoding="utf-8")
            failed = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "reserve-order", str(payload)],
                                    text=True, capture_output=True)
            self.assertEqual(failed.returncode, 2, failed.stdout)
            self.assertEqual(failed.stdout, "")
            self.assertIn("error", json.loads(failed.stderr))

    def test_cli_array_keeps_earlier_successes(self):
        for order_id in ("A", "B", "C"):
            self.app.place(order_id, [{"sku": "T", "quantity": 1}])
        self.app.restock("T", 2)
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([{"order_id": "A"}, {"order_id": "B"}, {"order_id": "missing"}, {"order_id": "C"}]),
                         encoding="utf-8")
        run = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "reserve-order", str(batch)],
                             text=True, capture_output=True)
        self.assertEqual(run.returncode, 2)
        self.assertEqual(run.stdout, "")
        app = OrderDesk(self.root)
        data = json.loads(app.path.read_text(encoding="utf-8"))
        self.assertEqual(data["reservations"], {"A": {"T": 1}, "B": {"T": 1}})
        self.assertEqual([e["action"] for e in app.history("A")["events"]], ["place", "reserve-order"])
        self.assertEqual([e["action"] for e in app.history("C")["events"]], ["place"])

if __name__ == "__main__":
    unittest.main()
