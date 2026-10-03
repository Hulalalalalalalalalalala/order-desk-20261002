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
        self.app.add_product("F", "Free", 0)

    def test_reduce_keeps_identity_status_and_deal_price(self):
        self.app.restock("T", 10)
        self.app.restock("C", 10)
        placed = self.app.place("O1", [
            {"sku": "T", "quantity": 3},
            {"sku": "C", "quantity": 2},
        ])
        reduced = self.app.reduce_order(" O1 ", [{"sku": " C ", "quantity": 1, "note": "ignored"}])
        self.assertEqual(reduced["order_id"], "O1")
        self.assertEqual(reduced["status"], "placed")
        self.assertEqual(reduced["lines"], [
            {"sku": "T", "quantity": 3, "unit_price_cents": 100, "subtotal_cents": 300},
            {"sku": "C", "quantity": 1, "unit_price_cents": 200, "subtotal_cents": 200},
        ])
        self.assertEqual(reduced["total_cents"], 500)
        self.assertNotIn("shipment", reduced)
        self.assertEqual(self.app.get("O1"), reduced)
        self.assertEqual(self.app.list_orders(), [reduced])
        self.assertEqual(self.app.get("O1")["lines"], placed["lines"][:1] + [
            {"sku": "C", "quantity": 1, "unit_price_cents": 200, "subtotal_cents": 200}
        ])

    def test_reduction_comes_off_last_row_first(self):
        self.app.place("O1", [
            {"sku": "T", "quantity": 2},
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 2},
        ])
        reduced = self.app.reduce_order("O1", [{"sku": "T", "quantity": 3}])
        # Two rows of two, reduce three: the last row vanishes and the first
        # keeps one; the middle row keeps its place.
        self.assertEqual(reduced["lines"], [
            {"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100},
            {"sku": "C", "quantity": 1, "unit_price_cents": 200, "subtotal_cents": 200},
        ])
        self.assertEqual(reduced["total_cents"], 300)

    def test_surviving_duplicate_rows_stay_split_and_ordered(self):
        self.app.place("O1", [
            {"sku": "T", "quantity": 2},
            {"sku": "T", "quantity": 2},
            {"sku": "T", "quantity": 2},
        ])
        reduced = self.app.reduce_order("O1", [{"sku": "T", "quantity": 1}])
        self.assertEqual(reduced["lines"], [
            {"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200},
            {"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200},
            {"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100},
        ])
        reduced = self.app.reduce_order("O1", [{"sku": "T", "quantity": 2}])
        self.assertEqual(reduced["lines"], [
            {"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200},
            {"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100},
        ])

    def test_duplicate_request_lines_merge(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 4}])
        reduced = self.app.reduce_order("O1", [
            {"sku": "T", "quantity": 1},
            {"sku": "T", "quantity": 1},
        ])
        self.assertEqual(reduced["lines"][0]["quantity"], 2)

    def test_zero_price_lines_are_recomputed_and_kept(self):
        self.app.place("O1", [{"sku": "F", "quantity": 3}, {"sku": "T", "quantity": 1}])
        reduced = self.app.reduce_order("O1", [{"sku": "F", "quantity": 2}])
        self.assertEqual(reduced["lines"], [
            {"sku": "F", "quantity": 1, "unit_price_cents": 0, "subtotal_cents": 0},
            {"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100},
        ])
        self.assertEqual(reduced["total_cents"], 100)

    def test_deal_price_is_never_repriced_from_catalog(self):
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        data["products"]["T"]["price_cents"] = 150
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        reduced = OrderDesk(self.root).reduce_order("O1", [{"sku": "T", "quantity": 1}])
        self.assertEqual(reduced["lines"], [
            {"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200},
        ])
        self.assertEqual(reduced["total_cents"], 200)

    def test_releases_only_reservation_difference_and_leaves_other_stock(self):
        self.app.restock("T", 10)
        self.app.restock("C", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 4}, {"sku": "C", "quantity": 2}])
        self.app.place("O2", [{"sku": "T", "quantity": 3}])
        # T: on_hand 10, reserved 7 (O1 4 + O2 3); C: reserved 2.
        self.app.reduce_order("O1", [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 10, "reserved": 6, "available": 4})
        self.assertEqual(self.app.stock("C"),
                         {"sku": "C", "on_hand": 10, "reserved": 2, "available": 8})
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(data["reservations"]["O1"], {"T": 3, "C": 2})
        self.assertEqual(data["reservations"]["O2"], {"T": 3})

    def test_reservation_above_remaining_shrinks_to_remaining(self):
        # Ordered 4 while unmanaged; restock and top up to 4 reservations.
        self.app.place("O1", [{"sku": "T", "quantity": 4}])
        self.app.restock("T", 10)
        self.app.reserve_order("O1")
        self.assertEqual(self.app.stock("T")["reserved"], 4)
        self.app.reduce_order("O1", [{"sku": "T", "quantity": 3}])
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 10, "reserved": 1, "available": 9})

    def test_stray_reservation_larger_than_ordered_is_capped_by_min_rule(self):
        self.app.place("O1", [{"sku": "T", "quantity": 4}])
        self.app.restock("T", 10)
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        data["reservations"] = {"O1": {"T": 5}}
        data["inventory"]["T"]["reserved"] = 5
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        # Reducing by 1 leaves 3 ordered; the reservation becomes min(5, 3)=3,
        # releasing 2 rather than a mere 1.
        OrderDesk(self.root).reduce_order("O1", [{"sku": "T", "quantity": 1}])
        self.assertEqual(OrderDesk(self.root).stock("T"),
                         {"sku": "T", "on_hand": 10, "reserved": 3, "available": 7})

    def test_zeroed_reservation_loses_its_attribution(self):
        self.app.restock("T", 10)
        self.app.restock("C", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])
        self.app.reduce_order("O1", [{"sku": "T", "quantity": 2}])
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(data["reservations"]["O1"], {"C": 1})
        # F is an unmanaged product that holds no reservation, so the order can
        # survive while its last managed reservation is released: the whole
        # attribution record is then dropped rather than left as {}.
        self.app.place("O2", [{"sku": "T", "quantity": 2}, {"sku": "F", "quantity": 1}])
        self.app.reduce_order("O2", [{"sku": "T", "quantity": 2}])
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("O2", data.get("reservations", {}))
        self.assertEqual(self.app.get("O2")["lines"], [
            {"sku": "F", "quantity": 1, "unit_price_cents": 0, "subtotal_cents": 0},
        ])
        self.assertEqual(self.app.stock("T")["reserved"], 0)
        self.assertEqual(self.app.stock("C")["reserved"], 1)

    def test_unmanaged_product_has_no_reservation_and_no_stock_event(self):
        self.app.place("O1", [{"sku": "T", "quantity": 5}])
        reduced = self.app.reduce_order("O1", [{"sku": "T", "quantity": 2}])
        self.assertEqual(reduced["lines"][0]["quantity"], 3)
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": None, "reserved": 0, "available": None})
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("inventory", data)
        self.assertNotIn("reservations", data)
        self.assertEqual(self.app.stock_history("T")["events"], [])

    def test_legacy_order_without_reservation_record_releases_nothing(self):
        self.app.restock("T", 10)
        self.app.place("O2", [{"sku": "T", "quantity": 2}])
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        data["orders"]["OLD"] = {
            "order_id": "OLD", "status": "placed",
            "lines": [{"sku": "T", "quantity": 3, "unit_price_cents": 100, "subtotal_cents": 300}],
            "total_cents": 300,
        }
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        reduced = app.reduce_order("OLD", [{"sku": "T", "quantity": 1}])
        self.assertEqual(reduced["lines"][0]["quantity"], 2)
        # Only O2's reservation remains; no stock event is recorded for T.
        self.assertEqual(app.stock("T"),
                         {"sku": "T", "on_hand": 10, "reserved": 2, "available": 8})
        events = app.stock_history("T")["events"]
        self.assertEqual([e["action"] for e in events], ["restock", "place"])

    def test_paused_missing_and_short_products_never_block_reduction(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 2}])
        self.app.set_product_enabled("C", False)
        # Available for T is zero; reducing still succeeds and frees stock.
        reduced = self.app.reduce_order("O1", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 5, "reserved": 3, "available": 2})
        self.assertEqual([line["sku"] for line in reduced["lines"]], ["T", "C"])
        # Now drop C from the catalog entirely; reducing C still works.
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        del data["products"]["C"]
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        reduced = OrderDesk(self.root).reduce_order("O1", [{"sku": "C", "quantity": 1}])
        self.assertEqual([line["sku"] for line in reduced["lines"]], ["T"])

    def test_repeat_request_is_judged_against_current_quantity(self):
        self.app.place("O1", [{"sku": "T", "quantity": 4}])
        self.app.reduce_order("O1", [{"sku": "T", "quantity": 2}])
        self.app.reduce_order("O1", [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.get("O1")["lines"][0]["quantity"], 1)
        with self.assertRaises(ValueError):
            self.app.reduce_order("O1", [{"sku": "T", "quantity": 2}])
        self.assertEqual(self.app.get("O1")["lines"][0]["quantity"], 1)

    def test_validation_errors(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.cancel("O1")
        self.app.place("O2", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])
        good = [{"sku": "T", "quantity": 1}]
        for order_id, lines in (
            ("missing", good),            # unknown order
            ("O1", good),                 # cancelled
            (None, good), (123, good), ("   ", good),
            ("O2", None), ("O2", "x"), ("O2", []),
            ("O2", ["x"]), ("O2", [{"quantity": 1}]), ("O2", [{"sku": "T"}]),
            ("O2", [{"sku": "  ", "quantity": 1}]),
            ("O2", [{"sku": "T", "quantity": 0}]),
            ("O2", [{"sku": "T", "quantity": -1}]),
            ("O2", [{"sku": "T", "quantity": 1.5}]),
            ("O2", [{"sku": "T", "quantity": True}]),
            ("O2", [{"sku": "T", "quantity": "1"}]),
            ("O2", [{"sku": "t", "quantity": 1}]),       # case-sensitive
            ("O2", [{"sku": "X", "quantity": 1}]),       # not in order
            ("O2", [{"sku": "T", "quantity": 3}]),       # merged over-quantity
            ("O2", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}]),  # whole order
        ):
            with self.assertRaises(ValueError, msg=(order_id, lines)):
                self.app.reduce_order(order_id, lines)

    def test_reducing_last_remaining_line_is_rejected_use_cancel(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        with self.assertRaises(ValueError):
            self.app.reduce_order("O1", [{"sku": "T", "quantity": 2}])
        self.assertEqual(self.app.get("O1")["status"], "placed")
        self.assertEqual(self.app.get("O1")["lines"][0]["quantity"], 2)
        self.assertEqual(self.app.stock("T")["reserved"], 2)
        # Whole-order cancellation still goes through cancel.
        self.app.cancel("O1")
        self.assertEqual(self.app.get("O1")["status"], "cancelled")

    def test_shipped_order_cannot_be_reduced(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.ship("O1", "DHL", "1")
        with self.assertRaises(ValueError):
            self.app.reduce_order("O1", [{"sku": "T", "quantity": 1}])

    def test_failed_reduce_does_not_rewrite_or_consume_sequence(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        raw = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.reduce_order("O1", [{"sku": "T", "quantity": 9}])
        self.assertEqual(self.app.path.read_bytes(), raw)
        self.assertEqual([e["action"] for e in self.app.history("O1")["events"]], ["place"])
        self.assertEqual(self.app.stock("T")["reserved"], 2)

    def test_failed_reduce_creates_no_root(self):
        empty = self.root / "empty"
        app = OrderDesk(empty)
        with self.assertRaises(ValueError):
            app.reduce_order("ghost", [{"sku": "T", "quantity": 1}])
        self.assertFalse(empty.exists())

    def test_history_event_holds_full_order_snapshot_and_sequences_continue(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        first = self.app.reduce_order("O1", [{"sku": "T", "quantity": 1}])
        second = self.app.reduce_order("O1", [{"sku": "T", "quantity": 1}])
        history = self.app.history("O1")
        self.assertTrue(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                         [(1, "place"), (2, "reduce-order"), (3, "reduce-order")])
        self.assertEqual(history["events"][1]["result"], first)
        self.assertEqual(history["events"][2]["result"], second)
        self.assertEqual(history["events"][0]["result"]["lines"][0]["quantity"], 3)
        reopened = OrderDesk(self.root).history("O1")
        self.assertEqual(reopened, history)
        self.assertEqual(OrderDesk(self.root).get("O1"), second)

    def test_stock_history_records_only_actual_releases(self):
        self.app.restock("T", 10)
        self.app.restock("C", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 4}, {"sku": "C", "quantity": 2}])
        self.app.reduce_order("O1", [{"sku": "T", "quantity": 3}])
        events = self.app.stock_history("T")["events"]
        self.assertEqual([(e["action"], e["reference_id"]) for e in events[-1:]],
                         [("reduce-order", "O1")])
        before, after = events[-1]["before"], events[-1]["after"]
        self.assertEqual(before, {"sku": "T", "on_hand": 10, "reserved": 4, "available": 6})
        self.assertEqual(after, {"sku": "T", "on_hand": 10, "reserved": 1, "available": 9})
        # C was part of the order but not involved: no reduce event.
        self.assertEqual(
            [e["action"] for e in self.app.stock_history("C")["events"]],
            ["restock", "place"],
        )

    def test_legacy_order_reduce_starts_history_at_one(self):
        data = {"products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
                "orders": {"OLD": {"order_id": "OLD", "status": "placed",
                                   "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100,
                                              "subtotal_cents": 200}],
                                   "total_cents": 200}}}
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        reduced = app.reduce_order("OLD", [{"sku": "T", "quantity": 1}])
        history = app.history("OLD")
        self.assertFalse(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                         [(1, "reduce-order")])
        self.assertEqual(history["events"][0]["result"], reduced)

    def test_cancel_and_ship_after_reduce_use_remaining_reservations(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 5}])
        self.app.reduce_order("O1", [{"sku": "T", "quantity": 2}])
        self.app.ship("O1", "DHL", "1")
        # Only the remaining 3 reservations were deducted from on_hand.
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 2, "reserved": 0, "available": 2})

        self.app.restock("C", 5)
        self.app.place("O2", [{"sku": "C", "quantity": 5}])
        self.app.reduce_order("O2", [{"sku": "C", "quantity": 2}])
        self.app.cancel("O2")
        self.assertEqual(self.app.stock("C"),
                         {"sku": "C", "on_hand": 5, "reserved": 0, "available": 5})

    def test_returns_are_capped_at_remaining_quantity(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 5}])
        self.app.reduce_order("O1", [{"sku": "T", "quantity": 2}])
        self.app.ship("O1", "DHL", "1")
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        with self.assertRaises(ValueError):
            self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        remaining = self.app.get_returns("O1")["remaining"]
        self.assertEqual(remaining, [{"sku": "T", "quantity": 0}])

    def test_fulfillment_queries_reflect_remaining_content(self):
        self.app.restock("T", 10)
        self.app.restock("C", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 4}, {"sku": "C", "quantity": 2}])
        self.app.reduce_order("O1", [{"sku": "T", "quantity": 1}, {"sku": "C", "quantity": 2}])
        progress = self.app.order_progress("O1")
        self.assertEqual([(line["sku"], line["ordered"], line["reserved"]) for line in progress["lines"]],
                         [("T", 3, 3)])
        pick = self.app.pick_list(["O1"])
        self.assertEqual([(line["sku"], line["quantity"], line["reserved"]) for line in pick["lines"]],
                         [("T", 3, 3)])
        audit = self.app.reservation_audit("C")
        self.assertEqual(audit["orders"], [])
        self.assertEqual(audit["stock"]["reserved"], 0)
        self.assertEqual(
            [task for entry in self.app.order_worklist("all") if entry["order_id"] == "O1"
             for task in entry["tasks"]],
            ["ship"],
        )

    def test_amend_after_reduce_keeps_its_own_repricing_behavior(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 4}])
        self.app.reduce_order("O1", [{"sku": "T", "quantity": 1}])
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        data["products"]["T"]["price_cents"] = 150
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        amended = OrderDesk(self.root).amend("O1", [{"sku": "T", "quantity": 2}])
        self.assertEqual(amended["lines"][0]["unit_price_cents"], 150)
        self.assertEqual(amended["total_cents"], 300)

    def test_cli_reduce_success_and_failure(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        payload = self.root / "a.json"
        payload.write_text(json.dumps({"order_id": " O1 ", "lines": [{"sku": "T", "quantity": 1}]}),
                           encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "reduce-order", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual(result["total_cents"], 200)
        payload.write_text(json.dumps({"order_id": "O1", "lines": [{"sku": "T", "quantity": 9}]}),
                           encoding="utf-8")
        failed = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "reduce-order", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(failed.stdout, "")
        self.assertIn("error", json.loads(failed.stderr))
        self.assertEqual(OrderDesk(self.root).get("O1")["total_cents"], 200)

    def test_cli_reduce_array_keeps_successes_after_failure(self):
        self.app.restock("T", 10)
        self.app.place("A", [{"sku": "T", "quantity": 3}])
        self.app.place("B", [{"sku": "T", "quantity": 3}])
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([
            {"order_id": "A", "lines": [{"sku": "T", "quantity": 1}]},
            {"order_id": "missing", "lines": [{"sku": "T", "quantity": 1}]},
            {"order_id": "B", "lines": [{"sku": "T", "quantity": 1}]},
        ]), encoding="utf-8")
        run = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "reduce-order", str(batch)],
            text=True, capture_output=True)
        self.assertEqual(run.returncode, 2)
        self.assertEqual(run.stdout, "")
        app = OrderDesk(self.root)
        self.assertEqual(app.get("A")["lines"][0]["quantity"], 2)
        self.assertEqual(app.get("B")["lines"][0]["quantity"], 3)
        self.assertEqual([e["action"] for e in app.history("A")["events"]], ["place", "reduce-order"])
        self.assertEqual([e["action"] for e in app.history("B")["events"]], ["place"])


if __name__ == "__main__":
    unittest.main()
