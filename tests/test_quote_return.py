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
        self.app.add_product("U", "Unmanaged", 0)

    def _shipped(self, order_id, lines, stock=None, status="shipped"):
        for sku, quantity in (stock or {"T": 10, "C": 10}).items():
            self.app.restock(sku, quantity)
        self.app.place(order_id, lines)
        self.app.ship(order_id, "DHL", "TRK-" + order_id)
        if status == "delivered":
            self.app.confirm_delivery(order_id, "Sam", "2026-10-01")

    def test_prices_at_deal_price_merges_sorts_and_covers_only_request(self):
        self._shipped("O1", [
            {"sku": "T", "quantity": 5},
            {"sku": "C", "quantity": 2},
            {"sku": "U", "quantity": 4},
        ], stock={"T": 10, "C": 10})
        result = self.app.quote_return("O1", [
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 1, "note": "ignored"},
            {"sku": " T ", "quantity": 2},
        ])
        self.assertEqual(set(result), {"order_id", "lines", "total_cents", "can_record"})
        self.assertEqual(result["order_id"], "O1")
        self.assertEqual([line["sku"] for line in result["lines"]], ["C", "T"])
        self.assertEqual(result["total_cents"], 500)
        self.assertTrue(result["can_record"])
        tea = result["lines"][1]
        self.assertEqual(set(tea), {"sku", "quantity", "unit_price_cents", "subtotal_cents", "remaining"})
        self.assertEqual(tea, {"sku": "T", "quantity": 3, "unit_price_cents": 100,
                               "subtotal_cents": 300, "remaining": 5})
        self.assertEqual(result["lines"][0], {"sku": "C", "quantity": 1, "unit_price_cents": 200,
                                              "subtotal_cents": 200, "remaining": 2})

    def test_uses_saved_deal_price_ignoring_catalog_repricing(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        self.app.reprice_products([{"sku": "T", "expected_price_cents": 100, "price_cents": 150}])
        result = self.app.quote_return("O1", [{"sku": "T", "quantity": 2}])
        self.assertEqual(result["lines"][0]["unit_price_cents"], 100)
        self.assertEqual(result["total_cents"], 200)

    def test_zero_deal_price_is_valid(self):
        self._shipped("O1", [{"sku": "U", "quantity": 3}], stock={"T": 10})
        result = self.app.quote_return("O1", [{"sku": "U", "quantity": 3}])
        self.assertEqual(result["lines"], [{
            "sku": "U", "quantity": 3, "unit_price_cents": 0,
            "subtotal_cents": 0, "remaining": 3,
        }])
        self.assertEqual(result["total_cents"], 0)
        self.assertTrue(result["can_record"])

    def test_remaining_counts_active_and_received_not_cancelled_and_uses_amended_quantity(self):
        self._shipped("O1", [{"sku": "T", "quantity": 6}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R3", [{"sku": "T", "quantity": 1}])
        self.app.receive_return("R3")
        # Active R1 (3) + R2 (1) + received R3 (1) occupy 5 of 6.
        self.assertEqual(self.app.quote_return("O1", [{"sku": "T", "quantity": 1}])["lines"][0]["remaining"], 1)
        # Amending R1 down to 1 frees 2: occupied is now R1(1)+R2(1)+R3(1)=3.
        self.app.amend_return("R1", [{"sku": "T", "quantity": 3}], [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.quote_return("O1", [{"sku": "T", "quantity": 1}])["lines"][0]["remaining"], 3)
        # Cancelling R2 frees its allowance.
        self.app.cancel_return("R2")
        self.assertEqual(self.app.quote_return("O1", [{"sku": "T", "quantity": 1}])["lines"][0]["remaining"], 4)

    def test_over_quota_returns_full_amount_with_can_record_false(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        result = self.app.quote_return("O1", [
            {"sku": "T", "quantity": 4},
            {"sku": "C", "quantity": 2},
        ])
        self.assertFalse(result["can_record"])
        self.assertEqual(result["total_cents"], 800)
        self.assertEqual(result["lines"], [
            {"sku": "C", "quantity": 2, "unit_price_cents": 200, "subtotal_cents": 400, "remaining": 2},
            {"sku": "T", "quantity": 4, "unit_price_cents": 100, "subtotal_cents": 400, "remaining": 2},
        ])

    def test_within_quota_after_partial_return_can_record_true(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        result = self.app.quote_return("O1", [{"sku": "T", "quantity": 2}])
        self.assertTrue(result["can_record"])

    def test_delivered_order_supported_other_statuses_rejected(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}], status="delivered")
        result = self.app.quote_return("O1", [{"sku": "T", "quantity": 1}])
        self.assertTrue(result["can_record"])
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.quote_return("O2", [{"sku": "T", "quantity": 1}])
        self.app.cancel("O2")
        with self.assertRaises(ValueError):
            self.app.quote_return("O2", [{"sku": "T", "quantity": 1}])

    def test_paused_unmanaged_and_uncatalogued_products_do_not_block(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}, {"sku": "U", "quantity": 2}], stock={"T": 10})
        self.app.set_product_enabled("T", False)
        result = self.app.quote_return("O1", [{"sku": "T", "quantity": 2}, {"sku": "U", "quantity": 2}])
        self.assertTrue(result["can_record"])
        # A product dropped from the catalog after ordering still previews at
        # its saved deal price.
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        del data["products"]["U"]
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        result = OrderDesk(self.root).quote_return("O1", [{"sku": "U", "quantity": 2}])
        self.assertEqual(result["lines"][0]["unit_price_cents"], 0)
        self.assertEqual(result["total_cents"], 0)

    def test_unknown_order_and_invalid_identifiers_rejected(self):
        for bad_id in (None, 123, 1.5, b"O1", ["O1"], {"x": 1}, "   ", "\t\n", True):
            with self.assertRaises(ValueError):
                self.app.quote_return(bad_id, [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.quote_return("NOPE", [{"sku": "T", "quantity": 1}])

    def test_invalid_lines_shapes_rejected(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
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
        ]
        before = self.app.path.read_bytes()
        for payload in bad_inputs:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    self.app.quote_return("O1", payload)
        self.assertEqual(self.app.path.read_bytes(), before)
        # Case-sensitive sku matching and sku outside the original order.
        with self.assertRaises(ValueError):
            self.app.quote_return("O1", [{"sku": "t", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.quote_return("O1", [{"sku": "C", "quantity": 1}])

    def test_invalid_deal_prices_rejected_without_catalog_substitution(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])

        def raw_order(lines):
            data = json.loads(self.app.path.read_text(encoding="utf-8"))
            data["orders"]["O1"]["lines"] = lines
            self.app.path.write_text(json.dumps(data), encoding="utf-8")

        cases = [
            # Missing deal price, even though the catalog still knows T at 100.
            [{"sku": "T", "quantity": 2, "subtotal_cents": 200}],
            # Boolean deal price.
            [{"sku": "T", "quantity": 2, "unit_price_cents": True, "subtotal_cents": 2}],
            # String deal price.
            [{"sku": "T", "quantity": 2, "unit_price_cents": "100", "subtotal_cents": 200}],
            # Negative deal price.
            [{"sku": "T", "quantity": 2, "unit_price_cents": -1, "subtotal_cents": -2}],
            # Same sku across rows with inconsistent prices.
            [{"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100},
             {"sku": "T", "quantity": 1, "unit_price_cents": 120, "subtotal_cents": 120}],
        ]
        for lines in cases:
            raw_order(lines)
            with self.subTest(lines=lines):
                with self.assertRaises(ValueError):
                    OrderDesk(self.root).quote_return("O1", [{"sku": "T", "quantity": 1}])

    def test_never_writes_or_creates_files_or_consumes_quota_or_history(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        before = self.app.path.read_bytes()
        self.app.quote_return("O1", [{"sku": "T", "quantity": 99}])
        with self.assertRaises(ValueError):
            self.app.quote_return("O1", [{"sku": "C", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.quote_return("NOPE", [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.path.read_bytes(), before)
        # No order event was appended.
        self.assertEqual([e["action"] for e in self.app.history("O1")["events"]], ["place", "ship"])
        # The preview occupied no allowance: the full quantity can still be
        # registered afterwards.
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 5}])
        self.assertEqual(self.app.get_returns("O1")["remaining"], [{"sku": "T", "quantity": 0}])
        # A root that never existed is not created by a failed preview.
        fresh = self.root / "fresh"
        with self.assertRaises(ValueError):
            OrderDesk(fresh).quote_return("O1", [{"sku": "T", "quantity": 1}])
        self.assertFalse(fresh.exists())

    def test_legacy_data_without_returns_treated_as_none_returned(self):
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "orders": {"OLD": {
                "order_id": "OLD", "status": "shipped",
                "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200}],
                "total_cents": 200,
                "shipment": {"carrier": "DHL", "tracking_no": "Z"},
            }},
        }
        legacy_root = Path(self.temp.name) / "legacy"
        legacy_root.mkdir()
        OrderDesk(legacy_root).path.write_text(json.dumps(data), encoding="utf-8")
        result = OrderDesk(legacy_root).quote_return("OLD", [{"sku": "T", "quantity": 2}])
        self.assertEqual(result, {
            "order_id": "OLD",
            "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100,
                       "subtotal_cents": 200, "remaining": 2}],
            "total_cents": 200,
            "can_record": True,
        })

    def test_repeatable_and_stable_after_reopen(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        payload = [{"sku": "C", "quantity": 1}, {"sku": "T", "quantity": 2}]
        first = self.app.quote_return("O1", payload)
        second = self.app.quote_return("O1", list(reversed(payload)))
        reopened = OrderDesk(self.root).quote_return("O1", payload)
        self.assertEqual(first, second)
        self.assertEqual(first, reopened)

    def test_does_not_change_stock_or_order(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        order_before = self.app.get("O1")
        stock_before = self.app.stock("T")
        self.app.quote_return("O1", [{"sku": "T", "quantity": 2}])
        self.assertEqual(self.app.get("O1"), order_before)
        self.assertEqual(self.app.stock("T"), stock_before)

    def test_cli_quote_return_success_and_failures(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        payload = self.root / "quote-return.json"
        payload.write_text(json.dumps({"order_id": "O1", "lines": [{"sku": "T", "quantity": 1}]}),
                           encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "quote-return", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        value = json.loads(ok.stdout)
        self.assertEqual(value["total_cents"], 100)
        self.assertTrue(value["can_record"])
        before = (self.root / "data.json").read_bytes()
        for body in (
            {"order_id": "O1", "lines": [{"sku": "T", "quantity": 0}]},
            {"order_id": "O1", "lines": [{"sku": "C", "quantity": 1}]},
            {"order_id": "O1", "lines": []},
            {"order_id": "NOPE", "lines": [{"sku": "T", "quantity": 1}]},
        ):
            payload.write_text(json.dumps(body), encoding="utf-8")
            failed = subprocess.run(
                [sys.executable, "-m", "order_desk", "--root", str(self.root), "quote-return", str(payload)],
                text=True, capture_output=True)
            self.assertEqual(failed.returncode, 2)
            self.assertEqual(failed.stdout, "")
            self.assertIn("error", json.loads(failed.stderr))
        self.assertEqual((self.root / "data.json").read_bytes(), before)

    def test_cli_array_semantics(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        batch = self.root / "batch.json"
        # Both entries succeed: the outer array prints both results in order.
        batch.write_text(json.dumps([
            {"order_id": "O1", "lines": [{"sku": "T", "quantity": 1}]},
            {"order_id": "O1", "lines": [{"sku": "T", "quantity": 2}]},
        ]), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "quote-return", str(batch)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        values = json.loads(ok.stdout)
        self.assertEqual([v["total_cents"] for v in values], [100, 200])
        # Entries run independently; the failing one makes the command fail.
        batch.write_text(json.dumps([
            {"order_id": "O1", "lines": [{"sku": "T", "quantity": 1}]},
            {"order_id": "NOPE", "lines": [{"sku": "T", "quantity": 1}]},
        ]), encoding="utf-8")
        failed = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "quote-return", str(batch)],
            text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(failed.stdout, "")
        self.assertIn("unknown order", json.loads(failed.stderr)["error"])


if __name__ == "__main__":
    unittest.main()
