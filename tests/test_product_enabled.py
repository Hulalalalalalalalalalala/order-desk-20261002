import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk


class ProductEnabledTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)

    def test_new_product_enabled_by_default_without_stored_field(self):
        self.assertEqual(self.app.get_product("T"),
                         {"sku": "T", "name": "Tea", "price_cents": 100, "enabled": True})
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("enabled", raw["products"]["T"])
        # add_product's return shape is unchanged.
        self.assertEqual(self.app.add_product("C", "Coffee", 200),
                         {"sku": "C", "name": "Coffee", "price_cents": 200})

    def test_pause_and_resume_persist_and_repeatable(self):
        paused = self.app.set_product_enabled(" T ", False)
        self.assertEqual(paused, {"sku": "T", "name": "Tea", "price_cents": 100, "enabled": False})
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertIs(raw["products"]["T"]["enabled"], False)
        self.assertEqual(OrderDesk(self.root).get_product("T")["enabled"], False)
        # Repeating the same state still succeeds.
        self.assertFalse(self.app.set_product_enabled("T", False)["enabled"])
        resumed = self.app.set_product_enabled("T", True)
        self.assertTrue(resumed["enabled"])
        self.assertTrue(OrderDesk(self.root).get_product("T")["enabled"])
        self.assertTrue(self.app.set_product_enabled("T", True)["enabled"])

    def test_legacy_product_without_enabled_is_enabled_and_not_rewritten(self):
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("enabled", raw["products"]["T"])
        before = self.app.path.read_bytes()
        self.assertTrue(OrderDesk(self.root).get_product("T")["enabled"])
        # A query never fills the field in.
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertNotIn("enabled", json.loads(self.app.path.read_text())["products"]["T"])

    def test_invalid_sku_and_enabled_raise(self):
        for sku in (None, 123, True, "   ", ""):
            with self.subTest(sku=sku):
                with self.assertRaises(ValueError):
                    self.app.set_product_enabled(sku, False)
                with self.assertRaises(ValueError):
                    self.app.get_product(sku)
        with self.assertRaises(ValueError):
            self.app.set_product_enabled("X", False)
        with self.assertRaises(ValueError):
            self.app.get_product("X")
        # SKU matching is case sensitive after trimming.
        with self.assertRaises(ValueError):
            self.app.set_product_enabled("t", False)
        for enabled in (0, 1, "true", "false", None, "yes"):
            with self.subTest(enabled=enabled):
                with self.assertRaises(ValueError):
                    self.app.set_product_enabled("T", enabled)

    def test_failures_do_not_write_or_create_files(self):
        before = self.app.path.read_bytes()
        for kwargs in ({"sku": "X", "enabled": False}, {"sku": "T", "enabled": 1}):
            with self.assertRaises(ValueError):
                self.app.set_product_enabled(**kwargs)
        with self.assertRaises(ValueError):
            self.app.get_product("X")
        self.assertEqual(self.app.path.read_bytes(), before)
        fresh = self.root / "fresh"
        with self.assertRaises(ValueError):
            OrderDesk(fresh).set_product_enabled("T", False)
        with self.assertRaises(ValueError):
            OrderDesk(fresh).get_product("T")
        self.assertFalse(fresh.exists())

    def test_toggling_leaves_everything_else_untouched(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.set_product_enabled("T", False)
        product = json.loads(self.app.path.read_text())["products"]["T"]
        self.assertEqual((product["name"], product["price_cents"]), ("Tea", 100))
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 5, "reserved": 2, "available": 3})
        self.assertEqual(self.app.get("O1")["total_cents"], 200)
        self.assertEqual([e["action"] for e in self.app.history("O1")["events"]], ["place"])
        self.app.set_product_enabled("T", True)
        self.assertEqual([e["action"] for e in self.app.history("O1")["events"]], ["place"])


class PausedSalesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)

    def test_quote_rejects_paused_product_entirely(self):
        self.app.restock("C", 5)
        self.app.set_product_enabled("T", False)
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.quote([{"sku": "C", "quantity": 1}, {"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.path.read_bytes(), before)
        # Unmanaged paused products are rejected too.
        with self.assertRaises(ValueError):
            self.app.quote([{"sku": "T", "quantity": 1}])

    def test_place_rejects_paused_product_entirely(self):
        self.app.restock("C", 5)
        self.app.set_product_enabled("T", False)
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.place("O1", [{"sku": "C", "quantity": 1}, {"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.stock("C")["reserved"], 0)
        self.assertEqual(self.app.list_orders(), [])
        with self.assertRaises(ValueError):
            self.app.get("O1")

    def test_resume_restores_quote_and_place(self):
        self.app.restock("T", 2)
        self.app.set_product_enabled("T", False)
        with self.assertRaises(ValueError):
            self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.app.set_product_enabled("T", True)
        quote = self.app.quote([{"sku": "T", "quantity": 2}])
        self.assertTrue(quote["can_place"])
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.assertEqual(self.app.stock("T")["reserved"], 2)

    def test_amend_keep_reduce_remove_paused_allowed(self):
        self.app.restock("T", 5)
        self.app.restock("C", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 2}])
        self.app.set_product_enabled("T", False)
        # Keep the same merged quantity while rearranging lines.
        kept = self.app.amend("O1", [
            {"sku": "T", "quantity": 1},
            {"sku": "T", "quantity": 2},
            {"sku": "C", "quantity": 2},
        ])
        self.assertEqual([line["quantity"] for line in kept["lines"] if line["sku"] == "T"], [1, 2])
        # Reduce.
        reduced = self.app.amend("O1", [{"sku": "T", "quantity": 1}, {"sku": "C", "quantity": 2}])
        self.assertEqual(self.app.stock("T")["reserved"], 1)
        # Remove.
        removed = self.app.amend("O1", [{"sku": "C", "quantity": 2}])
        self.assertEqual(self.app.stock("T")["reserved"], 0)
        self.assertEqual(removed["total_cents"], 400)

    def test_amend_cannot_add_or_increase_paused_product(self):
        self.app.restock("T", 5)
        self.app.restock("C", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.set_product_enabled("C", False)
        # A paused SKU absent from the original list cannot be added.
        with self.assertRaises(ValueError):
            self.app.amend("O1", [{"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 1}])
        self.app.set_product_enabled("C", True)
        self.app.set_product_enabled("T", False)
        # Two lines merging to 3 reproduce the original total of 3: OK; 4 is rejected.
        ok = self.app.amend("O1", [{"sku": "T", "quantity": 2}, {"sku": "T", "quantity": 1}])
        self.assertEqual(sum(l["quantity"] for l in ok["lines"]), 3)
        with self.assertRaises(ValueError):
            self.app.amend("O1", [{"sku": "T", "quantity": 2}, {"sku": "T", "quantity": 2}])
        # Each line below the old total but merged above it is rejected too.
        with self.assertRaises(ValueError):
            self.app.amend("O1", [{"sku": "T", "quantity": 2}, {"sku": "T", "quantity": 2}])
        # Reduce to 1 first; growing back to 2 is then forbidden.
        self.app.amend("O1", [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.amend("O1", [{"sku": "T", "quantity": 2}])
        self.assertEqual(self.app.get("O1")["lines"][0]["quantity"], 1)

    def test_amend_uses_current_ordered_quantity_not_reservation(self):
        # Unmanaged product: no reservation record exists, but the current
        # ordered quantity still governs paused-amend comparisons.
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.set_product_enabled("T", False)
        amended = self.app.amend("O1", [{"sku": "T", "quantity": 3}])
        self.assertEqual(amended["total_cents"], 300)
        with self.assertRaises(ValueError):
            self.app.amend("O1", [{"sku": "T", "quantity": 4}])

    def test_failed_paused_amend_changes_nothing(self):
        self.app.restock("T", 5)
        self.app.restock("C", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 2}])
        self.app.set_product_enabled("C", False)
        raw = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.amend("O1", [{"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 3}])
        self.assertEqual(self.app.path.read_bytes(), raw)
        self.assertEqual(self.app.stock("C")["reserved"], 2)
        self.assertEqual([e["action"] for e in self.app.history("O1")["events"]], ["place"])

    def test_resume_allows_amend_add_and_increase(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.app.set_product_enabled("T", False)
        with self.assertRaises(ValueError):
            self.app.amend("O1", [{"sku": "T", "quantity": 2}])
        self.app.set_product_enabled("T", True)
        amended = self.app.amend("O1", [{"sku": "T", "quantity": 2}])
        self.assertEqual(amended["lines"][0]["quantity"], 2)
        self.assertEqual([e["action"] for e in self.app.history("O1")["events"]], ["place", "amend"])

    def test_other_operations_still_apply_to_paused_product(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.set_product_enabled("T", False)
        # Restock keeps working.
        self.assertEqual(self.app.restock("T", 3)["on_hand"], 8)
        # Fulfillment path: ship, return registration and return receiving.
        self.app.ship("O1", "DHL", "1Z")
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        receipt = self.app.receive_return("R1")
        self.assertEqual(receipt["lines"][0]["after"]["on_hand"], 7)
        # Stock count still applies.
        count = self.app.count_stock("S1", [{"sku": "T", "on_hand": 6}])
        self.assertEqual(count["lines"][0]["delta"], -1)
        # Queries still work.
        self.assertEqual(self.app.stock("T")["on_hand"], 6)
        self.assertIsNotNone(self.app.get_return_receipt("R1"))
        self.assertIsNotNone(self.app.get_stock_count("S1"))
        # Cancel path on a second order.
        self.app.set_product_enabled("T", True)
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        self.app.set_product_enabled("T", False)
        self.assertEqual(self.app.cancel("O2")["status"], "cancelled")
        self.assertEqual(self.app.stock("T")["reserved"], 0)


class EnabledCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)

    def run_cli(self, action, payload=None):
        args = [sys.executable, "-m", "order_desk", "--root", str(self.root), action]
        if payload is not None:
            path = self.root / "in.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            args.append(str(path))
        return subprocess.run(args, text=True, capture_output=True)

    def test_set_and_get_commands(self):
        ok = self.run_cli("set-product-enabled", {"sku": " T ", "enabled": False})
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout),
                         {"sku": "T", "name": "Tea", "price_cents": 100, "enabled": False})
        got = self.run_cli("get-product", {"sku": "T"})
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertFalse(json.loads(got.stdout)["enabled"])
        failed = self.run_cli("set-product-enabled", {"sku": "T", "enabled": 1})
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(failed.stdout, "")
        missing = self.run_cli("get-product", {"sku": "X"})
        self.assertEqual(missing.returncode, 2)
        self.assertTrue(OrderDesk(self.root).get_product("T")["enabled"] is False)

    def test_array_batch_partial_failure(self):
        self.app.add_product("C", "Coffee", 200)
        run = self.run_cli("set-product-enabled", [
            {"sku": "T", "enabled": False},
            {"sku": "X", "enabled": False},
            {"sku": "C", "enabled": False},
        ])
        self.assertEqual(run.returncode, 2)
        self.assertEqual(run.stdout, "")
        app = OrderDesk(self.root)
        self.assertFalse(app.get_product("T")["enabled"])
        self.assertTrue(app.get_product("C")["enabled"])


if __name__ == "__main__":
    unittest.main()
