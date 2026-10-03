import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk


class StockCountReversalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)
        self.app.add_product("U", "Unmanaged", 0)
        self.app.restock("T", 10)

    def test_reverse_offsets_delta_and_keeps_later_business(self):
        # 十件盘为八件后补货五件：撤销把十三件变成十五件。
        self.app.count_stock("c1", [{"sku": "T", "on_hand": 8}])
        self.app.restock("T", 5)
        result = self.app.reverse_stock_count("c1")
        self.assertEqual(result, {
            "count_id": "c1",
            "lines": [{
                "sku": "T",
                "delta": 2,
                "before": {"sku": "T", "on_hand": 13, "reserved": 0, "available": 13},
                "after": {"sku": "T", "on_hand": 15, "reserved": 0, "available": 15},
            }],
        })
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 15, "reserved": 0, "available": 15})

    def test_reverse_covers_all_lines_sorted_and_keeps_reservations(self):
        self.app.restock("C", 4)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.count_stock("c1", [{"sku": "T", "on_hand": 8}, {"sku": "C", "on_hand": 6}])
        result = self.app.reverse_stock_count("c1")
        self.assertEqual([line["sku"] for line in result["lines"]], ["C", "T"])
        self.assertEqual(result["lines"][0], {
            "sku": "C",
            "delta": -2,
            "before": {"sku": "C", "on_hand": 6, "reserved": 0, "available": 6},
            "after": {"sku": "C", "on_hand": 4, "reserved": 0, "available": 4},
        })
        self.assertEqual(result["lines"][1], {
            "sku": "T",
            "delta": 2,
            "before": {"sku": "T", "on_hand": 8, "reserved": 3, "available": 5},
            "after": {"sku": "T", "on_hand": 10, "reserved": 3, "available": 7},
        })
        # The order's actual reservation is untouched.
        self.assertEqual(self.app.get("O1")["status"], "placed")
        self.app.cancel("O1")
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 10, "reserved": 0, "available": 10})

    def test_reverse_only_once_and_count_id_stays_occupied(self):
        self.app.count_stock("c1", [{"sku": "T", "on_hand": 8}])
        self.app.reverse_stock_count("c1")
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.reverse_stock_count("c1")
        with self.assertRaises(ValueError):
            self.app.reverse_stock_count("  c1  ")
        # The original count id is still occupied for new counts.
        with self.assertRaises(ValueError):
            self.app.count_stock("c1", [{"sku": "T", "on_hand": 9}])
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_reverse_rejects_unknown_or_invalid_id_without_writing(self):
        empty_root = Path(self.temp.name) / "empty"
        fresh = OrderDesk(empty_root)
        for count_id in ("missing", "", "   ", 3, True, None):
            with self.assertRaises(ValueError):
                fresh.reverse_stock_count(count_id)
            with self.assertRaises(ValueError):
                fresh.get_stock_count_reversal(count_id)
        self.assertFalse(empty_root.exists())
        self.app.count_stock("c1", [{"sku": "T", "on_hand": 8}])
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.reverse_stock_count("other")
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_query_before_reversal_rejected_and_readonly(self):
        self.app.count_stock("c1", [{"sku": "T", "on_hand": 8}])
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.get_stock_count_reversal("c1")
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_credential_persists_and_survives_later_business(self):
        self.app.count_stock("c1", [{"sku": "T", "on_hand": 8}])
        result = self.app.reverse_stock_count("c1")
        self.assertEqual(self.app.get_stock_count_reversal("c1"), result)
        snapshot = self.app.get_stock_count("c1")
        self.app.restock("T", 20)
        self.app.count_stock("c2", [{"sku": "T", "on_hand": 30}])
        # Later business never rewrites the credential or the count snapshot.
        self.assertEqual(self.app.get_stock_count_reversal("c1"), result)
        self.assertEqual(self.app.get_stock_count("c1"), snapshot)
        # Reopening the root returns the same stored credential.
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.get_stock_count_reversal("c1"), result)
        self.assertEqual(reopened.get_stock_count("c1"), snapshot)

    def test_disabled_product_can_still_be_reversed(self):
        self.app.count_stock("c1", [{"sku": "T", "on_hand": 8}])
        self.app.set_product_enabled("T", False)
        result = self.app.reverse_stock_count("c1")
        self.assertEqual(result["lines"][0]["after"]["on_hand"], 10)
        self.assertEqual(self.app.stock("T")["on_hand"], 10)

    def test_failure_is_atomic_and_leaves_other_lines_untouched(self):
        self.app.restock("C", 4)
        self.app.count_stock("c1", [{"sku": "C", "on_hand": 6}, {"sku": "T", "on_hand": 8}])
        # C drops out of the catalog after the count: reversing c1 must fail
        # as a whole and leave the T line untouched.
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        raw["products"].pop("C")
        self.app._write(raw)
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.reverse_stock_count("c1")
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.stock("T")["on_hand"], 8)

    def test_negative_or_below_reserved_result_rejected(self):
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.count_stock("c1", [{"sku": "T", "on_hand": 12}])  # delta +2
        # Count down to exactly the reserved quantity; reversing c1 would give
        # 3 - 2 = 1, below the current reserved quantity.
        self.app.count_stock("c2", [{"sku": "T", "on_hand": 3}])
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.reverse_stock_count("c1")
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 3, "reserved": 3, "available": 0})
        # A negative result is rejected too: free the reservation, count up
        # (delta +2) then down to 1, so reversing would give 1 - 2 < 0.
        self.app.cancel("O1")
        self.app.count_stock("c3", [{"sku": "T", "on_hand": 5}])
        self.app.count_stock("c4", [{"sku": "T", "on_hand": 1}])
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.reverse_stock_count("c3")
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.stock("T")["on_hand"], 1)

    def test_unmanaged_product_rejected(self):
        self.app.add_product("P", "Paused", 50)
        self.app.restock("P", 2)
        self.app.count_stock("c1", [{"sku": "P", "on_hand": 5}, {"sku": "T", "on_hand": 8}])
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        raw["inventory"].pop("P")
        self.app._write(raw)
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.reverse_stock_count("c1")
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.stock("T")["on_hand"], 8)

    def test_zero_delta_lines_kept_and_all_zero_registers_without_events(self):
        self.app.restock("C", 4)
        self.app.count_stock("c1", [{"sku": "T", "on_hand": 10}, {"sku": "C", "on_hand": 2}])
        events_t = len(self.app.stock_history("T")["events"])
        result = self.app.reverse_stock_count("c1")
        self.assertEqual([line["sku"] for line in result["lines"]], ["C", "T"])
        self.assertEqual(result["lines"][0]["delta"], 2)
        self.assertEqual(result["lines"][1]["delta"], 0)
        self.assertEqual(result["lines"][1]["before"], result["lines"][1]["after"])
        # Only the actually changed product gains a stock event.
        self.assertEqual(len(self.app.stock_history("T")["events"]), events_t)
        c_events = self.app.stock_history("C")["events"]
        self.assertEqual(c_events[-1]["action"], "reverse-stock-count")
        self.assertEqual(c_events[-1]["reference_id"], "c1")
        # An all-zero reversal still registers its credential and no events.
        self.app.count_stock("c2", [{"sku": "T", "on_hand": 10}])
        events_t = len(self.app.stock_history("T")["events"])
        result2 = self.app.reverse_stock_count("c2")
        self.assertEqual(result2["lines"][0]["delta"], 0)
        self.assertEqual(self.app.get_stock_count_reversal("c2"), result2)
        self.assertEqual(len(self.app.stock_history("T")["events"]), events_t)

    def test_stock_history_event_chains_and_legacy_complete_false(self):
        self.app.count_stock("c1", [{"sku": "T", "on_hand": 8}])
        self.app.restock("T", 5)
        self.app.reverse_stock_count("c1")
        events = self.app.stock_history("T")["events"]
        self.assertEqual([e["sequence"] for e in events], [1, 2, 3, 4])
        self.assertEqual([e["action"] for e in events],
                         ["restock", "count-stock", "restock", "reverse-stock-count"])
        last = events[-1]
        self.assertEqual(last["reference_id"], "c1")
        self.assertEqual(last["before"], {"sku": "T", "on_hand": 13, "reserved": 0, "available": 13})
        self.assertEqual(last["after"], {"sku": "T", "on_hand": 15, "reserved": 0, "available": 15})
        self.assertTrue(self.app.stock_history("T")["complete"])
        # A legacy product managed before stock history existed starts its
        # reversal event at sequence 1 with complete=False.
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        raw["inventory"]["C"] = {"on_hand": 4, "reserved": 0}
        raw["stock_counts"]["c9"] = {
            "count_id": "c9",
            "lines": [{"sku": "C", "delta": -1,
                       "before": {"sku": "C", "on_hand": 5, "reserved": 0, "available": 5},
                       "after": {"sku": "C", "on_hand": 4, "reserved": 0, "available": 4}}],
        }
        self.app._write(raw)
        self.app.reverse_stock_count("c9")
        history = self.app.stock_history("C")
        self.assertFalse(history["complete"])
        self.assertEqual(len(history["events"]), 1)
        self.assertEqual(history["events"][0]["sequence"], 1)
        self.assertEqual(history["events"][0]["action"], "reverse-stock-count")

    def test_legacy_data_without_counts_or_reversals_reads_as_empty(self):
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        raw.pop("stock_counts", None)
        raw.pop("stock_count_reversals", None)
        self.app._write(raw)
        reopened = OrderDesk(self.root)
        with self.assertRaises(ValueError):
            reopened.reverse_stock_count("anything")
        with self.assertRaises(ValueError):
            reopened.get_stock_count_reversal("anything")
        # No records are fabricated for the legacy data.
        self.assertNotIn("stock_count_reversals", json.loads(self.app.path.read_text(encoding="utf-8")))

    def test_cli_reverse_and_query_with_array_partial_failure(self):
        self.app.count_stock("c1", [{"sku": "T", "on_hand": 8}])
        payload = self.root / "batch.json"
        payload.write_text(json.dumps([
            {"count_id": "c1"},
            {"count_id": "c1"},
        ]), encoding="utf-8")
        failed = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "reverse-stock-count", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2, failed.stdout)
        self.assertIn("already reversed", json.loads(failed.stderr)["error"])
        # The first row succeeded; its credential is queryable.
        query = self.root / "q.json"
        query.write_text(json.dumps({"count_id": "c1"}), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "stock-count-reversal", str(query)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual(result["count_id"], "c1")
        self.assertEqual(result["lines"][0]["delta"], 2)
        self.assertEqual(result["lines"][0]["after"]["on_hand"], 10)
        # Querying a count that was never reversed fails with exit code 2.
        self.app.count_stock("c2", [{"sku": "T", "on_hand": 12}])
        query.write_text(json.dumps({"count_id": "c2"}), encoding="utf-8")
        failed2 = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "stock-count-reversal", str(query)],
            text=True, capture_output=True)
        self.assertEqual(failed2.returncode, 2, failed2.stdout)


if __name__ == "__main__":
    unittest.main()
