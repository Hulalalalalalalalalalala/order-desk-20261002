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

    def test_result_structure_and_normalization(self):
        self.app.restock("T", 10)
        self.app.restock("C", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        result = self.app.quote_amend(" O1 ", [
            {"sku": " C ", "quantity": 1, "note": "ignored"},
            {"sku": "T", "quantity": 3},
            {"sku": "C", "quantity": 1},
            {"sku": "Z", "quantity": 2},
        ])
        self.assertEqual(set(result), {"order_id", "lines", "total_cents", "stock", "can_amend"})
        self.assertEqual(result["order_id"], "O1")
        # Input order and duplicate SKUs are preserved; priced at the catalog.
        self.assertEqual(result["lines"], [
            {"sku": "C", "quantity": 1, "unit_price_cents": 200, "subtotal_cents": 200},
            {"sku": "T", "quantity": 3, "unit_price_cents": 100, "subtotal_cents": 300},
            {"sku": "C", "quantity": 1, "unit_price_cents": 200, "subtotal_cents": 200},
            {"sku": "Z", "quantity": 2, "unit_price_cents": 0, "subtotal_cents": 0},
        ])
        self.assertEqual(result["total_cents"], 700)
        # Stock merges duplicate SKUs and sorts by sku.
        self.assertEqual([row["sku"] for row in result["stock"]], ["C", "T", "Z"])
        for row in result["stock"]:
            self.assertEqual(set(row), {"sku", "quantity", "own_reserved", "available", "shortfall"})
        self.assertEqual(result["stock"][0], {
            "sku": "C", "quantity": 2, "own_reserved": 0, "available": 10, "shortfall": 0})
        self.assertEqual(result["stock"][1], {
            "sku": "T", "quantity": 3, "own_reserved": 2, "available": 8, "shortfall": 0})
        # Unmanaged product: available null, reservation and shortfall zero.
        self.assertEqual(result["stock"][2], {
            "sku": "Z", "quantity": 2, "own_reserved": 0, "available": None, "shortfall": 0})
        self.assertTrue(result["can_amend"])

    def test_shortfall_scenario_from_spec(self):
        # On hand 5, this order holds 3, another order holds 1: available 1.
        # New demand 4 fits (1 + 3), demand 5 reports a shortfall of 1.
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        ok = self.app.quote_amend("O1", [{"sku": "T", "quantity": 2}, {"sku": "T", "quantity": 2}])
        self.assertEqual(ok["stock"], [{
            "sku": "T", "quantity": 4, "own_reserved": 3, "available": 1, "shortfall": 0}])
        self.assertTrue(ok["can_amend"])
        short = self.app.quote_amend("O1", [{"sku": "T", "quantity": 5}])
        self.assertEqual(short["stock"], [{
            "sku": "T", "quantity": 5, "own_reserved": 3, "available": 1, "shortfall": 1}])
        self.assertFalse(short["can_amend"])
        # Shortfall still returns the full priced result.
        self.assertEqual(short["total_cents"], 500)

    def test_unmanaged_products_stay_unlimited(self):
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        result = self.app.quote_amend("O1", [{"sku": "T", "quantity": 99}, {"sku": "C", "quantity": 50}])
        self.assertTrue(result["can_amend"])
        self.assertEqual(result["total_cents"], 99 * 100 + 50 * 200)
        self.assertEqual(result["stock"], [
            {"sku": "C", "quantity": 50, "own_reserved": 0, "available": None, "shortfall": 0},
            {"sku": "T", "quantity": 99, "own_reserved": 0, "available": None, "shortfall": 0},
        ])

    def test_product_managed_after_place_uses_current_stock(self):
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.restock("T", 3)
        ok = self.app.quote_amend("O1", [{"sku": "T", "quantity": 3}])
        self.assertEqual(ok["stock"], [{
            "sku": "T", "quantity": 3, "own_reserved": 0, "available": 3, "shortfall": 0}])
        self.assertTrue(ok["can_amend"])
        short = self.app.quote_amend("O1", [{"sku": "T", "quantity": 4}])
        self.assertEqual(short["stock"][0]["shortfall"], 1)
        self.assertFalse(short["can_amend"])

    def test_legacy_order_without_reservation_gets_no_allowance(self):
        self.app.restock("T", 5)
        self.app.place("O2", [{"sku": "T", "quantity": 2}])
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        data["orders"]["OLD"] = {"order_id": "OLD", "status": "placed",
                                 "lines": [{"sku": "T", "quantity": 3, "unit_price_cents": 100, "subtotal_cents": 300}],
                                 "total_cents": 300}
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        result = self.app.quote_amend("OLD", [{"sku": "T", "quantity": 4}])
        self.assertEqual(result["stock"], [{
            "sku": "T", "quantity": 4, "own_reserved": 0, "available": 3, "shortfall": 1}])
        self.assertFalse(result["can_amend"])

    def test_paused_product_rules_match_amend(self):
        self.app.place("O1", [{"sku": "T", "quantity": 2}, {"sku": "T", "quantity": 1}])
        self.app.set_product_enabled("T", False)
        self.app.set_product_enabled("C", False)
        # Keep, rearrange and reduce a paused sku already in the order.
        ok = self.app.quote_amend("O1", [{"sku": "T", "quantity": 3}])
        self.assertTrue(ok["can_amend"])
        ok = self.app.quote_amend("O1", [{"sku": "T", "quantity": 1}])
        self.assertTrue(ok["can_amend"])
        # Growing the merged total of a paused sku is rejected.
        with self.assertRaises(ValueError):
            self.app.quote_amend("O1", [{"sku": "T", "quantity": 4}])
        # Adding a paused sku that is not in the order is rejected.
        with self.assertRaises(ValueError):
            self.app.quote_amend("O1", [{"sku": "T", "quantity": 1}, {"sku": "C", "quantity": 1}])

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
            ("O2", None), ("O2", "x"), ("O2", []), ("O2", {}),
            ("O2", ["x"]), ("O2", [42]), ("O2", [{"quantity": 1}]), ("O2", [{"sku": "T"}]),
            ("O2", [{"sku": "  ", "quantity": 1}]),
            ("O2", [{"sku": 3, "quantity": 1}]),
            ("O2", [{"sku": "T", "quantity": 0}]),
            ("O2", [{"sku": "T", "quantity": -1}]),
            ("O2", [{"sku": "T", "quantity": 1.5}]),
            ("O2", [{"sku": "T", "quantity": True}]),
            ("O2", [{"sku": "T", "quantity": "1"}]),
            ("O2", [{"sku": "X", "quantity": 1}]),
            ("O2", [{"sku": "t", "quantity": 1}]),   # case sensitive
        ):
            with self.assertRaises(ValueError, msg=(order_id, lines)):
                self.app.quote_amend(order_id, lines)

    def test_preview_never_writes_or_creates_files(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        before = self.app.path.read_bytes()
        self.app.quote_amend("O1", [{"sku": "T", "quantity": 4}])
        self.app.quote_amend("O1", [{"sku": "T", "quantity": 99}])
        with self.assertRaises(ValueError):
            self.app.quote_amend("O1", [{"sku": "X", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.quote_amend("ghost", [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.path.read_bytes(), before)
        # No history events and no reservations are added by a preview.
        self.assertEqual([e["action"] for e in self.app.history("O1")["events"]], ["place"])
        # A root that never existed is not created by a failed preview.
        fresh = self.root / "fresh"
        with self.assertRaises(ValueError):
            OrderDesk(fresh).quote_amend("ghost", [{"sku": "T", "quantity": 1}])
        self.assertFalse(fresh.exists())

    def test_preview_does_not_lock_stock_or_price(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        ok = self.app.quote_amend("O1", [{"sku": "T", "quantity": 5}])
        self.assertTrue(ok["can_amend"])
        # Preview reserved nothing.
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 5, "reserved": 1, "available": 4})
        # Another order takes the stock; amend re-validates at submission.
        self.app.place("O2", [{"sku": "T", "quantity": 4}])
        with self.assertRaises(ValueError):
            self.app.amend("O1", [{"sku": "T", "quantity": 5}])
        # A smaller amendment still goes through at the current catalog price.
        amended = self.app.amend("O1", [{"sku": "T", "quantity": 1}])
        self.assertEqual(amended["total_cents"], 100)

    def test_repeatable_and_stable_after_reopen(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        first = self.app.quote_amend("O1", [{"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 1}])
        second = self.app.quote_amend("O1", [{"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 1}])
        reopened = OrderDesk(self.root).quote_amend("O1", [{"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 1}])
        self.assertEqual(first, second)
        self.assertEqual(first, reopened)

    def test_quote_and_amend_unaffected(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        preview = self.app.quote_amend("O1", [{"sku": "T", "quantity": 1}])
        self.assertTrue(preview["can_amend"])
        quote = self.app.quote([{"sku": "T", "quantity": 2}])
        self.assertTrue(quote["can_place"])
        amended = self.app.amend("O1", [{"sku": "T", "quantity": 1}])
        self.assertEqual(amended["total_cents"], 100)
        self.assertEqual(self.app.stock("T")["reserved"], 1)

    def test_cli_quote_amend_success_and_failures(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        payload = self.root / "qa.json"
        payload.write_text(json.dumps({"order_id": " O1 ", "lines": [{"sku": "T", "quantity": 2}]}),
                           encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "quote-amend", str(payload)],
                            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        value = json.loads(ok.stdout)
        self.assertEqual(value["order_id"], "O1")
        self.assertEqual(value["total_cents"], 200)
        self.assertTrue(value["can_amend"])
        before = (self.root / "data.json").read_bytes()
        for body in (
            {"order_id": "O1", "lines": [{"sku": "T", "quantity": 0}]},
            {"order_id": "O1", "lines": [{"sku": "X", "quantity": 1}]},
            {"order_id": "missing", "lines": [{"sku": "T", "quantity": 1}]},
            {"order_id": "O1", "lines": []},
        ):
            payload.write_text(json.dumps(body), encoding="utf-8")
            failed = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "quote-amend", str(payload)],
                                    text=True, capture_output=True)
            self.assertEqual(failed.returncode, 2)
            self.assertEqual(failed.stdout, "")
            self.assertIn("error", json.loads(failed.stderr))
        missing = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "quote-amend", "nope.json"],
                                 text=True, capture_output=True)
        self.assertEqual(missing.returncode, 2)
        self.assertEqual(missing.stdout, "")
        self.assertIn("error", json.loads(missing.stderr))
        self.assertEqual((self.root / "data.json").read_bytes(), before)

    def test_cli_quote_amend_array_processes_rows_independently(self):
        self.app.restock("T", 10)
        self.app.place("A", [{"sku": "T", "quantity": 1}])
        self.app.place("B", [{"sku": "T", "quantity": 1}])
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([
            {"order_id": "A", "lines": [{"sku": "T", "quantity": 2}]},
            {"order_id": "missing", "lines": [{"sku": "T", "quantity": 1}]},
        ]), encoding="utf-8")
        run = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "quote-amend", str(batch)],
                             text=True, capture_output=True)
        self.assertEqual(run.returncode, 2)
        self.assertEqual(run.stdout, "")
        self.assertIn("unknown order", json.loads(run.stderr)["error"])
        # Previews never write, even for the successful rows before the error.
        app = OrderDesk(self.root)
        self.assertEqual([e["action"] for e in app.history("A")["events"]], ["place"])
        self.assertEqual(app.get("A")["lines"][0]["quantity"], 1)
        ok_batch = self.root / "ok.json"
        ok_batch.write_text(json.dumps([
            {"order_id": "A", "lines": [{"sku": "T", "quantity": 2}]},
            {"order_id": "B", "lines": [{"sku": "T", "quantity": 3}]},
        ]), encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "quote-amend", str(ok_batch)],
                            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        values = json.loads(ok.stdout)
        self.assertEqual([v["order_id"] for v in values], ["A", "B"])
        self.assertEqual([v["total_cents"] for v in values], [200, 300])


if __name__ == "__main__":
    unittest.main()
