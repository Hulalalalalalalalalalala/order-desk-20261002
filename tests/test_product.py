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

if __name__ == "__main__":
    unittest.main()
