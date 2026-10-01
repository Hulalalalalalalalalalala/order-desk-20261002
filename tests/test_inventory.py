import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk

class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)

    def test_stock_view_before_and_after_restock(self):
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": None, "reserved": 0, "available": None})
        view = self.app.restock("T", 5)
        self.assertEqual(view, {"sku": "T", "on_hand": 5, "reserved": 0, "available": 5})
        view = self.app.restock("T", 2)
        self.assertEqual(view, {"sku": "T", "on_hand": 7, "reserved": 0, "available": 7})

    def test_reservation_scenario(self):
        self.app.restock("T", 5)
        order = self.app.place("O1", [{"sku": "T", "quantity": 2}, {"sku": "T", "quantity": 1}])
        self.assertEqual(order["total_cents"], 300)
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 5, "reserved": 3, "available": 2})
        with self.assertRaises(ValueError):
            self.app.place("O2", [{"sku": "T", "quantity": 3}])
        self.assertEqual(self.app.stock("T")["available"], 2)
        self.app.cancel("O1")
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 5, "reserved": 0, "available": 5})

    def test_shortage_is_atomic_across_products(self):
        self.app.restock("T", 5)
        self.app.restock("C", 1)
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.place("O1", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 3}])
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.stock("T")["reserved"], 0)
        self.assertEqual(self.app.stock("C")["reserved"], 0)
        with self.assertRaises(ValueError):
            self.app.get("O1")

    def test_unmanaged_product_is_not_limited(self):
        self.app.place("O1", [{"sku": "C", "quantity": 1000}])
        self.assertEqual(self.app.stock("C"), {"sku": "C", "on_hand": None, "reserved": 0, "available": None})
        self.assertEqual(self.app.cancel("O1")["status"], "cancelled")
        self.assertEqual(self.app.stock("C")["reserved"], 0)

    def test_legacy_order_gets_no_reservation_and_cannot_release_others(self):
        self.app.place("old", [{"sku": "T", "quantity": 4}])
        self.app.restock("T", 5)
        self.app.place("new", [{"sku": "T", "quantity": 3}])
        self.assertEqual(self.app.stock("T")["reserved"], 3)
        self.assertEqual(self.app.cancel("old")["status"], "cancelled")
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 5, "reserved": 3, "available": 2})
        self.assertEqual(self.app.cancel("new")["status"], "cancelled")
        self.assertEqual(self.app.stock("T")["reserved"], 0)

    def test_duplicate_order_id_still_rejected_under_stock(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        with self.assertRaises(ValueError):
            self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.stock("T")["reserved"], 2)

    def test_restock_validation_leaves_data_unchanged(self):
        self.app.restock("T", 5)
        before = self.app.path.read_bytes()
        for kwargs in ({"sku": "X", "quantity": 1}, {"sku": "  ", "quantity": 1}, {"sku": 3, "quantity": 1},
                       {"sku": "T", "quantity": 0}, {"sku": "T", "quantity": -2}, {"sku": "T", "quantity": True},
                       {"sku": "T", "quantity": 1.5}, {"sku": "T", "quantity": "1"}):
            with self.assertRaises(ValueError):
                self.app.restock(**kwargs)
        for sku in ("X", "  ", 3, True):
            with self.assertRaises(ValueError):
                self.app.stock(sku)
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.stock("T")["on_hand"], 5)

    def test_stock_survives_reopen_with_old_data(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 9}])
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.stock("T"), {"sku": "T", "on_hand": 5, "reserved": 2, "available": 3})
        self.assertEqual(reopened.stock("C"), {"sku": "C", "on_hand": None, "reserved": 0, "available": None})
        reopened.cancel("O1")
        self.assertEqual(OrderDesk(self.root).stock("T")["reserved"], 0)
        # An order document without inventory keys (legacy data shape) still behaves as unmanaged.
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        del raw["inventory"]
        del raw["reservations"]
        self.app._write(raw)
        legacy = OrderDesk(self.root)
        self.assertEqual(legacy.stock("T"), {"sku": "T", "on_hand": None, "reserved": 0, "available": None})

    def test_order_document_has_no_new_fields(self):
        self.app.restock("T", 5)
        order = self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.assertEqual(set(order), {"order_id", "status", "lines", "total_cents"})
        self.assertEqual(set(order["lines"][0]), {"sku", "quantity", "unit_price_cents", "subtotal_cents"})

    def test_cli_restock_stock_and_array_partial_failure(self):
        payload = self.root / "batch.json"
        payload.write_text(json.dumps([
            {"sku": "T", "quantity": 5},
            {"sku": "C", "quantity": 3},
            {"sku": "X", "quantity": 1},
        ]), encoding="utf-8")
        failed = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "restock", str(payload)],
                                text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2, failed.stdout)
        error = json.loads(failed.stderr)
        self.assertIn("unknown product", error["error"])
        # Successful rows before the failure are retained.
        query = self.root / "sku-t.json"
        query.write_text(json.dumps({"sku": "T"}), encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "stock", str(query)],
                            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout)["available"], 5)
        query.write_text(json.dumps({"sku": "C"}), encoding="utf-8")
        ok2 = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "stock", str(query)],
                             text=True, capture_output=True)
        self.assertEqual(json.loads(ok2.stdout)["on_hand"], 3)

if __name__ == "__main__":
    unittest.main()
