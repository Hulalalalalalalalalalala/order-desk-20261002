import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk


class ReservationPlanTests(unittest.TestCase):
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

    def test_priority_order_decides_who_can_complete(self):
        # Both orders are placed while the products are unmanaged, so neither
        # holds a reservation; restocking afterwards creates the shared margin.
        self.app.place("O1", [{"sku": "T", "quantity": 4}, {"sku": "C", "quantity": 5}])
        self.app.place("O2", [{"sku": "T", "quantity": 7}])
        self.app.restock("T", 10)
        self.app.restock("C", 5)
        result = self.app.reservation_plan(["O2", "O1"])
        self.assertEqual([item["order_id"] for item in result], ["O2", "O1"])
        for item in result:
            self.assertEqual(set(item), {"order_id", "can_reserve", "lines"})
        o2, o1 = result
        self.assertTrue(o2["can_reserve"])
        self.assertEqual(o2["lines"], [
            {"sku": "T", "quantity": 7, "reserved": 0, "available": 10, "shortfall": 0},
        ])
        self.assertFalse(o1["can_reserve"])
        # O2 consumed 7 of T; O1 sees 3 and falls short by 1. C was never
        # touched by O2 and still shows the full margin.
        self.assertEqual(o1["lines"], [
            {"sku": "C", "quantity": 5, "reserved": 0, "available": 5, "shortfall": 0},
            {"sku": "T", "quantity": 4, "reserved": 0, "available": 3, "shortfall": 1},
        ])
        # Reversing the priority reverses the outcome.
        result = self.app.reservation_plan(["O1", "O2"])
        self.assertTrue(result[0]["can_reserve"])
        self.assertEqual(self._line(result, "O1", "T")["available"], 10)
        self.assertFalse(result[1]["can_reserve"])
        self.assertEqual(self._line(result, "O2", "T")["available"], 6)
        self.assertEqual(self._line(result, "O2", "T")["shortfall"], 1)

    def test_failed_order_consumes_nothing_and_later_orders_continue(self):
        self.app.place("O1", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 10}])
        self.app.place("O2", [{"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 5}])
        self.app.restock("T", 5)
        self.app.restock("C", 5)
        result = self.app.reservation_plan(["O1", "O2"])
        self.assertFalse(result[0]["can_reserve"])
        # The fitting T line of the failing order is retained with zero
        # shortfall; the blocking C line shows the real gap.
        self.assertEqual(self._line(result, "O1", "T"),
                         {"sku": "T", "quantity": 2, "reserved": 0, "available": 5, "shortfall": 0})
        self.assertEqual(self._line(result, "O1", "C"),
                         {"sku": "C", "quantity": 10, "reserved": 0, "available": 5, "shortfall": 5})
        # O1 consumed neither product: O2 can complete against the full margin.
        self.assertTrue(result[1]["can_reserve"])
        self.assertEqual(self._line(result, "O2", "T")["available"], 5)
        self.assertEqual(self._line(result, "O2", "C")["available"], 5)

    def test_existing_reservations_reduce_new_demand_and_seed_margin(self):
        # B was ordered while T was unmanaged and holds no reservation; A was
        # placed against managed stock and holds 2.
        self.app.place("B", [{"sku": "T", "quantity": 3}])
        self.app.restock("T", 5)
        self.app.place("A", [{"sku": "T", "quantity": 2}])
        self.app.restock("T", 5)
        # On hand 10, A holds 2: the shared margin for new demand is 8.
        result = self.app.reservation_plan(["A", "B"])
        self.assertEqual([item["order_id"] for item in result], ["A", "B"])
        a, b = result
        self.assertTrue(a["can_reserve"])
        self.assertEqual(a["lines"], [
            {"sku": "T", "quantity": 2, "reserved": 2, "available": 8, "shortfall": 0},
        ])
        # A has no new demand, so nothing is consumed; B needs three fresh units
        # against the same margin and completes, consuming it (visible only to
        # orders later than B).
        self.assertTrue(b["can_reserve"])
        self.assertEqual(b["lines"], [
            {"sku": "T", "quantity": 3, "reserved": 0, "available": 8, "shortfall": 0},
        ])

    def test_reservation_held_by_order_placed_while_unmanaged_counts_as_zero(self):
        # O1 ordered unmanaged, then T became managed with stock for only part.
        self.app.place("O1", [{"sku": "T", "quantity": 5}])
        self.app.restock("T", 3)
        result = self.app.reservation_plan(["O1"])
        self.assertFalse(result[0]["can_reserve"])
        self.assertEqual(result[0]["lines"], [
            {"sku": "T", "quantity": 5, "reserved": 0, "available": 3, "shortfall": 2},
        ])
        self.app.restock("T", 2)
        result = self.app.reservation_plan(["O1"])
        self.assertTrue(result[0]["can_reserve"])
        self.assertEqual(result[0]["lines"][0]["available"], 5)
        self.assertEqual(result[0]["lines"][0]["shortfall"], 0)

    def test_duplicate_skus_merge_and_lines_sort_with_zero_gap_kept(self):
        self.app.place("O1", [
            {"sku": "T", "quantity": 2},
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 1},
        ])
        self.app.restock("T", 10)
        self.app.restock("C", 10)
        result = self.app.reservation_plan(["O1"])[0]
        self.assertEqual([line["sku"] for line in result["lines"]], ["C", "T"])
        for line in result["lines"]:
            self.assertEqual(set(line), {"sku", "quantity", "reserved", "available", "shortfall"})
        self.assertEqual(result["lines"][0]["quantity"], 1)
        self.assertEqual(result["lines"][1]["quantity"], 3)

    def test_unmanaged_product_is_unlimited_and_consumes_no_margin(self):
        self.app.place("O1", [{"sku": "U", "quantity": 1000}, {"sku": "T", "quantity": 6}])
        self.app.place("O2", [{"sku": "T", "quantity": 5}])
        self.app.restock("T", 5)
        result = self.app.reservation_plan(["O1", "O2"])
        # O1 fails on T, despite the unlimited unmanaged U being coverable.
        self.assertFalse(result[0]["can_reserve"])
        u = self._line(result, "O1", "U")
        self.assertEqual(u, {"sku": "U", "quantity": 1000, "reserved": 0,
                             "available": None, "shortfall": 0})
        self.assertEqual(self._line(result, "O1", "T")["shortfall"], 1)
        # The failed O1 consumed nothing, so O2 takes the full T margin.
        self.assertTrue(result[1]["can_reserve"])
        self.assertEqual(self._line(result, "O2", "T"),
                         {"sku": "T", "quantity": 5, "reserved": 0, "available": 5, "shortfall": 0})

    def test_unmanaged_line_in_completable_order_stays_null(self):
        self.app.place("O1", [{"sku": "U", "quantity": 99}, {"sku": "T", "quantity": 2}])
        self.app.restock("T", 5)
        result = self.app.reservation_plan(["O1"])
        self.assertTrue(result[0]["can_reserve"])
        self.assertEqual([line["sku"] for line in result[0]["lines"]], ["T", "U"])
        self.assertIsNone(self._line(result, "O1", "U")["available"])

    def test_paused_sales_do_not_block_preview(self):
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.restock("T", 5)
        self.app.set_product_enabled("T", False)
        result = self.app.reservation_plan(["O1"])[0]
        self.assertTrue(result["can_reserve"])
        self.assertEqual(result["lines"][0]["quantity"], 2)

    def test_legacy_data_without_inventory_or_reservations(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}, {"sku": "U", "quantity": 4}])
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        del raw["inventory"]
        raw.pop("reservations", None)
        self.app._write(raw)
        result = OrderDesk(self.root).reservation_plan(["O1"])[0]
        self.assertTrue(result["can_reserve"])
        for line in result["lines"]:
            self.assertIsNone(line["available"])
            self.assertEqual(line["reserved"], 0)
            self.assertEqual(line["shortfall"], 0)

    def test_invalid_order_ids_raise_value_error(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        self.app.cancel("O2")
        bad_inputs = [
            [], {}, "x", 42, None,
            [42], [None], [""], ["   "], ["O1", 3],
            ["O1", " O1 "],            # duplicate after trimming
            ["O1", "O1"],
            ["missing"],
            ["O2"],                    # cancelled, not placed
            ["O1", "missing"],         # unknown after a valid id
        ]
        for payload in bad_inputs:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    self.app.reservation_plan(payload)
        # Case sensitive: the unknown casing raises rather than partial results.
        with self.assertRaises(ValueError):
            self.app.reservation_plan(["O1", "o1"])

    def test_order_line_with_unknown_product_rejects_whole_query(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        data["orders"]["O1"]["lines"].append(
            {"sku": "GONE", "quantity": 1, "unit_price_cents": 50, "subtotal_cents": 50})
        self.app._write(data)
        raw = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            OrderDesk(self.root).reservation_plan(["O2", "O1"])
        self.assertEqual(self.app.path.read_bytes(), raw)

    def test_shipped_order_rejects_whole_query(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.app.ship("O1", "DHL", "1")
        with self.assertRaises(ValueError):
            self.app.reservation_plan(["O1"])

    def test_query_is_read_only_and_repeatable(self):
        self.app.place("O1", [{"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 2}])
        self.app.place("O2", [{"sku": "T", "quantity": 4}])
        self.app.restock("T", 5)
        self.app.restock("C", 5)
        before = self.app.path.read_bytes()
        first = self.app.reservation_plan(["O1", "O2"])
        second = self.app.reservation_plan(["O1", "O2"])
        reopened = OrderDesk(self.root).reservation_plan(["O1", "O2"])
        self.assertEqual(first, second)
        self.assertEqual(first, reopened)
        # Previewing changes nothing: orders, stock, reservations and history.
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.stock("T")["reserved"], 0)
        self.assertEqual([e["action"] for e in self.app.history("O1")["events"]], ["place"])
        self.assertEqual([e["action"] for e in self.app.stock_history("T")["events"]],
                         ["restock"])
        # A nonexistent root is not created by a failed query.
        fresh = self.root / "fresh"
        with self.assertRaises(ValueError):
            OrderDesk(fresh).reservation_plan(["O1"])
        self.assertFalse(fresh.exists())

    def test_other_orders_reservations_are_not_released_or_transferred(self):
        # O1 is ordered while unmanaged and holds no reservation; KEEPER is
        # placed later against managed stock and reserves 8 of 10.
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.restock("T", 10)
        self.app.place("KEEPER", [{"sku": "T", "quantity": 8}])
        before = self.app.path.read_bytes()
        result = self.app.reservation_plan(["O1"])
        # Only 2 units remain free, so O1 cannot complete; the keeper's 8 units
        # stay reserved and are never offered to O1.
        self.assertFalse(result[0]["can_reserve"])
        self.assertEqual(result[0]["lines"][0],
                         {"sku": "T", "quantity": 3, "reserved": 0, "available": 2, "shortfall": 1})
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.stock("T")["reserved"], 8)

    def test_cli_success_failure_and_independent_array_previews(self):
        self.app.place("A", [{"sku": "T", "quantity": 6}])
        self.app.place("B", [{"sku": "T", "quantity": 6}])
        self.app.restock("T", 10)
        payload = self.root / "plan.json"
        payload.write_text(json.dumps({"order_ids": [" A ", "B"]}), encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                             "reservation-plan", str(payload)],
                            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        value = json.loads(ok.stdout)
        self.assertEqual([item["order_id"] for item in value], ["A", "B"])
        self.assertTrue(value[0]["can_reserve"])
        self.assertEqual(value[0]["lines"][0]["available"], 10)
        self.assertFalse(value[1]["can_reserve"])
        self.assertEqual(value[1]["lines"][0]["available"], 4)
        self.assertEqual(value[1]["lines"][0]["shortfall"], 2)
        # Invalid query: no success output, error JSON on stderr, exit 2, no write.
        before = (self.root / "data.json").read_bytes()
        payload.write_text(json.dumps({"order_ids": ["A", "A"]}), encoding="utf-8")
        failed = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                                 "reservation-plan", str(payload)],
                                text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(failed.stdout, "")
        self.assertIn("error", json.loads(failed.stderr))
        self.assertEqual((self.root / "data.json").read_bytes(), before)
        # Outer array: each element is an independent preview (an array itself).
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([{"order_ids": ["A"]}, {"order_ids": ["B"]}]),
                         encoding="utf-8")
        multi = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                                "reservation-plan", str(batch)],
                               text=True, capture_output=True)
        self.assertEqual(multi.returncode, 0, multi.stderr)
        plans = json.loads(multi.stdout)
        self.assertEqual([[item["order_id"] for item in plan] for plan in plans],
                         [["A"], ["B"]])
        self.assertTrue(plans[0][0]["can_reserve"])
        self.assertTrue(plans[1][0]["can_reserve"])
        # An invalid element fails the command with the standard error JSON.
        batch.write_text(json.dumps([{"order_ids": ["A"]}, {"order_ids": ["nope"]}]),
                         encoding="utf-8")
        stopped = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                                  "reservation-plan", str(batch)],
                                 text=True, capture_output=True)
        self.assertEqual(stopped.returncode, 2, stopped.stdout)
        self.assertEqual(stopped.stdout, "")
        self.assertIn("error", json.loads(stopped.stderr))
        # The preview never wrote anything, even after the successful queries.
        self.assertEqual((self.root / "data.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
