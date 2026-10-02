import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk

class PickListTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)
        self.app.add_product("U", "Unrestocked", 50)

    def test_merges_demand_across_orders_with_per_order_detail(self):
        self.app.restock("T", 10)
        self.app.place("O2", [{"sku": "T", "quantity": 2}, {"sku": "T", "quantity": 1}])
        self.app.place("O1", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])
        result = self.app.pick_list(["O2", " O1 "])
        self.assertEqual(set(result), {"order_ids", "lines"})
        self.assertEqual(result["order_ids"], ["O1", "O2"])
        lines = {line["sku"]: line for line in result["lines"]}
        self.assertEqual(list(lines), ["C", "T"])
        tea = lines["T"]
        self.assertEqual(set(tea), {"sku", "quantity", "reserved", "available", "shortfall", "orders"})
        self.assertEqual(tea["quantity"], 5)
        self.assertEqual(tea["reserved"], 5)
        self.assertEqual(tea["available"], 5)
        self.assertEqual(tea["shortfall"], 0)
        self.assertEqual(tea["orders"], [
            {"order_id": "O1", "quantity": 2, "reserved": 2},
            {"order_id": "O2", "quantity": 3, "reserved": 3},
        ])
        coffee = lines["C"]
        self.assertEqual(coffee["quantity"], 1)
        self.assertEqual(coffee["reserved"], 0)
        self.assertEqual(coffee["available"], None)
        self.assertEqual(coffee["shortfall"], 0)
        self.assertEqual(coffee["orders"], [
            {"order_id": "O1", "quantity": 1, "reserved": 0},
        ])

    def test_unmanaged_products_keep_lines_with_null_available(self):
        self.app.place("O1", [{"sku": "U", "quantity": 1000}])
        result = self.app.pick_list(["O1"])
        self.assertEqual(result["order_ids"], ["O1"])
        self.assertEqual(len(result["lines"]), 1)
        line = result["lines"][0]
        self.assertEqual(line, {
            "sku": "U",
            "quantity": 1000,
            "reserved": 0,
            "available": None,
            "shortfall": 0,
            "orders": [{"order_id": "O1", "quantity": 1000, "reserved": 0}],
        })

    def test_shortfall_counts_only_selected_orders_reservations(self):
        # 10 on hand; O1 (not selected) holds 6, the selected O2 holds 2.
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 6}])
        self.app.place("O2", [{"sku": "T", "quantity": 2}])
        result = self.app.pick_list(["O2"])
        line = result["lines"][0]
        self.assertEqual(line["quantity"], 2)
        self.assertEqual(line["reserved"], 2)
        self.assertEqual(line["available"], 2)
        self.assertEqual(line["shortfall"], 0)
        self.assertEqual(line["orders"], [{"order_id": "O2", "quantity": 2, "reserved": 2}])

    def test_stock_count_to_zero_available_kept_fully_reserved(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.place("O2", [{"sku": "T", "quantity": 2}])
        # Count on_hand down to exactly the reserved total: available becomes
        # zero but both selected orders are fully reserved, so no shortfall.
        self.app.count_stock("S1", [{"sku": "T", "on_hand": 5}])
        self.assertEqual(self.app.stock("T")["available"], 0)
        result = self.app.pick_list(["O1", "O2"])
        line = result["lines"][0]
        self.assertEqual((line["quantity"], line["reserved"], line["available"], line["shortfall"]),
                         (5, 5, 0, 0))
        self.assertEqual(line["orders"], [
            {"order_id": "O1", "quantity": 3, "reserved": 3},
            {"order_id": "O2", "quantity": 2, "reserved": 2},
        ])

    def test_paused_product_still_included(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.set_product_enabled("T", False)
        result = self.app.pick_list(["O1"])
        self.assertEqual(result["lines"][0]["sku"], "T")
        self.assertEqual(result["lines"][0]["quantity"], 2)

    def test_amended_quantities_are_reflected(self):
        self.app.restock("T", 10)
        self.app.restock("C", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 4}])
        self.app.amend("O1", [{"sku": "C", "quantity": 2}, {"sku": "C", "quantity": 1}])
        result = self.app.pick_list(["O1"])
        self.assertEqual([line["sku"] for line in result["lines"]], ["C"])
        line = result["lines"][0]
        self.assertEqual(line["quantity"], 3)
        self.assertEqual(line["reserved"], 3)
        self.assertEqual(line["orders"], [{"order_id": "O1", "quantity": 3, "reserved": 3}])
        # T's reservation was released on amend.
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 10, "reserved": 0, "available": 10})

    def test_managed_after_order_placed_uses_current_available(self):
        self.app.place("O1", [{"sku": "C", "quantity": 4}])
        self.app.restock("C", 9)
        result = self.app.pick_list(["O1"])
        line = result["lines"][0]
        self.assertEqual((line["quantity"], line["reserved"], line["available"], line["shortfall"]),
                         (4, 0, 9, 0))
        self.assertEqual(line["orders"], [{"order_id": "O1", "quantity": 4, "reserved": 0}])
        # Only 2 currently available: gap of 2, no fabricated reservation.
        self.app.place("O2", [{"sku": "C", "quantity": 7}])
        result = self.app.pick_list(["O1"])
        line = result["lines"][0]
        self.assertEqual((line["quantity"], line["reserved"], line["available"], line["shortfall"]),
                         (4, 0, 2, 2))

    def test_legacy_order_without_reservation_record_reads_reserved_zero(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        raw["reservations"].pop("O1")
        self.app.path.write_text(json.dumps(raw), encoding="utf-8")
        result = OrderDesk(self.root).pick_list(["O1"])
        line = result["lines"][0]
        # The per-order record is gone so the order's actual reservation is
        # zero, while available stays consistent with stock's own counter.
        self.assertEqual(line["reserved"], 0)
        self.assertEqual(line["orders"][0]["reserved"], 0)
        self.assertEqual(line["available"], 3)
        self.assertEqual(line["shortfall"], 0)

    def test_invalid_arguments_raise_value_error(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        self.app.cancel("O2")
        for bad in (None, [], "O1", 3, ["O1", 3], ["  "], [""], ["O1", " O1 "],
                    ["O1", "O1"], ["missing"], ["O2"], ["O1", "O2"]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.app.pick_list(bad)

    def test_case_sensitive_ids_are_distinct(self):
        self.app.restock("T", 5)
        self.app.place("a", [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.pick_list(["A"])
        result = self.app.pick_list(["a"])
        self.assertEqual(result["order_ids"], ["a"])
        with self.assertRaises(ValueError):
            self.app.pick_list(["a", " a "])

    def test_query_is_read_only_and_deterministic(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        before = self.app.path.read_bytes()
        first = self.app.pick_list(["O1"])
        self.assertEqual(self.app.path.read_bytes(), before)
        # Reopening the same root and querying again yields the same result.
        reopened = OrderDesk(self.root).pick_list(["O1"])
        self.assertEqual(reopened, first)
        # No directory or file is created when the root has no data.
        empty = Path(self.temp.name) / "empty"
        with self.assertRaises(ValueError):
            OrderDesk(empty).pick_list(["O1"])
        self.assertFalse(empty.exists())

    def test_cli_pick_list_success_and_failure_and_array_stops_at_first_error(self):
        self.app.restock("T", 10)
        self.app.place("A", [{"sku": "T", "quantity": 3}])
        self.app.place("B", [{"sku": "T", "quantity": 2}])
        payload = self.root / "pick.json"
        payload.write_text(json.dumps({"order_ids": ["B", "A"]}), encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "pick-list", str(payload)],
                            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual(result["order_ids"], ["A", "B"])
        self.assertEqual(result["lines"][0]["quantity"], 5)
        # Failure: unknown order -> exit 2, error JSON on stderr, no stdout.
        bad = self.root / "bad.json"
        bad.write_text(json.dumps({"order_ids": ["missing"]}), encoding="utf-8")
        failed = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "pick-list", str(bad)],
                                text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(failed.stdout, "")
        self.assertIn("error", json.loads(failed.stderr))
        # Array input: queries run in order and stop at the first error; the
        # first successful result is not a mutation so data is unchanged.
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([
            {"order_ids": ["A"]},
            {"order_ids": ["nope"]},
        ]), encoding="utf-8")
        stopped = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "pick-list", str(batch)],
                                 text=True, capture_output=True)
        self.assertEqual(stopped.returncode, 2, stopped.stdout)
        self.assertIn("error", json.loads(stopped.stderr))
        self.assertEqual(OrderDesk(self.root).stock("T"),
                         {"sku": "T", "on_hand": 10, "reserved": 5, "available": 5})

if __name__ == "__main__":
    unittest.main()
