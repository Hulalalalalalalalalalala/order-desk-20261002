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

    def test_merges_orders_and_duplicate_skus_with_detail(self):
        self.app.restock("T", 10)
        self.app.restock("C", 5)
        self.app.place("O2", [{"sku": "T", "quantity": 2}, {"sku": "T", "quantity": 1}])
        self.app.place("O1", [{"sku": "T", "quantity": 4}, {"sku": "C", "quantity": 5}])
        result = self.app.pick_list([" O2 ", "O1"])
        self.assertEqual(set(result), {"order_ids", "lines"})
        # Normalized ids, returned ascending regardless of input order.
        self.assertEqual(result["order_ids"], ["O1", "O2"])
        self.assertEqual([line["sku"] for line in result["lines"]], ["C", "T"])
        coffee = result["lines"][0]
        self.assertEqual(set(coffee), {"sku", "quantity", "reserved", "available", "shortfall", "orders"})
        self.assertEqual(coffee["quantity"], 5)
        self.assertEqual(coffee["reserved"], 5)
        self.assertEqual(coffee["available"], 0)
        self.assertEqual(coffee["shortfall"], 0)
        self.assertEqual(coffee["orders"], [{"order_id": "O1", "quantity": 5, "reserved": 5}])
        tea = result["lines"][1]
        # Duplicate T lines inside O2 merge to 3; on hand 10, 7 reserved by the
        # selected orders themselves, so available is 3 and nothing is short.
        self.assertEqual(tea["quantity"], 7)
        self.assertEqual(tea["reserved"], 7)
        self.assertEqual(tea["available"], 3)
        self.assertEqual(tea["shortfall"], 0)
        self.assertEqual(tea["orders"], [
            {"order_id": "O1", "quantity": 4, "reserved": 4},
            {"order_id": "O2", "quantity": 3, "reserved": 3},
        ])
        for row in tea["orders"]:
            self.assertEqual(set(row), {"order_id", "quantity", "reserved"})

    def test_reserved_counts_only_selected_orders(self):
        self.app.restock("T", 10)
        self.app.place("OTHER", [{"sku": "T", "quantity": 8}])
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        result = self.app.pick_list(["O1"])
        line = result["lines"][0]
        # The other order's 8 units must not be counted as selected reserved,
        # but they do consume availability (10 - 10 = 0); O1 is fully covered.
        self.assertEqual(line["quantity"], 2)
        self.assertEqual(line["reserved"], 2)
        self.assertEqual(line["available"], 0)
        self.assertEqual(line["shortfall"], 0)
        self.assertEqual(line["orders"], [{"order_id": "O1", "quantity": 2, "reserved": 2}])

    def test_unmanaged_product_kept_with_null_availability(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "U", "quantity": 1000}, {"sku": "T", "quantity": 2}])
        result = self.app.pick_list(["O1"])
        self.assertEqual([line["sku"] for line in result["lines"]], ["T", "U"])
        unmanaged = result["lines"][1]
        self.assertEqual(unmanaged["quantity"], 1000)
        self.assertIsNone(unmanaged["available"])
        self.assertEqual(unmanaged["reserved"], 0)
        self.assertEqual(unmanaged["shortfall"], 0)
        self.assertEqual(unmanaged["orders"], [{"order_id": "O1", "quantity": 1000, "reserved": 0}])

    def test_managed_after_order_placed_uses_current_availability(self):
        # Ordered while U was unmanaged: no reservation exists. Restocking
        # afterwards manages the product but never backfills a reservation.
        self.app.place("O1", [{"sku": "U", "quantity": 10}])
        self.app.restock("U", 4)
        result = self.app.pick_list(["O1"])
        line = result["lines"][0]
        self.assertEqual(line["quantity"], 10)
        self.assertEqual(line["reserved"], 0)
        self.assertEqual(line["available"], 4)
        self.assertEqual(line["shortfall"], 6)
        self.assertEqual(line["orders"], [{"order_id": "O1", "quantity": 10, "reserved": 0}])
        # Enough current stock for the unreserved demand means no shortfall.
        self.app.restock("U", 6)
        result = self.app.pick_list(["O1"])
        self.assertEqual(result["lines"][0]["available"], 10)
        self.assertEqual(result["lines"][0]["shortfall"], 0)

    def test_zero_availability_with_full_own_reservation_has_no_shortfall(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 5}])
        self.assertEqual(self.app.stock("T")["available"], 0)
        result = self.app.pick_list(["O1"])
        line = result["lines"][0]
        self.assertEqual(line["quantity"], 5)
        self.assertEqual(line["reserved"], 5)
        self.assertEqual(line["available"], 0)
        self.assertEqual(line["shortfall"], 0)

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
        self.app.place("O1", [{"sku": "T", "quantity": 5}])
        self.app.amend("O1", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 3}])
        result = self.app.pick_list(["O1"])
        self.assertEqual([line["sku"] for line in result["lines"]], ["C", "T"])
        tea = next(line for line in result["lines"] if line["sku"] == "T")
        self.assertEqual(tea["quantity"], 2)
        self.assertEqual(tea["reserved"], 2)
        # The released 3 units show up as availability again.
        self.assertEqual(tea["available"], 8)
        coffee = result["lines"][0]
        self.assertEqual(coffee["quantity"], 3)
        self.assertEqual(coffee["reserved"], 3)
        self.assertEqual(coffee["available"], 7)

    def test_legacy_data_without_inventory_or_reservations(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}, {"sku": "U", "quantity": 4}])
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        del raw["inventory"]
        raw.pop("reservations", None)
        self.app._write(raw)
        result = OrderDesk(self.root).pick_list(["O1"])
        for line in result["lines"]:
            self.assertIsNone(line["available"])
            self.assertEqual(line["reserved"], 0)
            self.assertEqual(line["shortfall"], 0)
            for row in line["orders"]:
                self.assertEqual(row["reserved"], 0)
        by_sku = {line["sku"]: line for line in result["lines"]}
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
                    self.app.pick_list(payload)
        # Case sensitive: different casing is not a duplicate, but the unknown
        # one must still raise rather than return a partial summary.
        with self.assertRaises(ValueError):
            self.app.pick_list(["O1", "o1"])

    def test_query_is_read_only_and_repeatable(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        before = self.app.path.read_bytes()
        first = self.app.pick_list(["O1"])
        second = self.app.pick_list(["O1"])
        reopened = OrderDesk(self.root).pick_list(["O1"])
        self.assertEqual(first, second)
        self.assertEqual(first, reopened)
        self.assertEqual(self.app.path.read_bytes(), before)
        # A nonexistent root is not created by a failed query.
        fresh = self.root / "fresh"
        with self.assertRaises(ValueError):
            OrderDesk(fresh).pick_list(["O1"])
        self.assertFalse(fresh.exists())

    def test_cli_pick_list_success_failure_and_array(self):
        self.app.restock("T", 10)
        self.app.place("A", [{"sku": "T", "quantity": 3}])
        self.app.place("B", [{"sku": "T", "quantity": 2}])
        payload = self.root / "pick.json"
        payload.write_text(json.dumps({"order_ids": ["B", "A"]}), encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "pick-list", str(payload)],
                            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        value = json.loads(ok.stdout)
        self.assertEqual(value["order_ids"], ["A", "B"])
        self.assertEqual(value["lines"][0]["quantity"], 5)
        # Invalid query: no success output, error JSON on stderr, exit 2.
        before = (self.root / "data.json").read_bytes()
        payload.write_text(json.dumps({"order_ids": ["A", "A"]}), encoding="utf-8")
        failed = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "pick-list", str(payload)],
                                text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(failed.stdout, "")
        self.assertIn("error", json.loads(failed.stderr))
        self.assertEqual((self.root / "data.json").read_bytes(), before)
        # Array input is queried in order and stops at the first error.
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([
            {"order_ids": ["A"]},
            {"order_ids": ["nope"]},
        ]), encoding="utf-8")
        stopped = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "pick-list", str(batch)],
                                 text=True, capture_output=True)
        self.assertEqual(stopped.returncode, 2, stopped.stdout)
        self.assertEqual(stopped.stdout, "")
        self.assertIn("error", json.loads(stopped.stderr))
        # An array of valid queries prints one summary each.
        batch.write_text(json.dumps([{"order_ids": ["A"]}, {"order_ids": ["B"]}]), encoding="utf-8")
        multi = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "pick-list", str(batch)],
                               text=True, capture_output=True)
        self.assertEqual(multi.returncode, 0, multi.stderr)
        summaries = json.loads(multi.stdout)
        self.assertEqual([summary["order_ids"] for summary in summaries], [["A"], ["B"]])


if __name__ == "__main__":
    unittest.main()
