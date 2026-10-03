import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk


class QuoteReturnTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)
        self.app.add_product("Z", "Free card", 0)
        self.app.place("O1", [
            {"sku": "T", "quantity": 2},
            {"sku": "T", "quantity": 3},
            {"sku": "C", "quantity": 1},
            {"sku": "Z", "quantity": 4},
        ])
        self.app.ship("O1", "UPS", "TRK1")

    def test_prices_from_deal_and_merges_lines(self):
        result = self.app.quote_return("O1", [
            {"sku": " T ", "quantity": 1, "note": "ignored"},
            {"sku": "T", "quantity": 2},
            {"sku": "C", "quantity": 1},
        ])
        self.assertEqual(set(result), {"order_id", "lines", "total_cents", "can_record"})
        self.assertEqual(result["order_id"], "O1")
        self.assertEqual([line["sku"] for line in result["lines"]], ["C", "T"])
        tea = result["lines"][1]
        self.assertEqual(set(tea), {"sku", "quantity", "unit_price_cents", "subtotal_cents", "remaining"})
        self.assertEqual(tea, {"sku": "T", "quantity": 3, "unit_price_cents": 100,
                               "subtotal_cents": 300, "remaining": 5})
        self.assertEqual(result["lines"][0], {"sku": "C", "quantity": 1, "unit_price_cents": 200,
                                              "subtotal_cents": 200, "remaining": 1})
        self.assertEqual(result["total_cents"], 500)
        self.assertTrue(result["can_record"])

    def test_zero_price_deal_is_valid(self):
        result = self.app.quote_return("O1", [{"sku": "Z", "quantity": 4}])
        self.assertEqual(result["lines"][0]["unit_price_cents"], 0)
        self.assertEqual(result["total_cents"], 0)
        self.assertTrue(result["can_record"])

    def test_remaining_counts_active_and_received_returns(self):
        self.app.restock("T", 10)
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        self.app.receive_return("R1")
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        result = self.app.quote_return("O1", [{"sku": "T", "quantity": 2}])
        self.assertEqual(result["lines"][0]["remaining"], 2)
        self.assertTrue(result["can_record"])
        # Cancelled registrations free their allowance again.
        self.app.cancel_return("R2")
        result = self.app.quote_return("O1", [{"sku": "T", "quantity": 3}])
        self.assertEqual(result["lines"][0]["remaining"], 3)
        self.assertTrue(result["can_record"])

    def test_amended_return_counts_at_latest_quantity(self):
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.amend_return("R1", [{"sku": "T", "quantity": 1}], [{"sku": "T", "quantity": 4}])
        result = self.app.quote_return("O1", [{"sku": "T", "quantity": 1}])
        self.assertEqual(result["lines"][0]["remaining"], 1)
        self.assertTrue(result["can_record"])

    def test_over_quantity_reports_full_amount_without_truncation(self):
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 4}])
        result = self.app.quote_return("O1", [{"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 1}])
        self.assertFalse(result["can_record"])
        tea = result["lines"][1]
        self.assertEqual(tea["quantity"], 3)
        self.assertEqual(tea["subtotal_cents"], 300)
        self.assertEqual(tea["remaining"], 1)
        self.assertEqual(result["total_cents"], 500)

    def test_exact_remaining_can_record(self):
        result = self.app.quote_return("O1", [{"sku": "T", "quantity": 5}])
        self.assertEqual(result["lines"][0]["remaining"], 5)
        self.assertTrue(result["can_record"])

    def test_delivered_order_quotes(self):
        self.app.confirm_delivery("O1", "Alice", "2026-10-01")
        result = self.app.quote_return("O1", [{"sku": "C", "quantity": 1}])
        self.assertTrue(result["can_record"])

    def test_catalog_state_never_blocks_original_order_items(self):
        # Paused sales, missing stock, unmanaged and even removed-from-catalog
        # products still quote from the stored deal prices.
        self.app.set_product_enabled("T", False)
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        del raw["products"]["C"]
        self.app._write(raw)
        app = OrderDesk(self.root)
        result = app.quote_return("O1", [{"sku": "T", "quantity": 1}, {"sku": "C", "quantity": 1}])
        self.assertEqual(result["total_cents"], 300)
        self.assertTrue(result["can_record"])
        # A later reprice does not change the deal price used for quoting.
        self.app.add_product("C", "Coffee", 200)
        self.app.reprice_products([{"sku": "C", "expected_price_cents": 200, "price_cents": 350}])
        result = self.app.quote_return("O1", [{"sku": "C", "quantity": 1}])
        self.assertEqual(result["lines"][0]["unit_price_cents"], 200)

    def test_invalid_inputs_raise_value_error(self):
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        before = self.app.path.read_bytes()
        bad_calls = [
            ("O1", []), ("O1", "x"), ("O1", 42), ("O1", None), ("O1", {}),
            ("O1", [{"sku": "T"}]),
            ("O1", [{"quantity": 1}]),
            ("O1", ["x"]), ("O1", [42]), ("O1", [None]),
            ("O1", [{"sku": "  ", "quantity": 1}]),
            ("O1", [{"sku": 3, "quantity": 1}]),
            ("O1", [{"sku": "T", "quantity": 0}]),
            ("O1", [{"sku": "T", "quantity": -2}]),
            ("O1", [{"sku": "T", "quantity": 1.5}]),
            ("O1", [{"sku": "T", "quantity": True}]),
            ("O1", [{"sku": "T", "quantity": "1"}]),
            ("O1", [{"sku": "X", "quantity": 1}]),
            ("O1", [{"sku": "t", "quantity": 1}]),
            ("NOPE", [{"sku": "T", "quantity": 1}]),
            ("  ", [{"sku": "T", "quantity": 1}]),
            (3, [{"sku": "T", "quantity": 1}]),
        ]
        for order_id, lines in bad_calls:
            with self.subTest(order_id=order_id, lines=lines):
                with self.assertRaises(ValueError):
                    self.app.quote_return(order_id, lines)
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_trimmed_order_id_and_case_sensitive_sku(self):
        result = self.app.quote_return(" O1 ", [{"sku": "T", "quantity": 1}])
        self.assertEqual(result["order_id"], "O1")
        with self.assertRaises(ValueError):
            self.app.quote_return("o1", [{"sku": "T", "quantity": 1}])

    def test_status_must_be_shipped_or_delivered(self):
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.quote_return("O2", [{"sku": "T", "quantity": 1}])
        self.app.cancel("O2")
        with self.assertRaises(ValueError):
            self.app.quote_return("O2", [{"sku": "T", "quantity": 1}])

    def test_stored_price_problems_raise_value_error(self):
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        # Missing price on one deal line of a requested sku.
        del raw["orders"]["O1"]["lines"][0]["unit_price_cents"]
        self.app._write(raw)
        with self.assertRaises(ValueError):
            OrderDesk(self.root).quote_return("O1", [{"sku": "T", "quantity": 1}])
        # An unrequested sku's broken price does not matter.
        result = OrderDesk(self.root).quote_return("O1", [{"sku": "C", "quantity": 1}])
        self.assertEqual(result["total_cents"], 200)

        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        raw["orders"]["O1"]["lines"][0]["unit_price_cents"] = True
        self.app._write(raw)
        with self.assertRaises(ValueError):
            OrderDesk(self.root).quote_return("O1", [{"sku": "T", "quantity": 1}])

        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        raw["orders"]["O1"]["lines"][0]["unit_price_cents"] = -5
        self.app._write(raw)
        with self.assertRaises(ValueError):
            OrderDesk(self.root).quote_return("O1", [{"sku": "T", "quantity": 1}])

        # Same sku stored at two different deal prices.
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        raw["orders"]["O1"]["lines"][0]["unit_price_cents"] = 100
        raw["orders"]["O1"]["lines"][1]["unit_price_cents"] = 120
        self.app._write(raw)
        with self.assertRaises(ValueError):
            OrderDesk(self.root).quote_return("O1", [{"sku": "T", "quantity": 1}])

    def test_quote_return_never_writes_or_creates_files(self):
        before = self.app.path.read_bytes()
        self.app.quote_return("O1", [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.quote_return("O1", [{"sku": "X", "quantity": 1}])
        self.assertEqual(self.app.path.read_bytes(), before)
        fresh = self.root / "fresh"
        with self.assertRaises(ValueError):
            OrderDesk(fresh).quote_return("O1", [{"sku": "T", "quantity": 1}])
        self.assertFalse(fresh.exists())

    def test_preview_does_not_occupy_allowance(self):
        self.app.quote_return("O1", [{"sku": "T", "quantity": 5}])
        # Registration afterwards still validates against the real data.
        record = self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 5}])
        self.assertEqual(record["lines"], [{"sku": "T", "quantity": 5}])
        result = self.app.quote_return("O1", [{"sku": "T", "quantity": 1}])
        self.assertEqual(result["lines"][0]["remaining"], 0)
        self.assertFalse(result["can_record"])

    def test_repeatable_and_stable_after_reopen(self):
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        first = self.app.quote_return("O1", [{"sku": "T", "quantity": 1}, {"sku": "C", "quantity": 1}])
        second = self.app.quote_return("O1", [{"sku": "C", "quantity": 1}, {"sku": "T", "quantity": 1}])
        reopened = OrderDesk(self.root).quote_return("O1", [{"sku": "T", "quantity": 1}, {"sku": "C", "quantity": 1}])
        self.assertEqual(first, second)
        self.assertEqual(first, reopened)

    def test_legacy_data_without_returns_reads_as_nothing_returned(self):
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        raw.pop("returns", None)
        raw.pop("cancelled_returns", None)
        self.app._write(raw)
        result = OrderDesk(self.root).quote_return("O1", [{"sku": "T", "quantity": 5}])
        self.assertEqual(result["lines"][0]["remaining"], 5)
        self.assertTrue(result["can_record"])

    def test_cli_quote_return_success_and_failures(self):
        payload = self.root / "quote-return.json"
        payload.write_text(json.dumps({"order_id": "O1", "lines": [{"sku": "T", "quantity": 2}]}),
                           encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                             "quote-return", str(payload)], text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        value = json.loads(ok.stdout)
        self.assertEqual(value["total_cents"], 200)
        self.assertTrue(value["can_record"])
        before = (self.root / "data.json").read_bytes()
        for body in (
            {"order_id": "O1", "lines": [{"sku": "T", "quantity": 0}]},
            {"order_id": "O1", "lines": [{"sku": "X", "quantity": 1}]},
            {"order_id": "O1", "lines": []},
            {"order_id": "NOPE", "lines": [{"sku": "T", "quantity": 1}]},
        ):
            payload.write_text(json.dumps(body), encoding="utf-8")
            failed = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                                     "quote-return", str(payload)], text=True, capture_output=True)
            self.assertEqual(failed.returncode, 2)
            self.assertEqual(failed.stdout, "")
            self.assertIn("error", json.loads(failed.stderr))
        self.assertEqual((self.root / "data.json").read_bytes(), before)

    def test_cli_quote_return_array_keeps_successful_rows(self):
        payload = self.root / "batch.json"
        payload.write_text(json.dumps([
            {"order_id": "O1", "lines": [{"sku": "C", "quantity": 1}]},
            {"order_id": "O1", "lines": [{"sku": "X", "quantity": 1}]},
        ]), encoding="utf-8")
        failed = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                                 "quote-return", str(payload)], text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(failed.stdout, "")
        self.assertIn("sku not in original order", json.loads(failed.stderr)["error"])


if __name__ == "__main__":
    unittest.main()
