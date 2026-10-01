import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk

class ShipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)

    def test_shipment_document_preserves_order_snapshot(self):
        self.app.restock("T", 5)
        order = self.app.place("O1", [{"sku": "T", "quantity": 2}, {"sku": "T", "quantity": 1}])
        shipped = self.app.ship("O1", "  DHL  ", " X-1 ")
        self.assertEqual(shipped["status"], "shipped")
        self.assertEqual(shipped["shipment"], {"carrier": "DHL", "tracking_no": "X-1"})
        self.assertEqual(set(shipped), {"order_id", "status", "lines", "total_cents", "shipment"})
        self.assertEqual(shipped["lines"], order["lines"])
        self.assertEqual(shipped["total_cents"], order["total_cents"])
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.get("O1"), shipped)
        self.assertEqual(reopened.list_orders()[0]["shipment"], shipped["shipment"])

    def test_ship_deducts_reserved_quantities_for_two_orders(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.place("O2", [{"sku": "T", "quantity": 2}])
        self.app.ship("O1", "DHL", "1")
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 7, "reserved": 2, "available": 5})
        self.app.ship("O2", "DHL", "2")
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 5, "reserved": 0, "available": 5})

    def test_unmanaged_and_later_managed_products_ship_without_stock_change(self):
        self.app.add_product("U", "Unrestocked", 50)
        self.app.place("O1", [{"sku": "U", "quantity": 1000}])
        shipped = self.app.ship("O1", "DHL", "1")
        self.assertEqual(shipped["status"], "shipped")
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("U", raw.get("inventory", {}))
        # Product managed only after the order was placed: no current stock is deducted.
        self.app.place("O2", [{"sku": "C", "quantity": 4}])
        self.app.restock("C", 9)
        self.app.ship("O2", "DHL", "2")
        self.assertEqual(self.app.stock("C"), {"sku": "C", "on_hand": 9, "reserved": 0, "available": 9})

    def test_mixed_order_with_duplicate_lines(self):
        self.app.add_product("U", "Unrestocked", 50)
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 2}, {"sku": "U", "quantity": 4}, {"sku": "T", "quantity": 1}])
        self.app.ship("O1", "DHL", "1")
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 7, "reserved": 0, "available": 7})

    def test_reserved_order_ships_even_when_available_is_zero(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.place("O2", [{"sku": "T", "quantity": 7}])
        self.app.ship("O2", "DHL", "2")
        self.assertEqual(self.app.stock("T")["available"], 0)
        self.app.ship("O1", "DHL", "1")
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 0, "reserved": 0, "available": 0})

    def test_invalid_shipments_are_rejected_and_leave_data_unchanged(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.app.cancel("O1")
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        self.app.ship("O2", "DHL", "2")
        before = self.app.path.read_bytes()
        for kwargs in (
            {"order_id": "missing", "carrier": "DHL", "tracking_no": "1"},
            {"order_id": "O1", "carrier": "DHL", "tracking_no": "1"},
            {"order_id": "O2", "carrier": "UPS", "tracking_no": "9"},
            {"order_id": "O2", "carrier": "  ", "tracking_no": "1"},
            {"order_id": "O2", "carrier": "DHL", "tracking_no": ""},
            {"order_id": 3, "carrier": "DHL", "tracking_no": "1"},
        ):
            with self.assertRaises(ValueError):
                self.app.ship(**kwargs)
        with self.assertRaises(ValueError):
            self.app.cancel("O2")
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.get("O2")["shipment"], {"carrier": "DHL", "tracking_no": "2"})
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 4, "reserved": 0, "available": 4})

    def test_placed_and_cancelled_orders_have_no_shipment_field(self):
        self.app.place("O1", [{"sku": "C", "quantity": 1}])
        self.app.place("O2", [{"sku": "C", "quantity": 1}])
        self.app.cancel("O2")
        self.assertNotIn("shipment", self.app.get("O1"))
        self.assertNotIn("shipment", self.app.get("O2"))

    def test_cli_ship_success_failure_and_array_partial_failure(self):
        self.app.restock("T", 10)
        self.app.place("A", [{"sku": "T", "quantity": 3}])
        self.app.place("B", [{"sku": "T", "quantity": 2}])
        payload = self.root / "ship.json"
        payload.write_text(json.dumps({"order_id": "A", "carrier": "DHL", "tracking_no": "T-1"}), encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "ship", str(payload)],
                            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout)["status"], "shipped")
        # Repeating the same shipment fails with exit 2 and changes nothing.
        failed = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "ship", str(payload)],
                                text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2)
        self.assertIn("error", json.loads(failed.stderr))
        # Array input runs in order and stops at the first error; earlier rows are retained.
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([
            {"order_id": "B", "carrier": "UPS", "tracking_no": "B-1"},
            {"order_id": "missing", "carrier": "UPS", "tracking_no": "X"},
        ]), encoding="utf-8")
        stopped = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "ship", str(batch)],
                                 text=True, capture_output=True)
        self.assertEqual(stopped.returncode, 2, stopped.stdout)
        self.assertIn("error", json.loads(stopped.stderr))
        self.assertEqual(OrderDesk(self.root).get("B")["status"], "shipped")
        self.assertEqual(OrderDesk(self.root).stock("T"), {"sku": "T", "on_hand": 5, "reserved": 0, "available": 5})

if __name__ == "__main__":
    unittest.main()
