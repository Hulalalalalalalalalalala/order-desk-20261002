import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk

class QuoteAmendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)
        self.app.add_product("Z", "Free card", 0)

    def test_preview_prices_current_catalog_and_keeps_rows(self):
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        result = self.app.quote_amend(" O1 ", [
            {"sku": " C ", "quantity": 1, "note": "ignored"},
            {"sku": "T", "quantity": 2},
            {"sku": "C", "quantity": 1},
            {"sku": "Z", "quantity": 3},
        ])
        self.assertEqual(set(result), {"order_id", "lines", "total_cents", "stock", "can_amend"})
        self.assertEqual(result["order_id"], "O1")
        # Input order and duplicate SKUs are preserved; zero-price rows stay.
        self.assertEqual(result["lines"], [
            {"sku": "C", "quantity": 1, "unit_price_cents": 200, "subtotal_cents": 200},
            {"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200},
            {"sku": "C", "quantity": 1, "unit_price_cents": 200, "subtotal_cents": 200},
            {"sku": "Z", "quantity": 3, "unit_price_cents": 0, "subtotal_cents": 0},
        ])
        self.assertEqual(result["total_cents"], 600)
        # Stock rows merge duplicate SKUs and sort by SKU; unmanaged products
        # read as unlimited with zero reservation and shortfall.
        self.assertEqual(result["stock"], [
            {"sku": "C", "quantity": 2, "own_reserved": 0, "available": None, "shortfall": 0},
            {"sku": "T", "quantity": 2, "own_reserved": 0, "available": None, "shortfall": 0},
            {"sku": "Z", "quantity": 3, "own_reserved": 0, "available": None, "shortfall": 0},
        ])
        self.assertTrue(result["can_amend"])

    def test_preview_uses_catalog_price_at_call_time(self):
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        data["products"]["T"]["price_cents"] = 150
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        result = self.app.quote_amend("O1", [{"sku": "T", "quantity": 2}])
        self.assertEqual(result["lines"][0]["unit_price_cents"], 150)
        self.assertEqual(result["total_cents"], 300)

    def test_stock_shortfall_scenario_from_spec(self):
        # On hand 5, this order holds 3, another holds 1: available is 1, so
        # a merged demand of 4 fits exactly and 5 reports a shortfall of 1.
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        fits = self.app.quote_amend("O1", [{"sku": "T", "quantity": 2}, {"sku": "T", "quantity": 2}])
        self.assertEqual(fits["stock"], [
            {"sku": "T", "quantity": 4, "own_reserved": 3, "available": 1, "shortfall": 0},
        ])
        self.assertTrue(fits["can_amend"])
        short = self.app.quote_amend("O1", [{"sku": "T", "quantity": 5}])
        self.assertEqual(short["stock"], [
            {"sku": "T", "quantity": 5, "own_reserved": 3, "available": 1, "shortfall": 1},
        ])
        self.assertFalse(short["can_amend"])
        # A shortfall still returns the full priced preview.
        self.assertEqual(short["total_cents"], 500)
        self.assertEqual(short["lines"][0]["subtotal_cents"], 500)

    def test_mixed_managed_and_unmanaged_stock_rows(self):
        self.app.restock("T", 2)
        self.app.place("O1", [{"sku": "T", "quantity": 1}, {"sku": "C", "quantity": 1}])
        result = self.app.quote_amend("O1", [{"sku": "C", "quantity": 9}, {"sku": "T", "quantity": 3}])
        self.assertEqual(result["stock"], [
            {"sku": "C", "quantity": 9, "own_reserved": 0, "available": None, "shortfall": 0},
            {"sku": "T", "quantity": 3, "own_reserved": 1, "available": 1, "shortfall": 1},
        ])
        self.assertFalse(result["can_amend"])

    def test_product_managed_after_place_uses_current_stock(self):
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.restock("T", 3)
        result = self.app.quote_amend("O1", [{"sku": "T", "quantity": 3}])
        self.assertEqual(result["stock"], [
            {"sku": "T", "quantity": 3, "own_reserved": 0, "available": 3, "shortfall": 0},
        ])
        self.assertTrue(result["can_amend"])
        short = self.app.quote_amend("O1", [{"sku": "T", "quantity": 4}])
        self.assertEqual(short["stock"][0]["shortfall"], 1)
        self.assertFalse(short["can_amend"])

    def test_legacy_order_without_reservation_reads_as_zero(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        data["orders"]["OLD"] = {"order_id": "OLD", "status": "placed",
                                 "lines": [{"sku": "T", "quantity": 3, "unit_price_cents": 100, "subtotal_cents": 300}],
                                 "total_cents": 300}
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        result = self.app.quote_amend("OLD", [{"sku": "T", "quantity": 3}])
        self.assertEqual(result["stock"], [
            {"sku": "T", "quantity": 3, "own_reserved": 0, "available": 3, "shortfall": 0},
        ])
        self.assertTrue(result["can_amend"])
        short = self.app.quote_amend("OLD", [{"sku": "T", "quantity": 4}])
        self.assertEqual(short["stock"][0]["shortfall"], 1)
        # The preview never backfills reservation or history records.
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("OLD", data.get("reservations", {}))
        self.assertNotIn("OLD", data.get("history", {}))

    def test_paused_product_rules_match_amend(self):
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.set_product_enabled("T", False)
        self.app.set_product_enabled("C", False)
        # Kept, reduced and removed paused products are all fine.
        self.assertEqual(self.app.quote_amend("O1", [{"sku": "T", "quantity": 2}])["total_cents"], 200)
        self.assertEqual(self.app.quote_amend("O1", [{"sku": "T", "quantity": 1}])["total_cents"], 100)
        # A new paused SKU or a grown merged total is rejected.
        with self.assertRaises(ValueError):
            self.app.quote_amend("O1", [{"sku": "C", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.quote_amend("O1", [{"sku": "T", "quantity": 3}])
        with self.assertRaises(ValueError):
            self.app.quote_amend("O1", [{"sku": "T", "quantity": 2}, {"sku": "T", "quantity": 1}])

    def test_validation_errors(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.app.cancel("O1")
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        good = [{"sku": "T", "quantity": 1}]
        for order_id, lines in (
            ("missing", good),            # unknown order
            ("O1", good),                 # not placed
            (None, good), (123, good), ("   ", good),
            ("O2", None), ("O2", "x"), ("O2", []),
            ("O2", ["x"]), ("O2", [{"quantity": 1}]), ("O2", [{"sku": "T"}]),
            ("O2", [{"sku": "  ", "quantity": 1}]),
            ("O2", [{"sku": "T", "quantity": 0}]),
            ("O2", [{"sku": "T", "quantity": -1}]),
            ("O2", [{"sku": "T", "quantity": 1.5}]),
            ("O2", [{"sku": "T", "quantity": True}]),
            ("O2", [{"sku": "T", "quantity": "1"}]),
            ("O2", [{"sku": "X", "quantity": 1}]),
        ):
            with self.assertRaises(ValueError, msg=(order_id, lines)):
                self.app.quote_amend(order_id, lines)

    def test_preview_writes_nothing_and_consumes_no_sequence(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        raw = self.app.path.read_bytes()
        self.app.quote_amend("O1", [{"sku": "T", "quantity": 4}])
        with self.assertRaises(ValueError):
            self.app.quote_amend("O1", [{"sku": "X", "quantity": 1}])
        self.assertEqual(self.app.path.read_bytes(), raw)
        self.assertEqual([e["action"] for e in self.app.history("O1")["events"]], ["place"])
        self.assertEqual(self.app.get("O1")["lines"],
                         [{"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100}])
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 5, "reserved": 1, "available": 4})

    def test_preview_creates_no_root(self):
        empty = self.root / "empty"
        app = OrderDesk(empty)
        with self.assertRaises(ValueError):
            app.quote_amend("ghost", [{"sku": "T", "quantity": 1}])
        self.assertFalse(empty.exists())

    def test_preview_locks_nothing_amend_revalidates(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        # Preview says 4 fits; nothing is reserved by looking at it.
        self.assertTrue(self.app.quote_amend("O1", [{"sku": "T", "quantity": 4}])["can_amend"])
        self.app.place("O3", [{"sku": "T", "quantity": 1}])
        # Stock moved after the preview: amend validates at submission time.
        with self.assertRaises(ValueError):
            self.app.amend("O1", [{"sku": "T", "quantity": 4}])
        self.assertEqual(self.app.get("O1")["lines"][0]["quantity"], 3)
        # A previewed shortfall does not block a later valid amend either.
        self.assertFalse(self.app.quote_amend("O2", [{"sku": "T", "quantity": 9}])["can_amend"])
        amended = self.app.amend("O2", [{"sku": "T", "quantity": 1}])
        self.assertEqual(amended["total_cents"], 100)

    def test_quote_and_amend_unaffected_by_preview(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.quote_amend("O1", [{"sku": "T", "quantity": 1}])
        quote = self.app.quote([{"sku": "T", "quantity": 2}])
        self.assertTrue(quote["can_place"])
        amended = self.app.amend("O1", [{"sku": "T", "quantity": 2}])
        self.assertEqual(amended["total_cents"], 200)
        self.assertEqual(self.app.stock("T")["reserved"], 2)

    def test_cli_quote_amend_success_and_failure(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        payload = self.root / "q.json"
        payload.write_text(json.dumps({"order_id": " O1 ", "lines": [{"sku": "T", "quantity": 4}]}),
                           encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "quote-amend", str(payload)],
                            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual(result["order_id"], "O1")
        self.assertTrue(result["can_amend"])
        self.assertEqual(result["total_cents"], 400)
        payload.write_text(json.dumps({"order_id": "O1", "lines": [{"sku": "X", "quantity": 1}]}),
                           encoding="utf-8")
        failed = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "quote-amend", str(payload)],
                                text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(failed.stdout, "")
        self.assertIn("error", json.loads(failed.stderr))
        # Neither run wrote anything.
        self.assertEqual(OrderDesk(self.root).get("O1")["total_cents"], 300)

    def test_cli_quote_amend_array_processes_entries_independently(self):
        self.app.restock("T", 5)
        self.app.place("A", [{"sku": "T", "quantity": 1}])
        self.app.place("B", [{"sku": "T", "quantity": 1}])
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([
            {"order_id": "A", "lines": [{"sku": "T", "quantity": 2}]},
            {"order_id": "B", "lines": [{"sku": "T", "quantity": 9}]},
        ]), encoding="utf-8")
        run = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "quote-amend", str(batch)],
                             text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        results = json.loads(run.stdout)
        self.assertEqual([r["order_id"] for r in results], ["A", "B"])
        self.assertTrue(results[0]["can_amend"])
        self.assertFalse(results[1]["can_amend"])
        bad = self.root / "bad.json"
        bad.write_text(json.dumps([
            {"order_id": "A", "lines": [{"sku": "T", "quantity": 2}]},
            {"order_id": "missing", "lines": [{"sku": "T", "quantity": 1}]},
        ]), encoding="utf-8")
        failed = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "quote-amend", str(bad)],
                                text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(failed.stdout, "")

if __name__ == "__main__":
    unittest.main()
