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

    def test_save_merges_sorts_and_returns_minimal_shape(self):
        result = self.app.save_cart("K1", [
            {"sku": "Z", "quantity": 2},
            {"sku": "T", "quantity": 1, "note": "ignored"},
            {"sku": " T ", "quantity": 2},
            {"sku": "C", "quantity": 7},
        ])
        self.assertEqual(result, {
            "cart_id": "K1",
            "lines": [
                {"sku": "C", "quantity": 7},
                {"sku": "T", "quantity": 3},
                {"sku": "Z", "quantity": 2},
            ],
        })
        self.assertEqual(self.app.get_cart("K1"), result)
        self.assertEqual(self.app.get_cart(" K1 "), result)

    def test_resave_replaces_cart_wholesale(self):
        self.app.save_cart("K1", [{"sku": "T", "quantity": 1}, {"sku": "C", "quantity": 2}])
        result = self.app.save_cart("K1", [{"sku": "Z", "quantity": 5}])
        self.assertEqual(result, {"cart_id": "K1", "lines": [{"sku": "Z", "quantity": 5}]})
        self.assertEqual(self.app.get_cart("K1"), result)

    def test_save_invalid_inputs_raise_and_keep_state(self):
        self.app.save_cart("K1", [{"sku": "T", "quantity": 1}])
        before = self.app.path.read_bytes()
        bad_inputs = [
            ([], "K2"), ("x", "K2"), (42, "K2"), (None, "K2"), ({}, "K2"),
            ([{"sku": "T"}], "K2"),
            ([{"quantity": 1}], "K2"),
            (["x"], "K2"), ([42], "K2"), ([None], "K2"),
            ([{"sku": "  ", "quantity": 1}], "K2"),
            ([{"sku": 3, "quantity": 1}], "K2"),
            ([{"sku": "T", "quantity": 0}], "K2"),
            ([{"sku": "T", "quantity": -2}], "K2"),
            ([{"sku": "T", "quantity": 1.5}], "K2"),
            ([{"sku": "T", "quantity": True}], "K2"),
            ([{"sku": "T", "quantity": "1"}], "K2"),
            ([{"sku": "X", "quantity": 1}], "K2"),
            ([{"sku": "T", "quantity": 1}], "  "),
            ([{"sku": "T", "quantity": 1}], 3),
            # Same failures must not rewrite an existing cart either.
            ([{"sku": "X", "quantity": 1}], "K1"),
            ([], "K1"),
        ]
        for lines, cart_id in bad_inputs:
            with self.subTest(lines=lines, cart_id=cart_id):
                with self.assertRaises(ValueError):
                    self.app.save_cart(cart_id, lines)
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.get_cart("K1"),
                         {"cart_id": "K1", "lines": [{"sku": "T", "quantity": 1}]})

    def test_save_allows_paused_and_out_of_stock_without_side_effects(self):
        self.app.set_product_enabled("T", False)
        self.app.restock("C", 1)
        result = self.app.save_cart("K1", [{"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 9}])
        self.assertEqual(result["lines"], [
            {"sku": "C", "quantity": 9},
            {"sku": "T", "quantity": 3},
        ])
        # Saving never reserves stock, records prices or touches history.
        self.assertEqual(self.app.stock("C"), {"sku": "C", "on_hand": 1, "reserved": 0, "available": 1})
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("orders", raw)
        self.assertNotIn("history", raw)
        self.assertNotIn("reservations", raw)
        self.assertEqual(raw["carts"]["K1"], result)

    def test_get_cart_unknown_or_invalid_id(self):
        fresh = self.root / "fresh"
        app = OrderDesk(fresh)
        for cart_id in ("K9", "  ", 3, None):
            with self.subTest(cart_id=cart_id):
                with self.assertRaises(ValueError):
                    app.get_cart(cart_id)
        # Querying never creates directories or files.
        self.assertFalse(fresh.exists())
        # SKU matching is case sensitive; cart ids too.
        self.app.save_cart("K1", [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.get_cart("k1")

    def test_checkout_places_order_and_removes_cart(self):
        self.app.restock("T", 5)
        self.app.save_cart("K1", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])
        order = self.app.checkout_cart("K1", "O1")
        self.assertEqual(order, {
            "order_id": "O1",
            "status": "placed",
            "lines": [
                {"sku": "C", "quantity": 1, "unit_price_cents": 200, "subtotal_cents": 200},
                {"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200},
            ],
            "total_cents": 400,
        })
        self.assertEqual(order, self.app.get("O1"))
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 5, "reserved": 2, "available": 3})
        with self.assertRaises(ValueError):
            self.app.get_cart("K1")
        history = self.app.history("O1")
        self.assertTrue(history["complete"])
        self.assertEqual([event["action"] for event in history["events"]], ["place"])
        self.assertEqual(history["events"][0]["sequence"], 1)
        self.assertEqual(history["events"][0]["result"], order)

    def test_checkout_uses_catalog_and_stock_at_checkout_time(self):
        self.app.save_cart("K1", [{"sku": "T", "quantity": 2}])
        self.app.restock("T", 1)
        # Stock ran out after saving: checkout fails and the cart survives.
        with self.assertRaises(ValueError):
            self.app.checkout_cart("K1", "O1")
        self.assertEqual(self.app.get_cart("K1"),
                         {"cart_id": "K1", "lines": [{"sku": "T", "quantity": 2}]})
        self.app.restock("T", 1)
        order = self.app.checkout_cart("K1", "O1")
        self.assertEqual(order["total_cents"], 200)
        self.assertEqual(self.app.stock("T")["reserved"], 2)

    def test_checkout_rejects_paused_product_and_keeps_state(self):
        self.app.save_cart("K1", [{"sku": "T", "quantity": 1}])
        self.app.set_product_enabled("T", False)
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.checkout_cart("K1", "O1")
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.get_cart("K1"),
                         {"cart_id": "K1", "lines": [{"sku": "T", "quantity": 1}]})

    def test_checkout_failures_leave_everything_untouched(self):
        self.app.restock("T", 5)
        self.app.save_cart("K1", [{"sku": "T", "quantity": 2}])
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        before = self.app.path.read_bytes()
        bad_calls = [
            ("K9", "O2"),      # unknown cart
            ("  ", "O2"),      # invalid cart id
            ("K1", "  "),      # invalid order id
            ("K1", "O1"),      # order id already exists
        ]
        for cart_id, order_id in bad_calls:
            with self.subTest(cart_id=cart_id, order_id=order_id):
                with self.assertRaises(ValueError):
                    self.app.checkout_cart(cart_id, order_id)
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_cart_and_order_ids_do_not_share_namespace(self):
        # An order id never blocks a cart id and vice versa.
        self.app.place("K1", [{"sku": "T", "quantity": 1}])
        cart = self.app.save_cart("K1", [{"sku": "C", "quantity": 1}])
        self.assertEqual(cart["cart_id"], "K1")
        # The order namespace still rejects the duplicate order id...
        with self.assertRaises(ValueError):
            self.app.checkout_cart("K1", "K1")
        # ...while the cart itself is intact and can check out to a fresh id.
        order = self.app.checkout_cart("K1", "O9")
        self.assertEqual(order["lines"],
                         [{"sku": "C", "quantity": 1, "unit_price_cents": 200, "subtotal_cents": 200}])
        self.assertEqual(self.app.get("K1")["lines"][0]["sku"], "T")

    def test_persistence_across_reopen(self):
        self.app.restock("T", 5)
        self.app.save_cart("K1", [{"sku": "T", "quantity": 2}])
        self.app.save_cart("K2", [{"sku": "C", "quantity": 1}])
        order = self.app.checkout_cart("K1", "O1")
        reopened = OrderDesk(self.root)
        # Un-checked-out carts survive a reopen; checked-out carts never return.
        self.assertEqual(reopened.get_cart("K2"),
                         {"cart_id": "K2", "lines": [{"sku": "C", "quantity": 1}]})
        with self.assertRaises(ValueError):
            reopened.get_cart("K1")
        self.assertEqual(reopened.get("O1"), order)
        self.assertEqual(reopened.history("O1")["events"][0]["result"], order)
        self.assertEqual(reopened.stock("T")["reserved"], 2)

    def test_legacy_data_without_carts_behaves_as_empty(self):
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("carts", raw)
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            OrderDesk(self.root).get_cart("K1")
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_cli_cart_commands(self):
        payload = self.root / "cart.json"
        payload.write_text(json.dumps({"cart_id": "K1", "lines": [
            {"sku": "T", "quantity": 2}, {"sku": "T", "quantity": 1},
        ]}), encoding="utf-8")
        saved = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                                "save-cart", str(payload)], text=True, capture_output=True)
        self.assertEqual(saved.returncode, 0, saved.stderr)
        self.assertEqual(json.loads(saved.stdout),
                         {"cart_id": "K1", "lines": [{"sku": "T", "quantity": 3}]})
        payload.write_text(json.dumps({"cart_id": "K1"}), encoding="utf-8")
        got = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                              "cart", str(payload)], text=True, capture_output=True)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(json.loads(got.stdout)["lines"], [{"sku": "T", "quantity": 3}])
        payload.write_text(json.dumps({"cart_id": "K1", "order_id": "O1"}), encoding="utf-8")
        out = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                              "checkout-cart", str(payload)], text=True, capture_output=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["total_cents"], 300)
        again = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                                "checkout-cart", str(payload)], text=True, capture_output=True)
        self.assertEqual(again.returncode, 2)
        self.assertEqual(again.stdout, "")
        self.assertIn("error", json.loads(again.stderr))

    def test_cli_array_keeps_successful_rows(self):
        payload = self.root / "batch.json"
        payload.write_text(json.dumps([
            {"cart_id": "K1", "lines": [{"sku": "T", "quantity": 1}]},
            {"cart_id": "K2", "lines": [{"sku": "X", "quantity": 1}]},
        ]), encoding="utf-8")
        failed = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                                 "save-cart", str(payload)], text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(failed.stdout, "")
        self.assertIn("unknown product", json.loads(failed.stderr)["error"])
        # The first row succeeded and stays saved.
        self.assertEqual(self.app.get_cart("K1"),
                         {"cart_id": "K1", "lines": [{"sku": "T", "quantity": 1}]})


if __name__ == "__main__":
    unittest.main()
