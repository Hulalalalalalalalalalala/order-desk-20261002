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

    def test_batch_reprice_succeeds_and_persists(self):
        result = self.app.reprice_products([
            {"sku": "C", "expected_price_cents": 200, "price_cents": 250},
            {"sku": " T ", "expected_price_cents": 100, "price_cents": 150},
        ])
        # Result is sorted by normalized sku regardless of input order.
        self.assertEqual([row["sku"] for row in result], ["C", "T"])
        self.assertEqual(result[0], {
            "sku": "C",
            "before": {"sku": "C", "name": "Coffee", "price_cents": 200, "enabled": True},
            "after": {"sku": "C", "name": "Coffee", "price_cents": 250, "enabled": True},
        })
        self.assertEqual(result[1]["before"]["price_cents"], 100)
        self.assertEqual(result[1]["after"]["price_cents"], 150)
        self.assertEqual(OrderDesk(self.root).get_product("T")["price_cents"], 150)
        self.assertEqual(OrderDesk(self.root).get_product("C")["price_cents"], 250)

    def test_zero_price_valid_but_other_types_rejected(self):
        ok = self.app.reprice_products([{"sku": "T", "expected_price_cents": 100, "price_cents": 0}])
        self.assertEqual(ok[0]["after"]["price_cents"], 0)
        bad = [
            {"sku": "T", "expected_price_cents": True, "price_cents": 1},
            {"sku": "T", "expected_price_cents": 0, "price_cents": False},
            {"sku": "T", "expected_price_cents": "100", "price_cents": 1},
            {"sku": "T", "expected_price_cents": 100, "price_cents": 1.5},
            {"sku": "T", "expected_price_cents": -1, "price_cents": 1},
            {"sku": "T", "expected_price_cents": None, "price_cents": 1},
            {"sku": "T", "expected_price_cents": 100},
            {"sku": "T", "price_cents": 1},
        ]
        for line in bad:
            with self.subTest(line=line):
                with self.assertRaises(ValueError):
                    self.app.reprice_products([line])

    def test_invalid_lines_and_items(self):
        for lines in (None, "x", 1, True, [], {}):
            with self.subTest(lines=lines):
                with self.assertRaises(ValueError):
                    self.app.reprice_products(lines)
        for item in (None, "T", 7, True, ["x"]):
            with self.subTest(item=item):
                with self.assertRaises(ValueError):
                    self.app.reprice_products([item])
        for sku in (None, 123, True, "   ", ""):
            with self.subTest(sku=sku):
                with self.assertRaises(ValueError):
                    self.app.reprice_products(
                        [{"sku": sku, "expected_price_cents": 100, "price_cents": 1}]
                    )

    def test_unknown_and_case_sensitive_sku(self):
        with self.assertRaises(ValueError):
            self.app.reprice_products([{"sku": "X", "expected_price_cents": 0, "price_cents": 1}])
        with self.assertRaises(ValueError):
            self.app.reprice_products([{"sku": "t", "expected_price_cents": 0, "price_cents": 1}])

    def test_duplicate_sku_rejected_even_when_identical(self):
        with self.assertRaises(ValueError):
            self.app.reprice_products([
                {"sku": "T", "expected_price_cents": 100, "price_cents": 100},
                {"sku": " T ", "expected_price_cents": 100, "price_cents": 100},
            ])

    def test_expected_price_mismatch_rejects_whole_batch(self):
        with self.assertRaises(ValueError):
            self.app.reprice_products([
                {"sku": "C", "expected_price_cents": 200, "price_cents": 250},
                {"sku": "T", "expected_price_cents": 99, "price_cents": 150},
            ])

    def test_extra_line_fields_ignored(self):
        result = self.app.reprice_products([
            {"sku": "T", "expected_price_cents": 100, "price_cents": 150, "note": "x", "enabled": False}
        ])
        self.assertEqual(result[0]["after"]["price_cents"], 150)
        self.assertTrue(self.app.get_product("T")["enabled"])

    def test_failure_is_all_or_nothing_even_on_last_line(self):
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.reprice_products([
                {"sku": "C", "expected_price_cents": 200, "price_cents": 250},
                {"sku": "T", "expected_price_cents": 99, "price_cents": 150},
            ])
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.get_product("C")["price_cents"], 200)
        self.assertEqual(self.app.get_product("T")["price_cents"], 100)
        # A rejected request on a fresh root never creates the directory.
        fresh = self.root / "fresh"
        with self.assertRaises(ValueError):
            OrderDesk(fresh).reprice_products(
                [{"sku": "T", "expected_price_cents": 0, "price_cents": 1}]
            )
        self.assertFalse(fresh.exists())

    def test_no_actual_change_succeeds_without_writing(self):
        before = self.app.path.read_bytes()
        result = self.app.reprice_products([
            {"sku": "C", "expected_price_cents": 200, "price_cents": 200},
            {"sku": "T", "expected_price_cents": 100, "price_cents": 100},
        ])
        self.assertEqual([row["sku"] for row in result], ["C", "T"])
        self.assertEqual(result[0]["before"], result[0]["after"])
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_paused_and_unmanaged_products_keep_their_state(self):
        self.app.set_product_enabled("T", False)
        result = self.app.reprice_products([
            {"sku": "T", "expected_price_cents": 100, "price_cents": 150},
            {"sku": "C", "expected_price_cents": 200, "price_cents": 0},
        ])
        by_sku = {row["sku"]: row for row in result}
        self.assertFalse(by_sku["T"]["before"]["enabled"])
        self.assertFalse(by_sku["T"]["after"]["enabled"])
        # Paused stays paused; the unmanaged product is not managed by reprice.
        self.assertFalse(self.app.get_product("T")["enabled"])
        self.assertIsNone(self.app.stock("T")["on_hand"])
        self.assertIsNone(self.app.stock("C")["on_hand"])
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("inventory", raw)
        self.assertIs(raw["products"]["T"]["enabled"], False)
        # Name, stock and reservations are untouched.
        self.app.restock("T", 5)
        self.app.set_product_enabled("T", True)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.set_product_enabled("T", False)
        self.app.reprice_products([{"sku": "T", "expected_price_cents": 150, "price_cents": 300}])
        self.assertEqual(self.app.get_product("T")["name"], "Tea")
        self.assertFalse(self.app.get_product("T")["enabled"])
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 5, "reserved": 2, "available": 3})

    def test_legacy_missing_enabled_is_not_filled_in(self):
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("enabled", raw["products"]["T"])
        self.app.reprice_products([{"sku": "T", "expected_price_cents": 100, "price_cents": 150}])
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("enabled", raw["products"]["T"])
        self.assertTrue(self.app.get_product("T")["enabled"])

    def test_no_history_or_sequence_is_added(self):
        self.app.restock("T", 5)
        order = self.app.place("O1", [{"sku": "T", "quantity": 2}])
        before_history = self.app.history("O1")
        before_stock = self.app.stock_history("T")
        self.app.reprice_products([{"sku": "T", "expected_price_cents": 100, "price_cents": 150}])
        self.assertEqual(self.app.history("O1"), before_history)
        self.assertEqual(self.app.stock_history("T"), before_stock)
        # Existing orders keep their deal lines, prices, amounts and snapshots.
        self.assertEqual(self.app.get("O1"), order)
        self.assertEqual(self.app.get("O1")["total_cents"], 200)

    def test_pricing_after_reprice(self):
        self.app.restock("T", 10)
        self.app.place("OLD", [{"sku": "T", "quantity": 2}])  # 200 at the old price
        self.app.reprice_products([{"sku": "T", "expected_price_cents": 100, "price_cents": 150}])
        # The closed deal never changes.
        self.assertEqual(self.app.get("OLD")["total_cents"], 200)
        # quote and new orders use the latest catalog.
        quote = self.app.quote([{"sku": "T", "quantity": 2}])
        self.assertEqual(quote["lines"][0]["unit_price_cents"], 150)
        self.assertEqual(quote["total_cents"], 300)
        order = self.app.place("NEW", [{"sku": "T", "quantity": 2}])
        self.assertEqual(order["total_cents"], 300)
        # A saved cart keeps only sku/quantity; checkout is priced at checkout time.
        self.app.save_cart("K1", [{"sku": "T", "quantity": 2}])
        self.assertEqual(self.app.get_cart("K1"),
                         {"cart_id": "K1", "lines": [{"sku": "T", "quantity": 2}]})
        checkout = self.app.checkout_cart("K1", "FROMCART")
        self.assertEqual(checkout["total_cents"], 300)
        # amend recomputes every line against the current catalog, including
        # lines whose quantity is unchanged.
        amended = self.app.amend("OLD", [{"sku": "T", "quantity": 2}])
        self.assertEqual(amended["total_cents"], 300)
        self.assertEqual(amended["lines"][0]["unit_price_cents"], 150)


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
            text=True, capture_output=True,
        )

    def test_success_returns_sorted_json(self):
        run = self.run_cli({"lines": [
            {"sku": "C", "expected_price_cents": 200, "price_cents": 250},
            {"sku": "T", "expected_price_cents": 100, "price_cents": 150},
        ]})
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual([row["sku"] for row in json.loads(run.stdout)], ["C", "T"])

    def test_business_rejection_goes_to_stderr(self):
        run = self.run_cli({"lines": [{"sku": "T", "expected_price_cents": 99, "price_cents": 150}]})
        self.assertEqual(run.returncode, 2)
        self.assertEqual(run.stdout, "")
        self.assertIn("error", json.loads(run.stderr))
        self.assertEqual(OrderDesk(self.root).get_product("T")["price_cents"], 100)

    def test_outer_array_requests_remain_independent(self):
        run = self.run_cli([
            {"lines": [{"sku": "T", "expected_price_cents": 100, "price_cents": 150}]},
            {"lines": [{"sku": "C", "expected_price_cents": 99, "price_cents": 1}]},
        ])
        self.assertEqual(run.returncode, 2)
        self.assertEqual(run.stdout, "")
        # The first successful request is kept; the failed one is not rolled back.
        app = OrderDesk(self.root)
        self.assertEqual(app.get_product("T")["price_cents"], 150)
        self.assertEqual(app.get_product("C")["price_cents"], 200)


if __name__ == "__main__":
    unittest.main()
