import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk


class RepriceProductsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)

    def test_batch_reprice_succeeds_and_persists_sorted(self):
        result = self.app.reprice_products([
            {"sku": "C", "expected_price_cents": 200, "price_cents": 250},
            {"sku": " T ", "expected_price_cents": 100, "price_cents": 150,
             "note": "ignored"},
        ])
        self.assertEqual(result, [
            {"sku": "C",
             "before": {"sku": "C", "name": "Coffee", "price_cents": 200, "enabled": True},
             "after": {"sku": "C", "name": "Coffee", "price_cents": 250, "enabled": True}},
            {"sku": "T",
             "before": {"sku": "T", "name": "Tea", "price_cents": 100, "enabled": True},
             "after": {"sku": "T", "name": "Tea", "price_cents": 150, "enabled": True}},
        ])
        self.assertEqual(OrderDesk(self.root).get_product("T")["price_cents"], 150)
        self.assertEqual(OrderDesk(self.root).get_product("C")["price_cents"], 250)
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["products"]["T"]["price_cents"], 150)
        # Name and the missing enabled flag are preserved.
        self.assertEqual(raw["products"]["T"]["name"], "Tea")
        self.assertNotIn("enabled", raw["products"]["T"])

    def test_zero_price_is_valid(self):
        result = self.app.reprice_products(
            [{"sku": "T", "expected_price_cents": 100, "price_cents": 0}])
        self.assertEqual(result[0]["after"]["price_cents"], 0)
        self.assertEqual(OrderDesk(self.root).get_product("T")["price_cents"], 0)

    def test_same_price_line_is_returned_but_all_same_batch_does_not_write(self):
        before = self.app.path.read_bytes()
        result = self.app.reprice_products(
            [{"sku": "T", "expected_price_cents": 100, "price_cents": 100}])
        self.assertEqual(result[0]["before"], result[0]["after"])
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_invalid_lines_shape_rejected(self):
        before = self.app.path.read_bytes()
        for lines in (None, [], {}, "x", 123, True):
            with self.subTest(lines=lines):
                with self.assertRaises(ValueError):
                    self.app.reprice_products(lines)
        for bad in (None, "x", 123, True, []):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.app.reprice_products([bad])
        # Missing required fields.
        for line in (
            {"expected_price_cents": 100, "price_cents": 150},
            {"sku": "T", "price_cents": 150},
            {"sku": "T", "expected_price_cents": 100},
            {},
        ):
            with self.subTest(line=line):
                with self.assertRaises(ValueError):
                    self.app.reprice_products([line])
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_invalid_sku_rejected(self):
        for sku in (None, 123, True, "   ", ""):
            with self.subTest(sku=sku):
                with self.assertRaises(ValueError):
                    self.app.reprice_products(
                        [{"sku": sku, "expected_price_cents": 100, "price_cents": 150}])
        # Case sensitive after trimming.
        with self.assertRaises(ValueError):
            self.app.reprice_products(
                [{"sku": "t", "expected_price_cents": 100, "price_cents": 150}])
        with self.assertRaises(ValueError):
            self.app.reprice_products(
                [{"sku": "X", "expected_price_cents": 0, "price_cents": 10}])

    def test_invalid_prices_rejected(self):
        for bad in (True, False, "100", 100.0, -1, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.app.reprice_products(
                        [{"sku": "T", "expected_price_cents": bad, "price_cents": 150}])
                with self.assertRaises(ValueError):
                    self.app.reprice_products(
                        [{"sku": "T", "expected_price_cents": 100, "price_cents": bad}])

    def test_duplicate_sku_rejected_even_with_same_target(self):
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.reprice_products([
                {"sku": " T ", "expected_price_cents": 100, "price_cents": 150},
                {"sku": "T", "expected_price_cents": 100, "price_cents": 150},
            ])
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_expected_price_mismatch_rejected_atomically(self):
        before = self.app.path.read_bytes()
        # Failure on the last item must not keep earlier changes.
        with self.assertRaises(ValueError):
            self.app.reprice_products([
                {"sku": "C", "expected_price_cents": 200, "price_cents": 250},
                {"sku": "T", "expected_price_cents": 101, "price_cents": 150},
            ])
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.get_product("C")["price_cents"], 200)
        self.assertEqual(self.app.get_product("T")["price_cents"], 100)

    def test_failure_never_creates_directory(self):
        fresh = self.root / "fresh"
        with self.assertRaises(ValueError):
            OrderDesk(fresh).reprice_products(
                [{"sku": "T", "expected_price_cents": 100, "price_cents": 150}])
        self.assertFalse(fresh.exists())
        with self.assertRaises(ValueError):
            OrderDesk(fresh).reprice_products([])
        self.assertFalse(fresh.exists())

    def test_paused_product_repriced_without_resuming(self):
        self.app.set_product_enabled("T", False)
        result = self.app.reprice_products(
            [{"sku": "T", "expected_price_cents": 100, "price_cents": 150}])
        self.assertFalse(result[0]["before"]["enabled"])
        self.assertFalse(result[0]["after"]["enabled"])
        self.assertFalse(OrderDesk(self.root).get_product("T")["enabled"])
        self.assertEqual(OrderDesk(self.root).get_product("T")["price_cents"], 150)

    def test_unmanaged_product_repriced_without_being_managed(self):
        self.app.reprice_products(
            [{"sku": "T", "expected_price_cents": 100, "price_cents": 150}])
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": None, "reserved": 0, "available": None})

    def test_reprice_preserves_deals_history_stock_and_carts(self):
        self.app.restock("T", 10)
        self.app.save_cart("K1", [{"sku": "T", "quantity": 2}])
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.reprice_products([
            {"sku": "T", "expected_price_cents": 100, "price_cents": 150}])
        # Existing order keeps its deal price and amount.
        order = self.app.get("O1")
        self.assertEqual(order["lines"][0]["unit_price_cents"], 100)
        self.assertEqual(order["total_cents"], 200)
        self.assertEqual([e["action"] for e in self.app.history("O1")["events"]], ["place"])
        self.assertEqual(self.app.history("O1")["events"][0]["result"]["total_cents"], 200)
        # Stock and reservations are untouched.
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 10, "reserved": 2, "available": 8})
        # The saved cart keeps only sku and quantity.
        self.assertEqual(self.app.get_cart("K1"),
                         {"cart_id": "K1", "lines": [{"sku": "T", "quantity": 2}]})
        # Quote and new placement price at the new catalog.
        quote = self.app.quote([{"sku": "T", "quantity": 2}])
        self.assertEqual(quote["total_cents"], 300)
        placed = self.app.place("O2", [{"sku": "T", "quantity": 2}])
        self.assertEqual(placed["total_cents"], 300)
        # Cart checkout prices at the new catalog and keeps cart contents until then.
        checked = self.app.checkout_cart("K1", "O3")
        self.assertEqual(checked["total_cents"], 300)
        with self.assertRaises(ValueError):
            self.app.get_cart("K1")
        # amend recomputes the old order's lines at the current catalog.
        amended = self.app.amend("O1", [{"sku": "T", "quantity": 2}])
        self.assertEqual(amended["lines"][0]["unit_price_cents"], 150)
        self.assertEqual(amended["total_cents"], 300)

    def test_no_history_or_sequence_consumed_on_failure(self):
        self.app.restock("T", 2)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.reprice_products([
                {"sku": "T", "expected_price_cents": 100, "price_cents": 150},
                {"sku": "X", "expected_price_cents": 0, "price_cents": 10},
            ])
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual([e["action"] for e in self.app.history("O1")["events"]], ["place"])
        self.assertEqual(self.app.stock_history("T")["events"][-1]["sequence"], 2)


class RepriceCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)

    def run_cli(self, payload):
        path = self.root / "in.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "reprice-products", str(path)],
            text=True, capture_output=True)

    def test_command_success_and_business_rejection(self):
        ok = self.run_cli({"lines": [
            {"sku": " T ", "expected_price_cents": 100, "price_cents": 150}]})
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout), [{
            "sku": "T",
            "before": {"sku": "T", "name": "Tea", "price_cents": 100, "enabled": True},
            "after": {"sku": "T", "name": "Tea", "price_cents": 150, "enabled": True},
        }])
        failed = self.run_cli({"lines": [
            {"sku": "C", "expected_price_cents": 199, "price_cents": 0}]})
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(failed.stdout, "")
        self.assertIn("error", json.loads(failed.stderr))

    def test_outer_array_partial_failure_is_not_rolled_back(self):
        run = self.run_cli([
            {"lines": [{"sku": "T", "expected_price_cents": 100, "price_cents": 150}]},
            {"lines": [{"sku": "C", "expected_price_cents": 199, "price_cents": 0}]},
        ])
        self.assertEqual(run.returncode, 2)
        self.assertEqual(run.stdout, "")
        app = OrderDesk(self.root)
        self.assertEqual(app.get_product("T")["price_cents"], 150)
        self.assertEqual(app.get_product("C")["price_cents"], 200)


if __name__ == "__main__":
    unittest.main()
