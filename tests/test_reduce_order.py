import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk

class ReduceOrderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)

    def test_reduce_keeps_identity_deal_prices_and_order(self):
        self.app.restock("T", 10)
        self.app.restock("C", 10)
        self.app.place("O1", [
            {"sku": "T", "quantity": 2},
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 2},
        ])
        # Reprice the catalog after the deal; the reduction must not reprice.
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        data["products"]["T"]["price_cents"] = 150
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        reduced = self.app.reduce_order(" O1 ", [{"sku": " T ", "quantity": 3, "note": "ignored"}])
        self.assertEqual(reduced["order_id"], "O1")
        self.assertEqual(reduced["status"], "placed")
        self.assertNotIn("shipment", reduced)
        # The last T line is consumed first; the surviving lines keep their
        # relative order and deal prices and are not merged.
        self.assertEqual(reduced["lines"], [
            {"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100},
            {"sku": "C", "quantity": 1, "unit_price_cents": 200, "subtotal_cents": 200},
        ])
        self.assertEqual(reduced["total_cents"], 300)
        self.assertEqual(self.app.get("O1"), reduced)
        self.assertEqual(self.app.list_orders(), [reduced])

    def test_same_sku_deducts_from_last_line_backwards(self):
        self.app.place("O1", [{"sku": "T", "quantity": 2}, {"sku": "T", "quantity": 2}])
        reduced = self.app.reduce_order("O1", [{"sku": "T", "quantity": 3}])
        self.assertEqual(reduced["lines"],
                         [{"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100}])
        self.assertEqual(reduced["total_cents"], 100)

    def test_duplicate_request_lines_merge_and_zero_price_lines_survive(self):
        self.app.add_product("Z", "Free sample", 0)
        self.app.place("O1", [
            {"sku": "Z", "quantity": 1},
            {"sku": "T", "quantity": 3},
        ])
        reduced = self.app.reduce_order("O1", [
            {"sku": "T", "quantity": 1},
            {"sku": "T", "quantity": 1},
        ])
        self.assertEqual(reduced["lines"], [
            {"sku": "Z", "quantity": 1, "unit_price_cents": 0, "subtotal_cents": 0},
            {"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100},
        ])
        self.assertEqual(reduced["total_cents"], 100)

    def test_reduce_releases_only_the_reservation_difference(self):
        self.app.restock("T", 10)
        self.app.restock("C", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 4}, {"sku": "C", "quantity": 2}])
        self.app.place("O2", [{"sku": "T", "quantity": 2}])
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 10, "reserved": 6, "available": 4})
        self.app.reduce_order("O1", [{"sku": "T", "quantity": 1}, {"sku": "C", "quantity": 2}])
        # This order's reservation drops to the remaining demand; the product
        # total falls and availability rises by the released amount only.
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 10, "reserved": 5, "available": 5})
        self.assertEqual(self.app.stock("C"), {"sku": "C", "on_hand": 5, "reserved": 0, "available": 5})
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        # The zeroed attribution is removed; the other order is untouched.
        self.assertEqual(data["reservations"], {"O1": {"T": 3}, "O2": {"T": 2}})

    def test_reservation_capped_at_actual_held(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        # Release part of the reservation first: the order holds less than it
        # has ordered, and a later reduction caps at what is actually held.
        self.app.release_reservation("O1", [{"sku": "T", "quantity": 2}])
        self.app.reduce_order("O1", [{"sku": "T", "quantity": 2}])
        # Remaining demand is 1 and the order actually holds 1: nothing is
        # released and no stock event is appended.
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 5, "reserved": 1, "available": 4})
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(data["reservations"], {"O1": {"T": 1}})
        self.assertNotIn("reduce-order",
                         [e["action"] for e in self.app.stock_history("T")["events"]])

    def test_missing_inventory_or_reservation_record_reads_as_none(self):
        # Unmanaged product: never reserved, never auto-managed by a reduce.
        self.app.place("O1", [{"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 2}])
        reduced = self.app.reduce_order("O1", [{"sku": "T", "quantity": 1}, {"sku": "C", "quantity": 1}])
        self.assertEqual(reduced["total_cents"], 2 * 100 + 200)
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": None, "reserved": 0, "available": None})
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("reservations", data)
        self.assertNotIn("inventory", data)
        # Managed order whose reservation record is removed underneath it:
        # the reduce treats it as unreserved and does not backfill, so the
        # inventory counter is left exactly as it was.
        self.app.restock("T", 5)
        self.app.place("O2", [{"sku": "T", "quantity": 3}])
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        del data["reservations"]["O2"]
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        self.app.reduce_order("O2", [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 5, "reserved": 3, "available": 2})
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("O2", data.get("reservations", {}))

    def test_paused_or_catalogless_products_never_block(self):
        self.app.restock("T", 5)
        self.app.restock("C", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 2}])
        self.app.set_product_enabled("T", False)
        # A catalog entry deleted behind the system's back must not block
        # giving quantities back either.
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        del data["products"]["C"]
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        reduced = self.app.reduce_order("O1", [{"sku": "T", "quantity": 1}, {"sku": "C", "quantity": 1}])
        self.assertEqual(reduced["lines"], [
            {"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100},
            {"sku": "C", "quantity": 1, "unit_price_cents": 200, "subtotal_cents": 200},
        ])
        self.assertEqual(self.app.stock("T")["reserved"], 1)
        # C is gone from the catalog, so inspect its inventory record directly.
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(data["inventory"]["C"], {"on_hand": 5, "reserved": 1})
        self.assertEqual(data["reservations"]["O1"], {"T": 1, "C": 1})

    def test_reduce_validation_errors(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.app.cancel("O1")
        self.app.place("O2", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])
        good = [{"sku": "T", "quantity": 1}]
        for order_id, lines in (
            ("missing", good),            # unknown order
            ("O1", good),                 # not placed
            (None, good), (123, good), ("   ", good),
            ("O2", None), ("O2", "x"), ("O2", []),
            ("O2", ["x"]), ("O2", [{"quantity": 1}]), ("O2", [{"sku": "T"}]),
            ("O2", [{"sku": "  ", "quantity": 1}]),
            ("O2", [{"sku": "t", "quantity": 1}]),   # case-sensitive
            ("O2", [{"sku": "T", "quantity": 0}]),
            ("O2", [{"sku": "T", "quantity": -1}]),
            ("O2", [{"sku": "T", "quantity": 1.5}]),
            ("O2", [{"sku": "T", "quantity": True}]),
            ("O2", [{"sku": "T", "quantity": "1"}]),
            ("O2", [{"sku": "X", "quantity": 1}]),   # sku not in order
            ("O2", [{"sku": "T", "quantity": 3}]),   # exceeds ordered
            ("O2", [{"sku": "T", "quantity": 2}, {"sku": "T", "quantity": 1}]),  # merged exceeds
            ("O2", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}]),  # empties the order
        ):
            with self.assertRaises(ValueError, msg=(order_id, lines)):
                self.app.reduce_order(order_id, lines)

    def test_whole_order_reduction_is_a_cancellation(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        with self.assertRaises(ValueError):
            self.app.reduce_order("O1", [{"sku": "T", "quantity": 2}])
        self.assertEqual(self.app.get("O1")["status"], "placed")
        self.app.cancel("O1")
        self.assertEqual(self.app.get("O1")["status"], "cancelled")

    def test_repeated_request_is_judged_against_current_quantities(self):
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        reduced = self.app.reduce_order("O1", [{"sku": "T", "quantity": 1}])
        self.assertEqual(reduced["lines"][0]["quantity"], 1)
        # The same request again now exceeds the remaining quantity.
        with self.assertRaises(ValueError):
            self.app.reduce_order("O1", [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.get("O1"), reduced)

    def test_failed_reduce_does_not_rewrite_or_consume_sequence(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        raw = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.reduce_order("O1", [{"sku": "T", "quantity": 3}])
        self.assertEqual(self.app.path.read_bytes(), raw)
        self.assertEqual([e["action"] for e in self.app.history("O1")["events"]], ["place"])
        self.assertEqual(self.app.stock_history("T")["events"][-1]["action"], "place")

    def test_failed_reduce_creates_no_root(self):
        empty = self.root / "empty"
        app = OrderDesk(empty)
        with self.assertRaises(ValueError):
            app.reduce_order("ghost", [{"sku": "T", "quantity": 1}])
        self.assertFalse(empty.exists())

    def test_reduce_history_and_stock_events(self):
        self.app.restock("T", 10)
        self.app.restock("C", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 2}])
        first = self.app.reduce_order("O1", [{"sku": "T", "quantity": 1}])
        second = self.app.reduce_order("O1", [{"sku": "C", "quantity": 2}])
        history = self.app.history("O1")
        self.assertTrue(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                         [(1, "place"), (2, "reduce-order"), (3, "reduce-order")])
        self.assertEqual(history["events"][1]["result"], first)
        self.assertEqual(history["events"][2]["result"], second)
        # The place snapshot keeps the original lines.
        self.assertEqual(history["events"][0]["result"]["total_cents"], 3 * 100 + 2 * 200)
        # Only skus with an actually released reservation get a stock event.
        tea_events = [(e["sequence"], e["action"], e["reference_id"])
                      for e in self.app.stock_history("T")["events"]]
        self.assertEqual(tea_events, [(1, "restock", None), (2, "place", "O1"), (3, "reduce-order", "O1")])
        tea_release = self.app.stock_history("T")["events"][-1]
        self.assertEqual(tea_release["before"], {"sku": "T", "on_hand": 10, "reserved": 3, "available": 7})
        self.assertEqual(tea_release["after"], {"sku": "T", "on_hand": 10, "reserved": 2, "available": 8})
        coffee_events = [(e["action"], e["reference_id"]) for e in self.app.stock_history("C")["events"]]
        self.assertEqual(coffee_events, [("restock", None), ("place", "O1"), ("reduce-order", "O1")])
        # Persisted in the same write; reopening agrees.
        self.assertEqual(OrderDesk(self.root).history("O1"), history)
        self.assertEqual(OrderDesk(self.root).get("O1"), second)

    def test_reduce_without_release_appends_no_stock_event(self):
        # Unmanaged product: the order event is recorded but no stock event.
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.reduce_order("O1", [{"sku": "T", "quantity": 1}])
        self.assertEqual([e["action"] for e in self.app.history("O1")["events"]],
                         ["place", "reduce-order"])
        self.assertEqual(self.app.stock_history("T"), {"sku": "T", "complete": True, "events": []})

    def test_legacy_order_reduce_starts_history_at_one(self):
        data = {"products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
                "orders": {"OLD": {"order_id": "OLD", "status": "placed",
                                   "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200}],
                                   "total_cents": 200}}}
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        reduced = app.reduce_order("OLD", [{"sku": "T", "quantity": 1}])
        history = app.history("OLD")
        self.assertFalse(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]], [(1, "reduce-order")])
        self.assertEqual(history["events"][0]["result"], reduced)

    def test_cancel_and_ship_after_reduce_use_remaining_reservations(self):
        self.app.restock("T", 5)
        self.app.restock("C", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 2}])
        self.app.reduce_order("O1", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])
        self.app.cancel("O1")
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 5, "reserved": 0, "available": 5})
        self.assertEqual(self.app.stock("C"), {"sku": "C", "on_hand": 5, "reserved": 0, "available": 5})
        self.app.place("O2", [{"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 2}])
        self.app.reduce_order("O2", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])
        self.app.ship("O2", "DHL", "1")
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 4, "reserved": 0, "available": 4})
        self.assertEqual(self.app.stock("C"), {"sku": "C", "on_hand": 4, "reserved": 0, "available": 4})

    def test_queries_and_returns_reflect_remaining_content(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 4}])
        self.app.reduce_order("O1", [{"sku": "T", "quantity": 3}])
        progress = self.app.order_progress("O1")
        self.assertEqual(progress["lines"][0]["ordered"], 1)
        self.assertEqual(progress["lines"][0]["reserved"], 1)
        pick = self.app.pick_list(["O1"])
        self.assertEqual(pick["lines"][0]["quantity"], 1)
        self.assertEqual(pick["lines"][0]["reserved"], 1)
        self.app.ship("O1", "DHL", "1")
        # The return allowance is capped by the remaining ordered quantity.
        with self.assertRaises(ValueError):
            self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        record = self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.assertEqual(record["lines"], [{"sku": "T", "quantity": 1}])

    def test_amend_after_reduce_reprices_while_reduce_keeps_deal_prices(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        data["products"]["T"]["price_cents"] = 150
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        reduced = self.app.reduce_order("O1", [{"sku": "T", "quantity": 1}])
        self.assertEqual(reduced["lines"][0]["unit_price_cents"], 100)
        self.assertEqual(reduced["total_cents"], 200)
        # amend keeps its existing behavior and reprices from the catalog.
        amended = self.app.amend("O1", [{"sku": "T", "quantity": 2}])
        self.assertEqual(amended["lines"][0]["unit_price_cents"], 150)
        self.assertEqual(amended["total_cents"], 300)

    def test_cli_reduce_order_success_and_failure(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        payload = self.root / "r.json"
        payload.write_text(json.dumps({"order_id": " O1 ", "lines": [{"sku": "T", "quantity": 1}]}),
                           encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "reduce-order", str(payload)],
                            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual(result["total_cents"], 200)
        self.assertEqual(result["lines"][0]["quantity"], 2)
        payload.write_text(json.dumps({"order_id": "O1", "lines": [{"sku": "T", "quantity": 99}]}),
                           encoding="utf-8")
        failed = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "reduce-order", str(payload)],
                                text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(failed.stdout, "")
        self.assertIn("error", json.loads(failed.stderr))
        self.assertEqual(OrderDesk(self.root).get("O1")["total_cents"], 200)

    def test_cli_reduce_order_array_stops_at_error_and_keeps_successes(self):
        self.app.restock("T", 10)
        self.app.place("A", [{"sku": "T", "quantity": 2}])
        self.app.place("B", [{"sku": "T", "quantity": 2}])
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([
            {"order_id": "A", "lines": [{"sku": "T", "quantity": 1}]},
            {"order_id": "missing", "lines": [{"sku": "T", "quantity": 1}]},
            {"order_id": "B", "lines": [{"sku": "T", "quantity": 1}]},
        ]), encoding="utf-8")
        run = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "reduce-order", str(batch)],
                             text=True, capture_output=True)
        self.assertEqual(run.returncode, 2)
        self.assertEqual(run.stdout, "")
        app = OrderDesk(self.root)
        self.assertEqual(app.get("A")["lines"][0]["quantity"], 1)
        self.assertEqual(app.get("B")["lines"][0]["quantity"], 2)
        self.assertEqual([e["action"] for e in app.history("A")["events"]], ["place", "reduce-order"])
        self.assertEqual([e["action"] for e in app.history("B")["events"]], ["place"])

if __name__ == "__main__":
    unittest.main()
