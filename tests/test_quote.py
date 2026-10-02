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
        self.app.add_product("Z", "Free card", 0)

    def test_merges_sorts_and_prices_lines(self):
        result = self.app.quote([
            {"sku": "Z", "quantity": 2},
            {"sku": "T", "quantity": 1, "note": "ignored"},
            {"sku": " T ", "quantity": 2},
            {"sku": "C", "quantity": 7},
        ])
        self.assertEqual(set(result), {"lines", "total_cents", "can_place"})
        self.assertEqual([line["sku"] for line in result["lines"]], ["C", "T", "Z"])
        self.assertEqual(result["total_cents"], 1700)
        self.assertTrue(result["can_place"])
        tea = result["lines"][1]
        self.assertEqual(set(tea), {"sku", "quantity", "unit_price_cents", "subtotal_cents", "available", "shortfall"})
        self.assertEqual(tea, {"sku": "T", "quantity": 3, "unit_price_cents": 100,
                               "subtotal_cents": 300, "available": None, "shortfall": 0})
        self.assertEqual(result["lines"][2]["subtotal_cents"], 0)

    def test_shortfall_scenario_from_spec(self):
        # On hand 5, reserved 3: merged need 3, available 2, shortfall 1;
        # the amount still counts all 3 units.
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        result = self.app.quote([{"sku": "T", "quantity": 1}, {"sku": "T", "quantity": 2}])
        self.assertEqual(result, {
            "lines": [{"sku": "T", "quantity": 3, "unit_price_cents": 100,
                       "subtotal_cents": 300, "available": 2, "shortfall": 1}],
            "total_cents": 300,
            "can_place": False,
        })

    def test_exact_availability_can_place(self):
        self.app.restock("T", 2)
        result = self.app.quote([{"sku": "T", "quantity": 2}])
        self.assertEqual(result["lines"][0]["available"], 2)
        self.assertEqual(result["lines"][0]["shortfall"], 0)
        self.assertTrue(result["can_place"])

    def test_invalid_inputs_raise_value_error(self):
        before = self.app.path.read_bytes()
        bad_inputs = [
            [], "x", 42, None, {},
            [{"sku": "T"}],
            [{"quantity": 1}],
            ["x"], [42], [None],
            [{"sku": "  ", "quantity": 1}],
            [{"sku": 3, "quantity": 1}],
            [{"sku": "T", "quantity": 0}],
            [{"sku": "T", "quantity": -2}],
            [{"sku": "T", "quantity": 1.5}],
            [{"sku": "T", "quantity": True}],
            [{"sku": "T", "quantity": "1"}],
            [{"sku": "X", "quantity": 1}],
        ]
        for payload in bad_inputs:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    self.app.quote(payload)
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_quote_never_writes_or_creates_files(self):
        self.app.restock("T", 5)
        before = self.app.path.read_bytes()
        self.app.quote([{"sku": "T", "quantity": 1}, {"sku": "C", "quantity": 2}])
        with self.assertRaises(ValueError):
            self.app.quote([{"sku": "X", "quantity": 1}])
        self.assertEqual(self.app.path.read_bytes(), before)
        # A root that never existed is not created by a failed quote.
        fresh = self.root / "fresh"
        with self.assertRaises(ValueError):
            OrderDesk(fresh).quote([{"sku": "N", "quantity": 1}])
        self.assertFalse(fresh.exists())

    def test_quote_does_not_change_business_state(self):
        self.app.restock("T", 5)
        ok = self.app.quote([{"sku": "T", "quantity": 4}])
        self.assertTrue(ok["can_place"])
        short = self.app.quote([{"sku": "T", "quantity": 6}])
        self.assertFalse(short["can_place"])
        self.assertEqual(short["lines"][0]["shortfall"], 1)
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 5, "reserved": 0, "available": 5})
        # Preview does not lock stock: place still checks and reserves at its own time.
        self.app.place("O1", [{"sku": "T", "quantity": 4}])
        self.assertEqual(self.app.stock("T")["reserved"], 4)
        self.assertEqual(self.app.list_orders(), [self.app.get("O1")])

    def test_repeatable_and_stable_after_reopen(self):
        self.app.restock("T", 5)
        first = self.app.quote([{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])
        second = self.app.quote([{"sku": "C", "quantity": 1}, {"sku": "T", "quantity": 2}])
        reopened = OrderDesk(self.root).quote([{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])
        self.assertEqual(first, second)
        self.assertEqual(first, reopened)
        # SKU matching is case sensitive.
        with self.assertRaises(ValueError):
            self.app.quote([{"sku": "t", "quantity": 1}])

    def test_legacy_data_without_inventory_is_unmanaged(self):
        self.app.restock("T", 5)
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        del raw["inventory"]
        raw.pop("reservations", None)
        self.app._write(raw)
        result = OrderDesk(self.root).quote([{"sku": "T", "quantity": 100}])
        line = result["lines"][0]
        self.assertIsNone(line["available"])
        self.assertEqual(line["shortfall"], 0)
        self.assertTrue(result["can_place"])

    def test_cli_quote_success_and_failures(self):
        payload = self.root / "quote.json"
        payload.write_text(json.dumps({"lines": [{"sku": "Z", "quantity": 1}]}), encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "quote", str(payload)],
                            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        value = json.loads(ok.stdout)
        self.assertEqual(value["total_cents"], 0)
        self.assertTrue(value["can_place"])
        # No data file is written for a catalog-only quote (root had no data.json yet
        # is impossible here since products exist, so assert content stays unchanged).
        before = (self.root / "data.json").read_bytes()
        for body in (
            {"lines": [{"sku": "Z", "quantity": 0}]},
            {"lines": [{"sku": "X", "quantity": 1}]},
            {"lines": []},
        ):
            payload.write_text(json.dumps(body), encoding="utf-8")
            failed = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "quote", str(payload)],
                                    text=True, capture_output=True)
            self.assertEqual(failed.returncode, 2)
            self.assertEqual(failed.stdout, "")
            self.assertIn("error", json.loads(failed.stderr))
        missing = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "quote", "nope.json"],
                                 text=True, capture_output=True)
        self.assertEqual(missing.returncode, 2)
        self.assertEqual(missing.stdout, "")
        self.assertIn("error", json.loads(missing.stderr))
        self.assertEqual((self.root / "data.json").read_bytes(), before)

    def test_cli_quote_array_keeps_successful_rows(self):
        payload = self.root / "batch.json"
        payload.write_text(json.dumps([
            {"lines": [{"sku": "C", "quantity": 1}]},
            {"lines": [{"sku": "X", "quantity": 1}]},
        ]), encoding="utf-8")
        failed = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "quote", str(payload)],
                                text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(failed.stdout, "")
        self.assertIn("unknown product", json.loads(failed.stderr)["error"])


if __name__ == "__main__":
    unittest.main()
