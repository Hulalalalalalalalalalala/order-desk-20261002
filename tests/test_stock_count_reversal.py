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
        self.app.restock("C", 4)

    def test_reversal_offsets_delta_and_keeps_later_business_changes(self):
        # 从十件盘为八件后补货五件，撤销应把十三件变成十五件。
        self.app.count_stock("c1", [{"sku": "T", "on_hand": 8}])
        self.app.restock("T", 5)
        self.assertEqual(self.app.stock("T")["on_hand"], 13)
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

    def test_reversal_covers_all_lines_sorted_and_keeps_reservations(self):
        self.app.place("O1", [{"sku": "C", "quantity": 2}])
        self.app.count_stock("c1", [
            {"sku": "T", "on_hand": 6},
            {"sku": "C", "on_hand": 9},
        ])
        self.app.restock("T", 1)
        result = self.app.reverse_stock_count("c1")
        self.assertEqual([line["sku"] for line in result["lines"]], ["C", "T"])
        coffee, tea = result["lines"]
        self.assertEqual(set(coffee), {"sku", "delta", "before", "after"})
        self.assertEqual(coffee["delta"], -5)
        self.assertEqual(coffee["before"], {"sku": "C", "on_hand": 9, "reserved": 2, "available": 7})
        self.assertEqual(coffee["after"], {"sku": "C", "on_hand": 4, "reserved": 2, "available": 2})
        self.assertEqual(tea["delta"], 4)
        self.assertEqual(tea["after"], {"sku": "T", "on_hand": 11, "reserved": 0, "available": 11})
        # 各订单实际预留不变。
        self.assertEqual(self.app.stock("C"), {"sku": "C", "on_hand": 4, "reserved": 2, "available": 2})
        self.app.cancel("O1")
        self.assertEqual(self.app.stock("C"), {"sku": "C", "on_hand": 4, "reserved": 0, "available": 4})

    def test_disabled_product_can_still_be_reversed(self):
        self.app.count_stock("c1", [{"sku": "T", "on_hand": 8}])
        self.app.set_product_enabled("T", False)
        result = self.app.reverse_stock_count("c1")
        self.assertEqual(result["lines"][0]["after"]["on_hand"], 10)
        self.assertEqual(self.app.stock("T")["on_hand"], 10)

    def test_zero_delta_count_registers_reversal_without_stock_events(self):
        self.app.count_stock("c1", [{"sku": "T", "on_hand": 10}])
        events_before = self.app.stock_history("T")["events"]
        result = self.app.reverse_stock_count("c1")
        self.assertEqual(result["lines"][0]["delta"], 0)
        self.assertEqual(result["lines"][0]["before"], result["lines"][0]["after"])
        self.assertEqual(self.app.stock_history("T")["events"], events_before)
        self.assertEqual(self.app.get_stock_count_reversal("c1"), result)

    def test_duplicate_reversal_rejected_and_count_id_stays_occupied(self):
        self.app.count_stock("c1", [{"sku": "T", "on_hand": 8}])
        self.app.reverse_stock_count("c1")
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.reverse_stock_count("c1")
        with self.assertRaises(ValueError):
            self.app.reverse_stock_count("  c1  ")
        # 原盘点编号继续占用。
        with self.assertRaises(ValueError):
            self.app.count_stock("c1", [{"sku": "T", "on_hand": 5}])
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_unknown_or_invalid_id_rejected_without_writing(self):
        self.app.count_stock("c1", [{"sku": "T", "on_hand": 8}])
        before = self.app.path.read_bytes()
        for count_id in ("missing", "C1", "", "   ", 3, True, None):
            with self.assertRaises(ValueError):
                self.app.reverse_stock_count(count_id)
            with self.assertRaises(ValueError):
                self.app.get_stock_count_reversal(count_id)
        self.assertEqual(self.app.path.read_bytes(), before)
        # 失败不建目录。
        empty_root = Path(self.temp.name) / "empty"
        fresh = OrderDesk(empty_root)
        with self.assertRaises(ValueError):
            fresh.reverse_stock_count("c1")
        with self.assertRaises(ValueError):
            fresh.get_stock_count_reversal("c1")
        self.assertFalse(empty_root.exists())

    def test_query_before_reversal_rejected_and_readonly(self):
        self.app.count_stock("c1", [{"sku": "T", "on_hand": 8}])
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.get_stock_count_reversal("c1")
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_unmanaged_or_missing_product_rejects_whole_reversal(self):
        # 未纳管商品不能出现在盘点里，构造旧数据：盘点后商品被移出库存记录。
        self.app.count_stock("c1", [{"sku": "T", "on_hand": 8}, {"sku": "C", "on_hand": 2}])
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        del raw["inventory"]["C"]
        self.app._write(raw)
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.reverse_stock_count("c1")
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.stock("T")["on_hand"], 8)
        # 商品不存在同样整笔拒绝。
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        raw["inventory"]["C"] = {"on_hand": 2, "reserved": 0}
        del raw["products"]["C"]
        self.app._write(raw)
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.reverse_stock_count("c1")
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_negative_or_below_reserved_new_on_hand_rejects_whole_reversal(self):
        self.app.count_stock("c1", [{"sku": "T", "on_hand": 12}, {"sku": "C", "on_hand": 1}])
        # T 当前在库 12，盘点 delta 为 +2，撤销后为 10，正常；构造低于预留的场景：
        self.app.place("O1", [{"sku": "T", "quantity": 11}])
        before = self.app.path.read_bytes()
        # 撤销后 T 为 10，低于预留 11，整笔拒绝，C 不变。
        with self.assertRaises(ValueError):
            self.app.reverse_stock_count("c1")
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.stock("C")["on_hand"], 1)
        # 新在库量为负：盘点了 +5 后发货消耗，撤销后为负。
        self.app.cancel("O1")
        self.app.count_stock("c2", [{"sku": "C", "on_hand": 6}])
        self.app.place("O2", [{"sku": "C", "quantity": 6}])
        self.app.ship("O2", "UPS", "TRK1")
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.reverse_stock_count("c2")
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_stock_history_records_reverse_events(self):
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.count_stock("c1", [{"sku": "T", "on_hand": 8}, {"sku": "C", "on_hand": 4}])
        self.app.reverse_stock_count("c1")
        tea = self.app.stock_history("T")
        self.assertTrue(tea["complete"])
        last = tea["events"][-1]
        self.assertEqual(last["sequence"], len(tea["events"]))
        self.assertEqual(last["action"], "reverse-stock-count")
        self.assertEqual(last["reference_id"], "c1")
        self.assertEqual(last["before"], {"sku": "T", "on_hand": 8, "reserved": 3, "available": 5})
        self.assertEqual(last["after"], {"sku": "T", "on_hand": 10, "reserved": 3, "available": 7})
        # C 的盘点 delta 为零，盘点与撤销都不新增事件。
        coffee = self.app.stock_history("C")
        self.assertEqual([event["action"] for event in coffee["events"]], ["restock"])

    def test_legacy_product_without_history_starts_incomplete(self):
        # 旧商品：有库存记录但无盘点与历史记录。
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        raw.pop("stock_history", None)
        raw["stock_counts"] = {
            "c1": {
                "count_id": "c1",
                "lines": [{
                    "sku": "T",
                    "before": {"sku": "T", "on_hand": 10, "reserved": 0, "available": 10},
                    "after": {"sku": "T", "on_hand": 8, "reserved": 0, "available": 8},
                    "delta": -2,
                }],
            }
        }
        self.app._write(raw)
        self.app.reverse_stock_count("c1")
        document = self.app.stock_history("T")
        self.assertFalse(document["complete"])
        self.assertEqual(len(document["events"]), 1)
        event = document["events"][0]
        self.assertEqual(event["sequence"], 1)
        self.assertEqual(event["action"], "reverse-stock-count")
        self.assertEqual(event["after"]["on_hand"], 12)

    def test_receipt_persists_and_survives_reopen_and_later_operations(self):
        self.app.count_stock("c1", [{"sku": "T", "on_hand": 8}])
        result = self.app.reverse_stock_count("c1")
        snapshot_count = self.app.get_stock_count("c1")
        self.app.restock("T", 20)
        self.app.count_stock("c2", [{"sku": "T", "on_hand": 25}])
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.get_stock_count_reversal("c1"), result)
        self.assertEqual(reopened.get_stock_count("c1"), snapshot_count)
        # 查询始终只读。
        before = self.app.path.read_bytes()
        self.assertEqual(self.app.get_stock_count_reversal("c1"), result)
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_legacy_data_without_reversal_records_treated_as_empty(self):
        self.app.count_stock("c1", [{"sku": "T", "on_hand": 8}])
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        raw.pop("stock_count_reversals", None)
        self.app._write(raw)
        reopened = OrderDesk(self.root)
        with self.assertRaises(ValueError):
            reopened.get_stock_count_reversal("c1")
        # 撤销仍可进行。
        result = reopened.reverse_stock_count("c1")
        self.assertEqual(result["lines"][0]["after"]["on_hand"], 10)

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
        # 第一项已成功，凭据可查询。
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


if __name__ == "__main__":
    unittest.main()
