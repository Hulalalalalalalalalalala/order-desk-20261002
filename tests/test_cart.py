import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk


class CartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)
        self.app.add_product("Z", "Free card", 0)

    def test_save_merges_sorts_and_normalizes_view(self):
        result = self.app.save_cart("K1", [
            {"sku": "Z", "quantity": 2, "note": "ignored"},
            {"sku": "T", "quantity": 1},
            {"sku": " T ", "quantity": 2},
            {"sku": "C", "quantity": 7},
        ])
        self.assertEqual(set(result), {"cart_id", "lines"})
        self.assertEqual(result, {
            "cart_id": "K1",
            "lines": [
                {"sku": "C", "quantity": 7},
                {"sku": "T", "quantity": 3},
                {"sku": "Z", "quantity": 2},
            ],
        })
        for line in result["lines"]:
            self.assertEqual(set(line), {"sku", "quantity"})
        self.assertEqual(self.app.get_cart(" K1 "), result)

    def test_resave_replaces_entire_cart(self):
        self.app.save_cart("K1", [{"sku": "T", "quantity": 1}])
        replaced = self.app.save_cart("K1", [{"sku": "C", "quantity": 2}])
        self.assertEqual(replaced["lines"], [{"sku": "C", "quantity": 2}])
        self.assertEqual(self.app.get_cart("K1"), replaced)

    def test_carts_orders_and_skus_are_separate_namespaces(self):
        self.app.place("X", [{"sku": "T", "quantity": 1}])
        # An order id does not occupy the cart namespace and vice versa.
        saved = self.app.save_cart("X", [{"sku": "C", "quantity": 1}])
        self.assertEqual(saved["cart_id"], "X")
        self.assertEqual(self.app.get_cart("X"), saved)
        # SKU matching is case sensitive.
        with self.assertRaises(ValueError):
            self.app.save_cart("K2", [{"sku": "t", "quantity": 1}])
        self.app.save_cart(" K 2 ", [{"sku": "C", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.get_cart("k 2")

    def test_paused_and_short_stock_products_are_savable(self):
        self.app.restock("T", 2)
        self.app.set_product_enabled("C", False)
        result = self.app.save_cart("K1", [
            {"sku": "C", "quantity": 9},
            {"sku": "T", "quantity": 5},
        ])
        self.assertEqual(result["lines"], [
            {"sku": "C", "quantity": 9},
            {"sku": "T", "quantity": 5},
        ])
        # Saving records no price, reserves nothing and creates no history.
        self.assertEqual(self.app.stock("T")["reserved"], 0)
        self.assertEqual(self.app.list_orders(), [])

    def test_invalid_save_inputs_raise_value_error(self):
        before = self.app.path.read_bytes()
        bad_inputs = [
            ([], "K"), ("x", "K"), (42, "K"), (None, "K"), ({}, "K"),
            ([{"sku": "T"}], "K"),
            ([{"quantity": 1}], "K"),
            (["x"], "K"), ([42], "K"), ([None], "K"),
            ([{"sku": "  ", "quantity": 1}], "K"),
            ([{"sku": 3, "quantity": 1}], "K"),
            ([{"sku": "T", "quantity": 0}], "K"),
            ([{"sku": "T", "quantity": -2}], "K"),
            ([{"sku": "T", "quantity": 1.5}], "K"),
            ([{"sku": "T", "quantity": True}], "K"),
            ([{"sku": "T", "quantity": "1"}], "K"),
            ([{"sku": "X", "quantity": 1}], "K"),
        ]
        for lines, cart_id in bad_inputs:
            with self.subTest(lines=lines, cart_id=cart_id):
                with self.assertRaises(ValueError):
                    self.app.save_cart(cart_id, lines)
        for cart_id in (None, 42, "   "):
            with self.subTest(cart_id=cart_id):
                with self.assertRaises(ValueError):
                    self.app.save_cart(cart_id, [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_failed_resave_does_not_overwrite(self):
        self.app.save_cart("K1", [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.save_cart("K1", [{"sku": "X", "quantity": 1}])
        self.assertEqual(self.app.get_cart("K1"), {
            "cart_id": "K1",
            "lines": [{"sku": "T", "quantity": 1}],
        })

    def test_get_unknown_or_invalid_raises_and_never_writes(self):
        with self.assertRaises(ValueError):
            self.app.get_cart("missing")
        for cart_id in (None, 42, "   "):
            with self.assertRaises(ValueError):
                self.app.get_cart(cart_id)
        fresh = self.root / "fresh"
        with self.assertRaises(ValueError):
            OrderDesk(fresh).get_cart("K1")
        self.assertFalse(fresh.exists())

    def test_checkout_creates_order_history_and_removes_cart(self):
        self.app.restock("T", 5)
        self.app.save_cart("K1", [
            {"sku": "T", "quantity": 2},
            {"sku": "Z", "quantity": 1},
            {"sku": "T", "quantity": 1},
        ])
        order = self.app.checkout_cart("K1", "O1")
        self.assertEqual(order, {
            "order_id": "O1",
            "status": "placed",
            "lines": [
                {"sku": "T", "quantity": 3, "unit_price_cents": 100, "subtotal_cents": 300},
                {"sku": "Z", "quantity": 1, "unit_price_cents": 0, "subtotal_cents": 0},
            ],
            "total_cents": 300,
        })
        self.assertEqual(self.app.get("O1"), order)
        with self.assertRaises(ValueError):
            self.app.get_cart("K1")
        self.assertEqual(self.app.stock("T")["reserved"], 3)
        history = self.app.history("O1")
        self.assertTrue(history["complete"])
        self.assertEqual([event["action"] for event in history["events"]], ["place"])
        self.assertEqual(history["events"][0]["result"], order)

    def test_checkout_uses_current_catalog_price(self):
        self.app.save_cart("K1", [{"sku": "T", "quantity": 2}])
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        raw["products"]["T"]["price_cents"] = 150
        self.app._write(raw)
        order = OrderDesk(self.root).checkout_cart("K1", "O1")
        self.assertEqual(order["total_cents"], 300)
        self.assertEqual(order["lines"][0]["unit_price_cents"], 150)

    def test_checkout_unmanaged_product_not_restricted(self):
        self.app.save_cart("K1", [{"sku": "T", "quantity": 1000}])
        order = self.app.checkout_cart("K1", "O1")
        self.assertEqual(order["total_cents"], 100000)
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("O1", raw.get("reservations", {}))

    def test_checkout_failures_leave_everything_untouched(self):
        self.app.restock("T", 2)
        self.app.set_product_enabled("C", False)
        self.app.place("O0", [{"sku": "Z", "quantity": 1}])
        self.app.save_cart("K-short", [{"sku": "T", "quantity": 3}])
        self.app.save_cart("K-paused", [{"sku": "C", "quantity": 1}])
        self.app.save_cart("K-unknown", [{"sku": "T", "quantity": 1}])
        raw_products = json.loads(self.app.path.read_text(encoding="utf-8"))["products"]
        # Simulate a product removed after the cart was saved.
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        del raw["products"]["T"]
        self.app._write(raw)
        with self.assertRaises(ValueError):
            self.app.checkout_cart("K-unknown", "O1")
        raw["products"] = raw_products
        self.app._write(raw)
        cases = [
            ("K-short", "O1", "insufficient stock"),
            ("K-paused", "O1", "not available for sale"),
            ("missing", "O1", "unknown cart"),
            ("K-short", "O0", "order already exists"),
        ]
        for cart_id, order_id, message in cases:
            with self.subTest(cart_id=cart_id, order_id=order_id):
                with self.assertRaises(ValueError) as caught:
                    self.app.checkout_cart(cart_id, order_id)
                self.assertIn(message, str(caught.exception))
        for invalid_cart in (None, 42, "   "):
            with self.assertRaises(ValueError):
                self.app.checkout_cart(invalid_cart, "O1")
        for invalid_order in (None, 42, "   "):
            with self.assertRaises(ValueError):
                self.app.checkout_cart("K-short", invalid_order)
        # Carts, orders, stock and history are unchanged after every rejection.
        for cart_id in ("K-short", "K-paused", "K-unknown"):
            self.assertTrue(self.app.get_cart(cart_id)["lines"])
        self.assertEqual(self.app.list_orders(), [self.app.get("O0")])
        self.assertEqual(self.app.stock("T")["reserved"], 0)
        # O0 keeps only its original place event; failed checkouts add nothing.
        self.assertEqual([e["action"] for e in self.app.history("O0")["events"]], ["place"])

    def test_persistence_across_reopen(self):
        self.app.restock("T", 5)
        self.app.save_cart("K1", [{"sku": "T", "quantity": 1}])
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.get_cart("K1"), {
            "cart_id": "K1",
            "lines": [{"sku": "T", "quantity": 1}],
        })
        reopened.checkout_cart("K1", "O1")
        again = OrderDesk(self.root)
        with self.assertRaises(ValueError):
            again.get_cart("K1")
        self.assertEqual(again.get("O1")["total_cents"], 100)
        self.assertEqual(again.stock("T")["reserved"], 1)
        self.assertTrue(again.history("O1")["complete"])

    def test_legacy_data_without_carts(self):
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("carts", raw)
        with self.assertRaises(ValueError):
            OrderDesk(self.root).get_cart("K1")
        raw_after = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("carts", raw_after)

    def test_cli_commands(self):
        payload = self.root / "cart.json"
        payload.write_text(json.dumps({"cart_id": "K1", "lines": [
            {"sku": "Z", "quantity": 1}, {"sku": "T", "quantity": 2}, {"sku": "T", "quantity": 1},
        ]}), encoding="utf-8")
        saved = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "save-cart", str(payload)],
                               text=True, capture_output=True)
        self.assertEqual(saved.returncode, 0, saved.stderr)
        self.assertEqual(json.loads(saved.stdout), {
            "cart_id": "K1",
            "lines": [{"sku": "T", "quantity": 3}, {"sku": "Z", "quantity": 1}],
        })
        query = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "cart",
                                str(self.root / "id.json")], text=True, capture_output=True)
        # Missing file exits 2; pass the id through a file.
        self.assertEqual(query.returncode, 2)
        (self.root / "id.json").write_text(json.dumps({"cart_id": "K1"}), encoding="utf-8")
        query = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "cart",
                                str(self.root / "id.json")], text=True, capture_output=True)
        self.assertEqual(query.returncode, 0, query.stderr)
        self.assertEqual(json.loads(query.stdout)["cart_id"], "K1")
        (self.root / "checkout.json").write_text(json.dumps({"cart_id": "K1", "order_id": "O1"}), encoding="utf-8")
        done = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                               "checkout-cart", str(self.root / "checkout.json")],
                              text=True, capture_output=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["order_id"], "O1")
        again = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                                "cart", str(self.root / "id.json")], text=True, capture_output=True)
        self.assertEqual(again.returncode, 2)
        self.assertIn("unknown cart", json.loads(again.stderr)["error"])

    def test_cli_array_keeps_successful_rows(self):
        payload = self.root / "batch.json"
        payload.write_text(json.dumps([
            {"cart_id": "K1", "lines": [{"sku": "T", "quantity": 1}]},
            {"cart_id": "K2", "lines": [{"sku": "X", "quantity": 1}]},
        ]), encoding="utf-8")
        failed = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                                 "save-cart", str(payload)], text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2)
        self.assertIn("unknown product", json.loads(failed.stderr)["error"])
        self.assertEqual(self.app.get_cart("K1")["lines"], [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.get_cart("K2")


if __name__ == "__main__":
    unittest.main()
