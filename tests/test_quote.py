import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk


class QuoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)
        self.app.add_product("Z", "Free", 0)

    def test_merges_sorts_and_prices_from_catalog(self):
        quote = self.app.quote([
            {"sku": " T ", "quantity": 1, "note": "ignored"},
            {"sku": "T", "quantity": 2},
            {"sku": "C", "quantity": 4},
            {"sku": "Z", "quantity": 7},
        ])
        self.assertEqual(set(quote), {"lines", "total_cents", "can_place"})
        self.assertEqual([line["sku"] for line in quote["lines"]], ["C", "T", "Z"])
        tea = quote["lines"][1]
        self.assertEqual(tea, {
            "sku": "T", "quantity": 3, "unit_price_cents": 100,
            "subtotal_cents": 300, "available": None, "shortfall": 0,
        })
        self.assertEqual(quote["lines"][0]["subtotal_cents"], 800)
        # Zero-price and unmanaged products stay in the quote.
        self.assertEqual(quote["lines"][2], {
            "sku": "Z", "quantity": 7, "unit_price_cents": 0,
            "subtotal_cents": 0, "available": None, "shortfall": 0,
        })
        self.assertEqual(quote["total_cents"], 1100)
        self.assertTrue(quote["can_place"])
        for line in quote["lines"]:
            self.assertEqual(set(line), {
                "sku", "quantity", "unit_price_cents",
                "subtotal_cents", "available", "shortfall",
            })

    def test_managed_shortfall_scenario_from_spec(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        quote = self.app.quote([{"sku": "T", "quantity": 1}, {"sku": "T", "quantity": 2}])
        line = quote["lines"][0]
        self.assertEqual(line["quantity"], 3)
        self.assertEqual(line["available"], 2)
        self.assertEqual(line["shortfall"], 1)
        self.assertEqual(line["subtotal_cents"], 300)
        self.assertEqual(quote["total_cents"], 300)
        self.assertFalse(quote["can_place"])

    def test_exact_availability_can_place(self):
        self.app.restock("C", 4)
        quote = self.app.quote([{"sku": "C", "quantity": 2}, {"sku": "C", "quantity": 2}])
        self.assertEqual(quote["lines"][0]["available"], 4)
        self.assertEqual(quote["lines"][0]["shortfall"], 0)
        self.assertTrue(quote["can_place"])

    def test_unmanaged_and_mixed_products(self):
        self.app.restock("T", 1)
        quote = self.app.quote([{"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 9}])
        by_sku = {line["sku"]: line for line in quote["lines"]}
        self.assertEqual(by_sku["T"]["available"], 1)
        self.assertEqual(by_sku["T"]["shortfall"], 4)
        self.assertIsNone(by_sku["C"]["available"])
        self.assertEqual(by_sku["C"]["shortfall"], 0)
        self.assertFalse(quote["can_place"])

    def test_invalid_inputs_raise_value_error(self):
        cases = [
            None, [], "x", 3, {"sku": "T", "quantity": 1},
            ["x"], [42], [None], [{}],
            [{"quantity": 1}], [{"sku": "T"}],
            [{"sku": "  ", "quantity": 1}], [{"sku": 3, "quantity": 1}],
            [{"sku": "T", "quantity": 0}], [{"sku": "T", "quantity": -1}],
            [{"sku": "T", "quantity": 1.5}], [{"sku": "T", "quantity": True}],
            [{"sku": "T", "quantity": "1"}], [{"sku": "X", "quantity": 1}],
        ]
        for lines in cases:
            with self.subTest(lines=lines):
                with self.assertRaises(ValueError):
                    self.app.quote(lines)

    def test_quote_never_writes_or_changes_state(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        before = self.app.path.read_bytes()
        self.app.quote([{"sku": "T", "quantity": 9}, {"sku": "C", "quantity": 2}])
        with self.assertRaises(ValueError):
            self.app.quote([{"sku": "X", "quantity": 1}])
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 5, "reserved": 3, "available": 2})
        with self.assertRaises(ValueError):
            self.app.get("O2")
        # Same data gives the same result, including after reopening the root.
        lines = [{"sku": "T", "quantity": 1}, {"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}]
        first = self.app.quote(lines)
        self.assertEqual(OrderDesk(self.root).quote(lines), first)
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_legacy_data_without_inventory_treated_as_unmanaged(self):
        self.app.restock("T", 5)
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        del raw["inventory"]
        self.app._write(raw)
        quote = OrderDesk(self.root).quote([{"sku": "T", "quantity": 100}])
        self.assertIsNone(quote["lines"][0]["available"])
        self.assertEqual(quote["lines"][0]["shortfall"], 0)
        self.assertTrue(quote["can_place"])

    def test_cli_quote_success_and_failure(self):
        payload = self.root / "quote.json"
        payload.write_text(json.dumps({"lines": [{"sku": "T", "quantity": 2}]}), encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "quote", str(payload)],
                            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(ok.stderr, "")
        result = json.loads(ok.stdout)
        self.assertEqual(result["total_cents"], 200)
        self.assertTrue(result["can_place"])
        payload.write_text(json.dumps({"lines": [{"sku": "X", "quantity": 1}]}), encoding="utf-8")
        bad = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "quote", str(payload)],
                             text=True, capture_output=True)
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(bad.stdout, "")
        self.assertIn("error", json.loads(bad.stderr))
        # Quote must not modify an existing data file.
        seeded = self.root / "seeded"
        seeded.mkdir()
        (seeded / "data.json").write_bytes(self.app.path.read_bytes())
        before = (seeded / "data.json").read_bytes()
        payload.write_text(json.dumps({"lines": [{"sku": "T", "quantity": 1}]}), encoding="utf-8")
        run = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(seeded), "quote", str(payload)],
                             text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual((seeded / "data.json").read_bytes(), before)

    def test_cli_quote_array_applies_each_operation_independently(self):
        payload = self.root / "batch.json"
        payload.write_text(json.dumps([
            {"lines": [{"sku": "T", "quantity": 1}]},
            {"lines": [{"sku": "X", "quantity": 1}]},
        ]), encoding="utf-8")
        run = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "quote", str(payload)],
                             text=True, capture_output=True)
        self.assertEqual(run.returncode, 2)
        self.assertIn("error", json.loads(run.stderr))


if __name__ == "__main__":
    unittest.main()
