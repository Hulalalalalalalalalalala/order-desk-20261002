import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk

STOCK_T = {"sku": "T", "on_hand": 10, "reserved": 3, "available": 7}

class StockCountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])

    def test_count_recalibrates_available_but_keeps_reservation(self):
        result = self.app.count_stock("c1", [{"sku": "T", "on_hand": 8}])
        self.assertEqual(result, {
            "count_id": "c1",
            "lines": [{
                "sku": "T",
                "before": STOCK_T,
                "after": {"sku": "T", "on_hand": 8, "reserved": 3, "available": 5},
                "delta": -2,
            }],
        })
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 8, "reserved": 3, "available": 5})
        # Reservation records survive, so ship still deducts the originally reserved amount.
        shipped = self.app.ship("O1", "DHL", "Z9")
        self.assertEqual(shipped["status"], "shipped")
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 5, "reserved": 0, "available": 5})

    def test_count_below_reserved_is_rejected(self):
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.count_stock("c1", [{"sku": "T", "on_hand": 2}])
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.stock("T"), STOCK_T)
        # Equal to reserved is allowed.
        result = self.app.count_stock("c1", [{"sku": "T", "on_hand": 3}])
        self.assertEqual(result["lines"][0]["after"]["available"], 0)
        self.assertEqual(result["lines"][0]["delta"], -7)

    def test_zero_is_accepted_but_boolean_and_other_types_are_not(self):
        self.app.cancel("O1")
        result = self.app.count_stock("c0", [{"sku": "T", "on_hand": 0}])
        self.assertEqual(result["lines"][0]["after"]["on_hand"], 0)
        for bad in (True, False, -1, 1.0, "1", None):
            with self.assertRaises(ValueError):
                self.app.count_stock("rx", [{"sku": "T", "on_hand": bad}])

    def test_lines_validated_and_atomic(self):
        before = self.app.path.read_bytes()
        invalid_payloads = [
            [],
            "x",
            None,
            [{"sku": "T"}],
            [{"on_hand": 1}],
            [{"sku": "T", "on_hand": 1}],          # below reserved quantity
            [{"sku": "  ", "on_hand": 1}],
            [{"sku": 7, "on_hand": 1}],
            [{"sku": "X", "on_hand": 1}],          # unknown product
            [{"sku": "C", "on_hand": 1}],          # exists but unmanaged
            [{"sku": "T", "on_hand": 5}, {"sku": "t", "on_hand": 6}],  # case-sensitive: "t" is unknown
            ["nope"],
        ]
        for payload in invalid_payloads:
            with self.assertRaises(ValueError):
                self.app.count_stock("c9", payload)
        # Two valid-shaped lines where the second is below reserved: nothing may change.
        with self.assertRaises(ValueError):
            self.app.count_stock("c9", [
                {"sku": "T", "on_hand": 9},
                {"sku": "C", "on_hand": 1},
            ])
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.stock("T"), STOCK_T)

    def test_duplicate_sku_in_one_count_rejected_without_merging(self):
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.count_stock("c9", [{"sku": "T", "on_hand": 5}, {"sku": "T", "on_hand": 6}])
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_invalid_count_id_rejected(self):
        before = self.app.path.read_bytes()
        for bad in ("", "   ", 5, None, True):
            with self.assertRaises(ValueError):
                self.app.count_stock(bad, [{"sku": "T", "on_hand": 8}])
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_duplicate_count_id_rejected_even_with_same_content(self):
        self.app.count_stock("c1", [{"sku": "T", "on_hand": 8}])
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.count_stock("c1", [{"sku": "T", "on_hand": 8}])
        with self.assertRaises(ValueError):
            self.app.count_stock(" c1 ", [{"sku": "T", "on_hand": 8}])
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_multiple_lines_sorted_with_delta_and_extra_fields_ignored(self):
        self.app.restock("C", 4)
        result = self.app.count_stock("c1", [
            {"sku": "T", "on_hand": 8, "note": "x"},
            {"sku": "C", "on_hand": 2},
        ])
        self.assertEqual(set(result), {"count_id", "lines"})
        self.assertEqual([line["sku"] for line in result["lines"]], ["C", "T"])
        c_line, t_line = result["lines"]
        self.assertEqual(set(c_line), {"sku", "before", "after", "delta"})
        self.assertEqual(c_line["before"], {"sku": "C", "on_hand": 4, "reserved": 0, "available": 4})
        self.assertEqual(c_line["after"], {"sku": "C", "on_hand": 2, "reserved": 0, "available": 2})
        self.assertEqual(c_line["delta"], -2)
        self.assertEqual(t_line["delta"], -2)
        # Unlisted products are left untouched.
        self.assertEqual(self.app.stock("C")["on_hand"], 2)

    def test_snapshot_is_frozen_and_consistent_after_reopen(self):
        result = self.app.count_stock("c1", [{"sku": "T", "on_hand": 8}])
        self.app.restock("T", 4)
        self.app.cancel("O1")
        self.assertEqual(self.app.get_stock_count("c1"), result)
        self.assertEqual(OrderDesk(self.root).get_stock_count("c1"), result)
        # Mutating the returned object must not corrupt the stored snapshot.
        snapshot = self.app.get_stock_count("c1")
        snapshot["lines"][0]["after"]["on_hand"] = 999
        self.assertEqual(self.app.get_stock_count("c1"), result)

    def test_get_stock_count_rejects_invalid_or_unknown_id_without_writing(self):
        self.app.count_stock("c1", [{"sku": "T", "on_hand": 8}])
        for bad in ("", "  ", 9, None):
            with self.assertRaises(ValueError):
                self.app.get_stock_count(bad)
        with self.assertRaises(ValueError):
            self.app.get_stock_count("missing")

    def test_query_never_creates_root(self):
        fresh = Path(self.temp.name) / "empty"
        app = OrderDesk(fresh)
        with self.assertRaises(ValueError):
            app.get_stock_count("x")
        self.assertFalse(fresh.exists())

    def test_legacy_data_without_counts_stays_usable(self):
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("stock_counts", raw)
        self.assertEqual(self.app.stock("T"), STOCK_T)
        with self.assertRaises(ValueError):
            self.app.get_stock_count("c1")
        # Registering the first count on legacy-shaped data works.
        result = self.app.count_stock("c1", [{"sku": "T", "on_hand": 9}])
        self.assertEqual(result["lines"][0]["after"]["on_hand"], 9)

    def test_cancel_after_count_uses_actual_reservation(self):
        self.app.count_stock("c1", [{"sku": "T", "on_hand": 8}])
        self.app.cancel("O1")
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 8, "reserved": 0, "available": 8})

    def test_cli_count_query_and_array_partial_failure(self):
        payload = self.root / "batch.json"
        payload.write_text(json.dumps([
            {"count_id": "c1", "lines": [{"sku": "T", "on_hand": 8}]},
            {"count_id": "c2", "lines": [{"sku": "T", "on_hand": 2}]},
        ]), encoding="utf-8")
        failed = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "count-stock", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2, failed.stdout)
        self.assertIn("reserved", json.loads(failed.stderr)["error"])
        # The first item succeeded; the failed one did not.
        query = self.root / "q.json"
        query.write_text(json.dumps({"count_id": "c1"}), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "stock-count", str(query)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout)["lines"][0]["after"]["on_hand"], 8)
        query.write_text(json.dumps({"count_id": "c2"}), encoding="utf-8")
        missing = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "stock-count", str(query)],
            text=True, capture_output=True)
        self.assertEqual(missing.returncode, 2)

if __name__ == "__main__":
    unittest.main()
