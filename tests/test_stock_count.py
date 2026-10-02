import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk


class StockCountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)
        self.app.add_product("U", "Unmanaged", 0)
        self.app.restock("T", 10)

    def test_count_replaces_on_hand_and_returns_snapshot(self):
        result = self.app.count_stock("c1", [{"sku": "T", "on_hand": 8}])
        self.assertEqual(result, {
            "count_id": "c1",
            "lines": [{
                "sku": "T",
                "before": {"sku": "T", "on_hand": 10, "reserved": 0, "available": 10},
                "after": {"sku": "T", "on_hand": 8, "reserved": 0, "available": 8},
                "delta": -2,
            }],
        })
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 8, "reserved": 0, "available": 8})

    def test_count_with_reservation_available_scenario(self):
        # 在库十件、预留三件：盘点八件后可用量为五件；盘点两件拒绝。
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        result = self.app.count_stock("c1", [{"sku": "T", "on_hand": 8}])
        self.assertEqual(result["lines"][0]["before"], {"sku": "T", "on_hand": 10, "reserved": 3, "available": 7})
        self.assertEqual(result["lines"][0]["after"], {"sku": "T", "on_hand": 8, "reserved": 3, "available": 5})
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 8, "reserved": 3, "available": 5})
        with self.assertRaises(ValueError):
            self.app.count_stock("c2", [{"sku": "T", "on_hand": 2}])
        # Counting exactly the reserved quantity is allowed; once the order
        # is cancelled, counting down to zero is allowed too.
        self.app.count_stock("c3", [{"sku": "T", "on_hand": 3}])
        self.assertEqual(self.app.stock("T")["available"], 0)
        self.app.cancel("O1")
        self.app.count_stock("c4", [{"sku": "T", "on_hand": 0}])
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 0, "reserved": 0, "available": 0})

    def test_lines_sorted_by_sku_and_unlisted_untouched(self):
        self.app.restock("C", 4)
        result = self.app.count_stock("c1", [
            {"sku": "T", "on_hand": 6, "note": "ignored"},
            {"sku": "C", "on_hand": 2},
        ])
        self.assertEqual([line["sku"] for line in result["lines"]], ["C", "T"])
        self.assertEqual(self.app.stock("T")["on_hand"], 6)
        self.assertEqual(self.app.stock("C")["on_hand"], 2)
        # A later count that omits C leaves C unchanged.
        self.app.count_stock("c2", [{"sku": "T", "on_hand": 9}])
        self.assertEqual(self.app.stock("C"), {"sku": "C", "on_hand": 2, "reserved": 0, "available": 2})
        self.assertEqual(self.app.stock("T")["on_hand"], 9)

    def test_cancel_and_ship_use_actual_reservations_after_count(self):
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.count_stock("c1", [{"sku": "T", "on_hand": 8}])
        self.app.cancel("O1")
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 8, "reserved": 0, "available": 8})
        self.app.place("O2", [{"sku": "T", "quantity": 3}])
        self.app.ship("O2", "UPS", "TRK1")
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 5, "reserved": 0, "available": 5})

    def test_duplicate_count_id_rejected_even_with_same_content(self):
        self.app.count_stock("c1", [{"sku": "T", "on_hand": 8}])
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.count_stock("c1", [{"sku": "T", "on_hand": 8}])
        with self.assertRaises(ValueError):
            self.app.count_stock("  c1  ", [{"sku": "T", "on_hand": 8}])
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_validation_failures_leave_data_unchanged(self):
        self.app.restock("C", 1)
        before = self.app.path.read_bytes()
        bad_payloads = [
            ("", [{"sku": "T", "on_hand": 8}]),
            ("   ", [{"sku": "T", "on_hand": 8}]),
            (3, [{"sku": "T", "on_hand": 8}]),
            (True, [{"sku": "T", "on_hand": 8}]),
            ("c", []),
            ("c", "x"),
            ("c", None),
            ("c", [{"sku": "T"}]),
            ("c", [{"on_hand": 8}]),
            ("c", [{"sku": "T", "on_hand": -1}]),
            ("c", [{"sku": "T", "on_hand": True}]),
            ("c", [{"sku": "T", "on_hand": 1.0}]),
            ("c", [{"sku": "T", "on_hand": "8"}]),
            ("c", [{"sku": "  ", "on_hand": 8}]),
            ("c", [{"sku": 3, "on_hand": 8}]),
            ("c", ["x"]),
            ("c", [{"sku": "T", "on_hand": 8}, {"sku": "T", "on_hand": 9}]),
            ("c", [{"sku": "X", "on_hand": 8}]),
            ("c", [{"sku": "U", "on_hand": 8}]),  # U exists but was never restocked: unmanaged
        ]
        for count_id, lines in bad_payloads:
            with self.assertRaises(ValueError):
                self.app.count_stock(count_id, lines)
        self.assertEqual(self.app.path.read_bytes(), before)
        # A low count in a multi-line batch must not change the other line.
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        after_order = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.count_stock("c", [{"sku": "C", "on_hand": 5}, {"sku": "T", "on_hand": 2}])
        self.assertEqual(self.app.path.read_bytes(), after_order)
        self.assertEqual(self.app.stock("T")["on_hand"], 10)
        self.assertEqual(self.app.stock("C")["on_hand"], 1)

    def test_get_returns_snapshot_untouched_by_later_operations(self):
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.count_stock("c1", [{"sku": "T", "on_hand": 8}])
        snapshot = self.app.get_stock_count("c1")
        self.app.restock("T", 20)
        self.app.count_stock("c2", [{"sku": "T", "on_hand": 25}])
        self.app.cancel("O1")
        self.assertEqual(self.app.get_stock_count("c1"), snapshot)
        self.assertEqual(snapshot["lines"][0]["after"]["reserved"], 3)
        # Reopening the root returns the same stored snapshot.
        self.assertEqual(OrderDesk(self.root).get_stock_count("c1"), snapshot)

    def test_get_rejects_unknown_or_invalid_id_without_writing(self):
        self.app.count_stock("c1", [{"sku": "T", "on_hand": 8}])
        empty_root = Path(self.temp.name) / "empty"
        fresh = OrderDesk(empty_root)
        for count_id in ("missing", "  ", 3, True, None):
            with self.assertRaises(ValueError):
                fresh.get_stock_count(count_id)
        self.assertFalse(empty_root.exists())

    def test_legacy_data_without_counts_keeps_working(self):
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        raw.pop("stock_counts", None)
        self.app._write(raw)
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.stock("T")["on_hand"], 10)
        with self.assertRaises(ValueError):
            reopened.get_stock_count("anything")

    def test_cli_count_and_query_and_array_partial_failure(self):
        payload = self.root / "batch.json"
        payload.write_text(json.dumps([
            {"count_id": "c1", "lines": [{"sku": "T", "on_hand": 8}]},
            {"count_id": "c1", "lines": [{"sku": "T", "on_hand": 8}]},
        ]), encoding="utf-8")
        failed = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "count-stock", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2, failed.stdout)
        self.assertIn("already exists", json.loads(failed.stderr)["error"])
        # The first row succeeded; its snapshot is queryable via stock-count.
        query = self.root / "q.json"
        query.write_text(json.dumps({"count_id": "c1"}), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "stock-count", str(query)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual(result["lines"][0]["delta"], -2)
        # Invalid lines inside one array item fail that item as a whole.
        payload.write_text(json.dumps([
            {"count_id": "c2", "lines": [{"sku": "T", "on_hand": 4}]},
            {"count_id": "c3", "lines": [{"sku": "T", "on_hand": "bad"}]},
        ]), encoding="utf-8")
        failed2 = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "count-stock", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(failed2.returncode, 2, failed2.stdout)
        query.write_text(json.dumps({"count_id": "c2"}), encoding="utf-8")
        ok2 = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "stock-count", str(query)],
            text=True, capture_output=True)
        self.assertEqual(json.loads(ok2.stdout)["lines"][0]["after"]["on_hand"], 4)


if __name__ == "__main__":
    unittest.main()
