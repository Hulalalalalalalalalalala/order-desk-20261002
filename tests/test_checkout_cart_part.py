import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk


class CheckoutCartPartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)
        self.app.add_product("Z", "Free card", 0)

    def test_partial_checkout_creates_order_and_keeps_leftovers(self):
        self.app.restock("T", 5)
        self.app.restock("C", 4)
        self.app.save_cart("K1", [
            {"sku": "Z", "quantity": 2},
            {"sku": "T", "quantity": 3},
            {"sku": "C", "quantity": 2},
        ])
        result = self.app.checkout_cart_part("K1", "O1", [
            {"sku": " T ", "quantity": 1, "note": "ignored"},
            {"sku": "T", "quantity": 1},
            {"sku": "C", "quantity": 2},
        ])
        self.assertEqual(set(result), {"order", "cart"})
        self.assertEqual(result["order"], {
            "order_id": "O1",
            "status": "placed",
            "lines": [
                {"sku": "C", "quantity": 2, "unit_price_cents": 200, "subtotal_cents": 400},
                {"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200},
            ],
            "total_cents": 600,
        })
        self.assertEqual(result["cart"], {
            "cart_id": "K1",
            "lines": [
                {"sku": "T", "quantity": 1},
                {"sku": "Z", "quantity": 2},
            ],
        })
        # The stored cart matches the returned one; on_hand never moves.
        self.assertEqual(self.app.get_cart("K1"), result["cart"])
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 5, "reserved": 2, "available": 3})
        self.assertEqual(self.app.stock("C"),
                         {"sku": "C", "on_hand": 4, "reserved": 2, "available": 2})
        # Order and stock history follow the place rules; no cart events.
        history = self.app.history("O1")
        self.assertTrue(history["complete"])
        self.assertEqual([event["action"] for event in history["events"]], ["place"])
        self.assertEqual(history["events"][0]["sequence"], 1)
        self.assertEqual(history["events"][0]["result"], result["order"])
        stock_events = self.app.stock_history("T")["events"]
        self.assertEqual([(e["action"], e["reference_id"]) for e in stock_events],
                         [("restock", None), ("place", "O1")])
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("cart_history", raw)

    def test_selecting_everything_deletes_cart_and_returns_null(self):
        self.app.restock("T", 5)
        self.app.save_cart("K1", [{"sku": "T", "quantity": 2}, {"sku": "Z", "quantity": 1}])
        result = self.app.checkout_cart_part("K1", "O1", [
            {"sku": "Z", "quantity": 1},
            {"sku": "T", "quantity": 2},
        ])
        self.assertIsNone(result["cart"])
        self.assertEqual(result["order"]["total_cents"], 200)
        with self.assertRaises(ValueError):
            self.app.get_cart("K1")
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("K1", raw.get("carts", {}))

    def test_partial_quantity_zeroes_a_line_and_drops_it(self):
        self.app.restock("T", 5)
        self.app.save_cart("K1", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])
        result = self.app.checkout_cart_part("K1", "O1", [{"sku": "C", "quantity": 1}])
        self.assertEqual(result["cart"],
                         {"cart_id": "K1", "lines": [{"sku": "T", "quantity": 2}]})

    def test_unmanaged_selected_sku_is_unlimited_and_stays_unmanaged(self):
        # Z and C were never restocked: no reservation, no auto-management.
        self.app.save_cart("K1", [{"sku": "C", "quantity": 99}, {"sku": "Z", "quantity": 4}])
        result = self.app.checkout_cart_part("K1", "O1", [{"sku": "C", "quantity": 99}])
        self.assertEqual(result["order"]["lines"], [
            {"sku": "C", "quantity": 99, "unit_price_cents": 200, "subtotal_cents": 19800},
        ])
        self.assertEqual(self.app.stock("C"),
                         {"sku": "C", "on_hand": None, "reserved": 0, "available": None})
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("C", raw.get("inventory", {}))
        self.assertNotIn("O1", raw.get("reservations", {}))

    def test_unselected_paused_missing_or_out_of_stock_never_blocks(self):
        self.app.restock("T", 1)
        self.app.save_cart("K1", [
            {"sku": "T", "quantity": 1},
            {"sku": "C", "quantity": 5},
            {"sku": "Z", "quantity": 2},
        ])
        # After saving: T goes paused, C disappears from the catalog, and
        # managed T has no availability for more than its one unit.
        self.app.set_product_enabled("T", False)
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        del raw["products"]["C"]
        self.app.path.write_text(json.dumps(raw), encoding="utf-8")
        # Selecting only Z: leftovers keep paused T and catalog-less C as-is.
        result = OrderDesk(self.root).checkout_cart_part("K1", "O1", [{"sku": "Z", "quantity": 2}])
        self.assertEqual(result["cart"]["lines"], [
            {"sku": "C", "quantity": 5},
            {"sku": "T", "quantity": 1},
        ])

    def test_selected_paused_missing_or_short_product_rejects_whole_request(self):
        self.app.restock("T", 2)
        self.app.save_cart("K1", [
            {"sku": "T", "quantity": 3},
            {"sku": "C", "quantity": 1},
            {"sku": "Z", "quantity": 2},
        ])
        self.app.set_product_enabled("Z", False)
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.checkout_cart_part("K1", "O1", [{"sku": "Z", "quantity": 1}])
        with self.assertRaises(ValueError):
            # Managed T: available 2 < requested 3 even though the cart holds 3.
            self.app.checkout_cart_part("K1", "O2", [{"sku": "T", "quantity": 3}])
        # Before the manual rewrite, bytes must be untouched.
        self.assertEqual(self.app.path.read_bytes(), before)
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        del raw["products"]["C"]
        self.app.path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(ValueError):
            OrderDesk(self.root).checkout_cart_part("K1", "O3", [{"sku": "C", "quantity": 1}])
        # Nothing moved: no orders/reservations, cart intact, stock unchanged.
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(set(raw.get("orders", {})), set())
        self.assertFalse(raw.get("reservations"))
        self.assertEqual(raw["carts"]["K1"]["lines"], [
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 3},
            {"sku": "Z", "quantity": 2},
        ])
        self.assertEqual(OrderDesk(self.root).stock("T"),
                         {"sku": "T", "on_hand": 2, "reserved": 0, "available": 2})

    def test_invalid_inputs_raise_and_leave_state_untouched(self):
        self.app.restock("T", 5)
        self.app.save_cart("K1", [{"sku": "T", "quantity": 2}])
        self.app.place("O0", [{"sku": "T", "quantity": 1}])
        before = self.app.path.read_bytes()
        bad_calls = [
            ("K9", "O1", [{"sku": "T", "quantity": 1}]),     # unknown cart
            ("  ", "O1", [{"sku": "T", "quantity": 1}]),     # invalid cart id
            (3, "O1", [{"sku": "T", "quantity": 1}]),
            ("K1", "  ", [{"sku": "T", "quantity": 1}]),     # invalid order id
            ("K1", 4, [{"sku": "T", "quantity": 1}]),
            ("K1", "O0", [{"sku": "T", "quantity": 1}]),     # order id exists
            ("K1", "O1", []),                                # empty lines
            ("K1", "O1", "x"),
            ("K1", "O1", 42),
            ("K1", "O1", None),
            ("K1", "O1", {}),
            ("K1", "O1", ["x"]),                             # line not object
            ("K1", "O1", [42]),
            ("K1", "O1", [None]),
            ("K1", "O1", [{"quantity": 1}]),                 # missing sku
            ("K1", "O1", [{"sku": "T"}]),                    # missing quantity
            ("K1", "O1", [{"sku": "  ", "quantity": 1}]),    # bad sku
            ("K1", "O1", [{"sku": 3, "quantity": 1}]),
            ("K1", "O1", [{"sku": "T", "quantity": 0}]),     # bad quantity
            ("K1", "O1", [{"sku": "T", "quantity": -1}]),
            ("K1", "O1", [{"sku": "T", "quantity": 1.5}]),
            ("K1", "O1", [{"sku": "T", "quantity": True}]),
            ("K1", "O1", [{"sku": "T", "quantity": "1"}]),
            ("K1", "O1", [{"sku": "C", "quantity": 1}]),     # not in cart
            ("K1", "O1", [{"sku": "t", "quantity": 1}]),     # case sensitive
            ("K1", "O1", [{"sku": "T", "quantity": 3}]),     # over cart quantity
        ]
        for cart_id, order_id, lines in bad_calls:
            with self.subTest(cart_id=cart_id, order_id=order_id, lines=lines):
                with self.assertRaises(ValueError):
                    self.app.checkout_cart_part(cart_id, order_id, lines)
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.get_cart("K1"),
                         {"cart_id": "K1", "lines": [{"sku": "T", "quantity": 2}]})

    def test_merged_duplicates_are_checked_together(self):
        self.app.restock("T", 3)
        self.app.save_cart("K1", [{"sku": "T", "quantity": 3}])
        # Each line fits alone; merged 2+2=4 exceeds both the cart and stock.
        with self.assertRaises(ValueError):
            self.app.checkout_cart_part("K1", "O1", [
                {"sku": "T", "quantity": 2},
                {"sku": "T", "quantity": 2},
            ])
        self.assertEqual(self.app.get_cart("K1"),
                         {"cart_id": "K1", "lines": [{"sku": "T", "quantity": 3}]})

    def test_failure_consumes_no_sequence(self):
        self.app.restock("T", 5)
        self.app.save_cart("K1", [{"sku": "T", "quantity": 2}])
        self.app.place("O0", [{"sku": "T", "quantity": 1}])  # T stock history seq 2
        with self.assertRaises(ValueError):
            self.app.checkout_cart_part("K1", "O1", [{"sku": "T", "quantity": 9}])
        self.app.checkout_cart_part("K1", "O1", [{"sku": "T", "quantity": 1}])
        actions = [(e["action"], e["reference_id"]) for e in self.app.stock_history("T")["events"]]
        self.assertEqual(actions, [("restock", None), ("place", "O0"), ("place", "O1")])
        self.assertEqual([e["sequence"] for e in self.app.stock_history("T")["events"]], [1, 2, 3])

    def test_prices_come_from_catalog_at_submission_time(self):
        self.app.save_cart("K1", [{"sku": "T", "quantity": 4}])
        self.app.reprice_products([
            {"sku": "T", "expected_price_cents": 100, "price_cents": 150}
        ])
        result = self.app.checkout_cart_part("K1", "O1", [{"sku": "T", "quantity": 2}])
        self.assertEqual(result["order"]["lines"], [
            {"sku": "T", "quantity": 2, "unit_price_cents": 150, "subtotal_cents": 300},
        ])

    def test_cart_and_order_ids_remain_independent(self):
        # An order id never blocks a cart id and vice versa.
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.app.save_cart("O1", [{"sku": "C", "quantity": 1}])
        self.app.save_cart("O2", [{"sku": "Z", "quantity": 1}])
        result = self.app.checkout_cart_part("O1", "K1", [{"sku": "C", "quantity": 1}])
        self.assertIsNone(result["cart"])
        self.assertEqual(result["order"]["order_id"], "K1")
        # The same-named original order is untouched; reusing its id fails.
        self.assertEqual(self.app.get("O1")["lines"][0]["sku"], "T")
        with self.assertRaises(ValueError):
            self.app.checkout_cart_part("O2", "O1", [{"sku": "Z", "quantity": 1}])
        # The rejected checkout leaves that cart in place.
        self.assertEqual(self.app.get_cart("O2"),
                         {"cart_id": "O2", "lines": [{"sku": "Z", "quantity": 1}]})

    def test_persistence_across_reopen(self):
        self.app.restock("T", 5)
        self.app.save_cart("K1", [{"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 1}])
        result = self.app.checkout_cart_part("K1", "O1", [{"sku": "T", "quantity": 2}])
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.get_cart("K1"), result["cart"])
        self.assertEqual(reopened.get("O1"), result["order"])
        self.assertEqual(reopened.history("O1")["events"][0]["result"], result["order"])
        self.assertEqual(reopened.stock("T")["reserved"], 2)

    def test_legacy_data_without_carts_behaves_as_empty(self):
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("carts", raw)
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            OrderDesk(self.root).checkout_cart_part("K1", "O2", [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_unknown_cart_never_creates_directory(self):
        fresh = self.root / "fresh"
        app = OrderDesk(fresh)
        with self.assertRaises(ValueError):
            app.checkout_cart_part("K1", "O1", [{"sku": "T", "quantity": 1}])
        self.assertFalse(fresh.exists())

    def test_cli_checkout_cart_part(self):
        self.app.restock("T", 5)
        self.app.save_cart("K1", [{"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 1}])
        payload = self.root / "part.json"
        payload.write_text(json.dumps({
            "cart_id": "K1", "order_id": "O1",
            "lines": [{"sku": "T", "quantity": 2}, {"sku": "T", "quantity": 1}],
        }), encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                             "checkout-cart-part", str(payload)], text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual(result["cart"],
                         {"cart_id": "K1", "lines": [{"sku": "C", "quantity": 1}]})
        self.assertEqual(result["order"]["lines"], [
            {"sku": "T", "quantity": 3, "unit_price_cents": 100, "subtotal_cents": 300},
        ])
        # Failure: error JSON on stderr, empty stdout, exit code 2.
        bad = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                              "checkout-cart-part", str(payload)], text=True, capture_output=True)
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(bad.stdout, "")
        self.assertIn("error", json.loads(bad.stderr))

    def test_cli_array_rows_stay_independent(self):
        self.app.restock("T", 5)
        self.app.save_cart("K1", [{"sku": "T", "quantity": 2}])
        self.app.save_cart("K2", [{"sku": "C", "quantity": 1}])
        payload = self.root / "batch.json"
        payload.write_text(json.dumps([
            {"cart_id": "K1", "order_id": "O1", "lines": [{"sku": "T", "quantity": 2}]},
            {"cart_id": "K2", "order_id": "O2", "lines": [{"sku": "X", "quantity": 1}]},
        ]), encoding="utf-8")
        failed = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                                 "checkout-cart-part", str(payload)],
                                text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(failed.stdout, "")
        self.assertIn("error", json.loads(failed.stderr))
        # The first row succeeded: its cart is gone and the order exists.
        with self.assertRaises(ValueError):
            self.app.get_cart("K1")
        self.assertEqual(self.app.get("O1")["total_cents"], 200)
        # The second row never happened.
        self.assertEqual(self.app.get_cart("K2"),
                         {"cart_id": "K2", "lines": [{"sku": "C", "quantity": 1}]})
        with self.assertRaises(ValueError):
            self.app.get("O2")


if __name__ == "__main__":
    unittest.main()
