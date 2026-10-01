import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk

class ProductTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)

    def test_order_price_and_reopen(self):
        self.app.add_product("T", "Tea", 125)
        result = self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.assertEqual(result["total_cents"], 375)
        self.assertEqual(OrderDesk(self.root).get("O1"), result)

    def test_bad_line_leaves_orders_unchanged(self):
        self.app.add_product("T", "Tea", 125)
        before = self.app.path.read_bytes()
        for quantity in [0, -1, True]:
            with self.assertRaises(ValueError):
                self.app.place("bad", [{"sku": "T", "quantity": quantity}])
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_cancellation_is_one_transition(self):
        self.app.add_product("T", "Tea", 125)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.cancel("O1")["status"], "cancelled")
        with self.assertRaises(ValueError):
            self.app.cancel("O1")

    def test_cli_demo_and_invalid_action(self):
        result = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "demo"], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["total_cents"], 8600)
        failed = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "not-an-action"], text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2)


class StockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 125)

    def test_unmanaged_stock_view(self):
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": None, "reserved": 0, "available": None})

    def test_restock_reserve_cancel_cycle(self):
        self.assertEqual(self.app.restock("T", 5), {"sku": "T", "on_hand": 5, "reserved": 0, "available": 5})
        self.app.place("O1", [{"sku": "T", "quantity": 2}, {"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 5, "reserved": 3, "available": 2})
        with self.assertRaises(ValueError):
            self.app.place("O2", [{"sku": "T", "quantity": 3}])
        self.assertEqual(self.app.stock("T")["reserved"], 3)
        self.assertEqual(self.app.cancel("O1")["status"], "cancelled")
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 5, "reserved": 0, "available": 5})
        self.assertEqual(OrderDesk(self.root).stock("T"), {"sku": "T", "on_hand": 5, "reserved": 0, "available": 5})

    def test_shortage_does_not_occupy_other_skus(self):
        self.app.add_product("C", "Coffee", 200)
        self.app.restock("T", 2)
        self.app.restock("C", 2)
        with self.assertRaises(ValueError):
            self.app.place("O1", [{"sku": "C", "quantity": 1}, {"sku": "T", "quantity": 5}])
        self.assertEqual(self.app.stock("C")["reserved"], 0)
        self.assertEqual(self.app.stock("T")["reserved"], 0)
        self.assertEqual(self.app.list_orders(), [])

    def test_legacy_order_releases_nothing(self):
        self.app.place("OLD", [{"sku": "T", "quantity": 10}])
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.assertEqual(self.app.cancel("OLD")["status"], "cancelled")
        self.assertEqual(self.app.stock("T")["reserved"], 3)

    def test_unmanaged_orders_ignore_stock(self):
        self.app.place("O1", [{"sku": "T", "quantity": 1000}])
        self.assertEqual(self.app.stock("T")["reserved"], 0)

    def test_restock_validation_leaves_data_unchanged(self):
        self.app.restock("T", 1)
        before = self.app.path.read_bytes()
        for call in [
            lambda: self.app.restock("NOPE", 1),
            lambda: self.app.stock("NOPE"),
            lambda: self.app.restock("  ", 1),
            lambda: self.app.restock(None, 1),
            lambda: self.app.restock("T", 0),
            lambda: self.app.restock("T", -1),
            lambda: self.app.restock("T", True),
            lambda: self.app.restock("T", 1.5),
            lambda: self.app.restock("T", "1"),
            lambda: self.app.stock(" "),
            lambda: self.app.stock(9),
        ]:
            with self.assertRaises(ValueError):
                call()
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_old_data_without_inventory_opens(self):
        legacy = self.root / "data.json"
        legacy.write_text(json.dumps({"products": {"T": {"sku": "T", "name": "Tea", "price_cents": 125}}, "orders": {}}), encoding="utf-8")
        self.assertEqual(OrderDesk(self.root).stock("T")["available"], None)

if __name__ == "__main__":
    unittest.main()
