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

    def test_priority_order_consumes_shared_pool(self):
        # All three orders were placed while T was unmanaged, so none holds a
        # reservation; the restock afterwards manages T with 7 available.
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.place("O2", [{"sku": "T", "quantity": 5}])
        self.app.place("O3", [{"sku": "T", "quantity": 2}])
        self.app.restock("T", 7)
        plan = self.app.reservation_plan(["O1", "O2", "O3"])
        self.assertEqual([item["order_id"] for item in plan], ["O1", "O2", "O3"])
        first, second, third = plan
        for item in plan:
            self.assertEqual(set(item), {"order_id", "can_reserve", "lines"})
        # O1 fits in the pool of 7 and consumes 3, leaving 4.
        self.assertTrue(first["can_reserve"])
        self.assertEqual(first["lines"], [
            {"sku": "T", "quantity": 3, "reserved": 0, "available": 7, "shortfall": 0},
        ])
        # O2 needs 5 but only 4 remain: rejected and consumes nothing.
        self.assertFalse(second["can_reserve"])
        self.assertEqual(second["lines"], [
            {"sku": "T", "quantity": 5, "reserved": 0, "available": 4, "shortfall": 1},
        ])
        # O3 still sees the 4 left by O1 and fits.
        self.assertTrue(third["can_reserve"])
        self.assertEqual(third["lines"], [
            {"sku": "T", "quantity": 2, "reserved": 0, "available": 4, "shortfall": 0},
        ])
        # Reversing the priority reverses the outcome: O2 now fits (5 of 7),
        # leaving 2, which is not enough for O1.
        reversed_plan = self.app.reservation_plan(["O2", "O1"])
        self.assertTrue(reversed_plan[0]["can_reserve"])
        self.assertEqual(reversed_plan[0]["lines"][0]["available"], 7)
        self.assertFalse(reversed_plan[1]["can_reserve"])
        self.assertEqual(reversed_plan[1]["lines"][0]["available"], 2)
        self.assertEqual(reversed_plan[1]["lines"][0]["shortfall"], 1)

    def test_merges_duplicate_skus_and_sorts_lines(self):
        self.app.restock("T", 10)
        self.app.restock("C", 5)
        self.app.place("O1", [
            {"sku": "T", "quantity": 2},
            {"sku": "C", "quantity": 5},
            {"sku": "T", "quantity": 1},
        ])
        plan = self.app.reservation_plan(["O1"])
        item = plan[0]
        self.assertTrue(item["can_reserve"])
        self.assertEqual([line["sku"] for line in item["lines"]], ["C", "T"])
        coffee, tea = item["lines"]
        self.assertEqual(set(coffee), {"sku", "quantity", "reserved", "available", "shortfall"})
        # Fully reserved at place time: new demand is zero, the zero-shortfall
        # lines are still kept, and nothing is deducted from the pool.
        self.assertEqual(coffee, {"sku": "C", "quantity": 5, "reserved": 5, "available": 0, "shortfall": 0})
        self.assertEqual(tea, {"sku": "T", "quantity": 3, "reserved": 3, "available": 7, "shortfall": 0})

    def test_fully_reserved_order_consumes_nothing(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 10}])
        self.app.place("O2", [{"sku": "U", "quantity": 4}])
        plan = self.app.reservation_plan(["O1", "O2"])
        # O1 is fully reserved, so its zero new demand leaves the pool at 0.
        self.assertTrue(plan[0]["can_reserve"])
        self.assertEqual(plan[0]["lines"][0]["available"], 0)
        self.assertTrue(plan[1]["can_reserve"])

    def test_partial_own_reservation_counts_only_new_demand(self):
        # Legacy state: O1 demands 6 but holds a partial reservation of 4, so
        # the plan only asks for the remaining 2 against the 1 still available.
        self.app.place("O1", [{"sku": "T", "quantity": 6}])
        self.app.restock("T", 5)
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        raw["inventory"]["T"]["reserved"] = 4
        raw["reservations"] = {"O1": {"T": 4}}
        self.app._write(raw)
        plan = OrderDesk(self.root).reservation_plan(["O1"])
        line = plan[0]["lines"][0]
        self.assertEqual(line["quantity"], 6)
        self.assertEqual(line["reserved"], 4)
        self.assertEqual(line["available"], 1)
        self.assertEqual(line["shortfall"], 1)
        self.assertFalse(plan[0]["can_reserve"])

    def test_unmanaged_product_is_unlimited_and_uses_no_pool(self):
        self.app.restock("T", 2)
        self.app.place("O1", [{"sku": "U", "quantity": 1000}, {"sku": "T", "quantity": 2}])
        plan = self.app.reservation_plan(["O1"])
        item = plan[0]
        self.assertTrue(item["can_reserve"])
        self.assertEqual([line["sku"] for line in item["lines"]], ["T", "U"])
        unmanaged = item["lines"][1]
        self.assertEqual(unmanaged["quantity"], 1000)
        self.assertIsNone(unmanaged["available"])
        self.assertEqual(unmanaged["reserved"], 0)
        self.assertEqual(unmanaged["shortfall"], 0)

    def test_existing_reservations_are_never_reassigned(self):
        # O2 ordered T while unmanaged; O1 later reserved all 10 on hand.
        # O2's plan sees zero availability and O1's reservation is untouched.
        self.app.place("O2", [{"sku": "T", "quantity": 5}])
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 10}])
        plan = self.app.reservation_plan(["O1", "O2"])
        self.assertTrue(plan[0]["can_reserve"])
        self.assertEqual(plan[0]["lines"][0]["reserved"], 10)
        self.assertFalse(plan[1]["can_reserve"])
        self.assertEqual(plan[1]["lines"][0]["available"], 0)
        self.assertEqual(plan[1]["lines"][0]["shortfall"], 5)
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 10, "reserved": 10, "available": 0})

    def test_paused_product_still_previewed(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.set_product_enabled("T", False)
        plan = self.app.reservation_plan(["O1"])
        self.assertTrue(plan[0]["can_reserve"])
        self.assertEqual(plan[0]["lines"][0]["quantity"], 2)

    def test_legacy_data_without_inventory_or_reservations(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}, {"sku": "U", "quantity": 4}])
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        del raw["inventory"]
        raw.pop("reservations", None)
        self.app._write(raw)
        plan = OrderDesk(self.root).reservation_plan(["O1"])
        item = plan[0]
        self.assertTrue(item["can_reserve"])
        for line in item["lines"]:
            self.assertIsNone(line["available"])
            self.assertEqual(line["reserved"], 0)
            self.assertEqual(line["shortfall"], 0)
        by_sku = {line["sku"]: line for line in item["lines"]}
        self.assertEqual(by_sku["T"]["quantity"], 2)
        self.assertEqual(by_sku["U"]["quantity"], 4)

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
        # Case sensitive: different casing is not a duplicate, but the unknown
        # one must still raise rather than return a partial plan.
        with self.assertRaises(ValueError):
            self.app.reservation_plan(["O1", "o1"])

    def test_order_with_unknown_product_rejects_whole_query(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.app.place("O2", [{"sku": "U", "quantity": 1}])
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        del raw["products"]["U"]
        self.app._write(raw)
        app = OrderDesk(self.root)
        with self.assertRaises(ValueError):
            app.reservation_plan(["O2"])
        # One bad order rejects the whole plan, even behind a valid one.
        with self.assertRaises(ValueError):
            app.reservation_plan(["O1", "O2"])

    def test_query_is_read_only_and_repeatable(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        before = self.app.path.read_bytes()
        first = self.app.reservation_plan(["O1"])
        second = self.app.reservation_plan(["O1"])
        reopened = OrderDesk(self.root).reservation_plan(["O1"])
        self.assertEqual(first, second)
        self.assertEqual(first, reopened)
        self.assertEqual(self.app.path.read_bytes(), before)
        # A nonexistent root is not created by a failed query.
        fresh = self.root / "fresh"
        with self.assertRaises(ValueError):
            OrderDesk(fresh).reservation_plan(["O1"])
        self.assertFalse(fresh.exists())

    def test_cli_reservation_plan_success_failure_and_array(self):
        self.app.place("A", [{"sku": "T", "quantity": 3}])
        self.app.place("B", [{"sku": "T", "quantity": 2}])
        self.app.restock("T", 4)
        payload = self.root / "plan.json"
        payload.write_text(json.dumps({"order_ids": ["B", "A"]}), encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "reservation-plan", str(payload)],
                            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        value = json.loads(ok.stdout)
        # Input order is preserved: B (2 of 4) fits, A (3 of the remaining 2) does not.
        self.assertEqual([item["order_id"] for item in value], ["B", "A"])
        self.assertTrue(value[0]["can_reserve"])
        self.assertFalse(value[1]["can_reserve"])
        self.assertEqual(value[1]["lines"][0]["available"], 2)
        self.assertEqual(value[1]["lines"][0]["shortfall"], 1)
        # Invalid query: no success output, error JSON on stderr, exit 2.
        before = (self.root / "data.json").read_bytes()
        payload.write_text(json.dumps({"order_ids": ["A", "A"]}), encoding="utf-8")
        failed = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "reservation-plan", str(payload)],
                                text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(failed.stdout, "")
        self.assertIn("error", json.loads(failed.stderr))
        self.assertEqual((self.root / "data.json").read_bytes(), before)
        # Array input is previewed in order and stops at the first error.
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([
            {"order_ids": ["A"]},
            {"order_ids": ["nope"]},
        ]), encoding="utf-8")
        stopped = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "reservation-plan", str(batch)],
                                 text=True, capture_output=True)
        self.assertEqual(stopped.returncode, 2, stopped.stdout)
        self.assertEqual(stopped.stdout, "")
        self.assertIn("error", json.loads(stopped.stderr))
        # An array of valid queries prints one plan each, each independent.
        batch.write_text(json.dumps([{"order_ids": ["A"]}, {"order_ids": ["B"]}]), encoding="utf-8")
        multi = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "reservation-plan", str(batch)],
                               text=True, capture_output=True)
        self.assertEqual(multi.returncode, 0, multi.stderr)
        plans = json.loads(multi.stdout)
        self.assertEqual([[item["order_id"] for item in plan] for plan in plans], [["A"], ["B"]])
        self.assertEqual((self.root / "data.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
