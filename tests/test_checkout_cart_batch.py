import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk


class CheckoutCartBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)
        self.app.add_product("Z", "Free card", 0)
        self.app.add_product("U", "Unmanaged", 50)

    def test_batch_checks_out_every_cart_into_full_orders_sorted(self):
        self.app.restock("T", 10)
        self.app.restock("C", 10)
        self.app.save_cart("K1", [
            {"sku": "Z", "quantity": 2},
            {"sku": "T", "quantity": 1, "note": "ignored"},
            {"sku": " T ", "quantity": 2},
            {"sku": "C", "quantity": 7},
        ])
        self.app.save_cart("K2", [{"sku": "T", "quantity": 2}, {"sku": "U", "quantity": 4}])
        # Input order is reversed; results come back sorted by order_id.
        result = self.app.checkout_cart_batch([
            {"cart_id": " K2 ", "order_id": "O2", "extra": "ignored"},
            {"cart_id": "K1", "order_id": "O1"},
        ])
        self.assertEqual([order["order_id"] for order in result], ["O1", "O2"])
        self.assertEqual(result[0], {
            "order_id": "O1",
            "status": "placed",
            "lines": [
                {"sku": "C", "quantity": 7, "unit_price_cents": 200, "subtotal_cents": 1400},
                {"sku": "T", "quantity": 3, "unit_price_cents": 100, "subtotal_cents": 300},
                {"sku": "Z", "quantity": 2, "unit_price_cents": 0, "subtotal_cents": 0},
            ],
            "total_cents": 1700,
        })
        # Zero-price and unmanaged lines are retained.
        self.assertEqual(result[1]["lines"], [
            {"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200},
            {"sku": "U", "quantity": 4, "unit_price_cents": 50, "subtotal_cents": 200},
        ])
        for order in result:
            self.assertEqual(self.app.get(order["order_id"]), order)
        # Selected carts are gone; get_cart raises afterward.
        for cart_id in ("K1", "K2"):
            with self.assertRaises(ValueError):
                self.app.get_cart(cart_id)

    def test_prices_use_catalog_and_stock_at_submission_time(self):
        self.app.save_cart("K1", [{"sku": "T", "quantity": 2}])
        self.app.save_cart("K2", [{"sku": "T", "quantity": 1}])
        self.app.restock("T", 5)
        self.app.reprice_products([
            {"sku": "T", "expected_price_cents": 100, "price_cents": 150},
        ])
        result = self.app.checkout_cart_batch([
            {"cart_id": "K1", "order_id": "O1"},
            {"cart_id": "K2", "order_id": "O2"},
        ])
        self.assertEqual([order["total_cents"] for order in result], [300, 150])

    def test_combined_demand_checked_against_shared_available(self):
        self.app.restock("T", 5)
        self.app.save_cart("K1", [{"sku": "T", "quantity": 3}])
        self.app.save_cart("K2", [{"sku": "T", "quantity": 3}])
        # Five available for a combined demand of six: whole batch rejected and
        # both carts survive.
        with self.assertRaises(ValueError):
            self.app.checkout_cart_batch([
                {"cart_id": "K1", "order_id": "O1"},
                {"cart_id": "K2", "order_id": "O2"},
            ])
        self.assertEqual(self.app.get_cart("K1"),
                         {"cart_id": "K1", "lines": [{"sku": "T", "quantity": 3}]})
        self.assertEqual(self.app.get_cart("K2"),
                         {"cart_id": "K2", "lines": [{"sku": "T", "quantity": 3}]})
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 5, "reserved": 0, "available": 5})
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("orders", raw)
        self.assertNotIn("history", raw)
        self.assertNotIn("reservations", raw)
        # Six available: both orders are created and six are reserved; on_hand
        # is unchanged.
        self.app.restock("T", 1)
        result = self.app.checkout_cart_batch([
            {"cart_id": "K1", "order_id": "O1"},
            {"cart_id": "K2", "order_id": "O2"},
        ])
        self.assertEqual([order["order_id"] for order in result], ["O1", "O2"])
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 6, "reserved": 6, "available": 0})
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["reservations"], {"O1": {"T": 3}, "O2": {"T": 3}})

    def test_existing_reservations_count_against_shared_available(self):
        self.app.restock("T", 6)
        # An outside order already holds two, leaving four for the batch.
        self.app.place("OUT", [{"sku": "T", "quantity": 2}])
        self.app.save_cart("K1", [{"sku": "T", "quantity": 2}])
        self.app.save_cart("K2", [{"sku": "T", "quantity": 3}])
        with self.assertRaises(ValueError):
            self.app.checkout_cart_batch([
                {"cart_id": "K1", "order_id": "O1"},
                {"cart_id": "K2", "order_id": "O2"},
            ])
        self.assertEqual(self.app.stock("T")["reserved"], 2)
        # The outside order and its reservation are untouched after success too.
        self.app.save_cart("K1", [{"sku": "T", "quantity": 2}])
        self.app.save_cart("K2", [{"sku": "T", "quantity": 2}])
        self.app.checkout_cart_batch([
            {"cart_id": "K1", "order_id": "O1"},
            {"cart_id": "K2", "order_id": "O2"},
        ])
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 6, "reserved": 6, "available": 0})
        self.assertEqual(self.app.get("OUT")["status"], "placed")

    def test_unknown_or_paused_product_rejects_whole_batch(self):
        self.app.restock("T", 5)
        self.app.save_cart("K1", [{"sku": "T", "quantity": 1}])
        self.app.save_cart("K2", [{"sku": "T", "quantity": 1}])
        self.app.set_product_enabled("T", False)
        # A cart holding a product removed from the catalog after it was saved.
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        raw["carts"]["KG"] = {"cart_id": "KG", "lines": [{"sku": "GONE", "quantity": 1}]}
        self.app.path.write_text(json.dumps(raw), encoding="utf-8")
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.checkout_cart_batch([
                {"cart_id": "K1", "order_id": "O1"},
                {"cart_id": "KG", "order_id": "OG"},
            ])
        with self.assertRaises(ValueError):
            self.app.checkout_cart_batch([
                {"cart_id": "K1", "order_id": "O1"},
                {"cart_id": "K2", "order_id": "O2"},
            ])
        self.assertEqual(self.app.path.read_bytes(), before)
        for cart_id in ("K1", "K2", "KG"):
            self.assertIsNotNone(self.app.get_cart(cart_id))

    def test_legacy_product_without_enabled_is_still_sellable(self):
        raw = {
            "products": {"L": {"sku": "L", "name": "Legacy", "price_cents": 7}},
            "carts": {"K1": {"cart_id": "K1", "lines": [{"sku": "L", "quantity": 2}]}},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        self.app.path.write_text(json.dumps(raw), encoding="utf-8")
        app = OrderDesk(self.root)
        result = app.checkout_cart_batch([{"cart_id": "K1", "order_id": "O1"}])
        self.assertEqual(result[0]["total_cents"], 14)
        # Unmanaged: no inventory record or reservation is fabricated.
        stored = json.loads(app.path.read_text(encoding="utf-8"))
        self.assertNotIn("inventory", stored)
        self.assertNotIn("O1", stored.get("reservations", {}))
        self.assertNotIn("enabled", stored["products"]["L"])

    def test_unmanaged_product_unlimited_and_not_managed(self):
        self.app.restock("T", 1)
        self.app.save_cart("K1", [{"sku": "U", "quantity": 1000}])
        self.app.save_cart("K2", [{"sku": "U", "quantity": 5}, {"sku": "T", "quantity": 1}])
        self.app.checkout_cart_batch([
            {"cart_id": "K1", "order_id": "O1"},
            {"cart_id": "K2", "order_id": "O2"},
        ])
        stock = self.app.stock("U")
        self.assertEqual(stock, {"sku": "U", "on_hand": None, "reserved": 0, "available": None})
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("U", raw["inventory"])
        self.assertEqual(raw["reservations"], {"O2": {"T": 1}})

    def test_invalid_batches_are_rejected_entirely(self):
        self.app.restock("T", 10)
        self.app.restock("C", 10)
        self.app.place("OEXIST", [{"sku": "C", "quantity": 1}])
        self.app.save_cart("K1", [{"sku": "T", "quantity": 1}])
        self.app.save_cart("K2", [{"sku": "C", "quantity": 1}])
        self.app.save_cart("K3", [{"sku": "T", "quantity": 1}])
        before = self.app.path.read_bytes()
        invalid = [
            [],
            None,
            {},
            "x",
            42,
            [None],
            ["x"],
            [42],
            [[]],
            [{"cart_id": "K1"}],
            [{"order_id": "O1"}],
            [{"cart_id": "K1", "order_id": 3}],
            [{"cart_id": None, "order_id": "O1"}],
            [{"cart_id": "K1", "order_id": "  "}],
            [{"cart_id": "  ", "order_id": "O1"}],
            [{"cart_id": "K1", "order_id": "O1"}, {"cart_id": " K1 ", "order_id": "O2"}],
            [{"cart_id": "K1", "order_id": "O1"}, {"cart_id": "K2", "order_id": " O1 "}],
            [{"cart_id": "MISSING", "order_id": "O1"}],
            [{"cart_id": "K1", "order_id": "OEXIST"}],
            # A bad later entry must discard the earlier valid ones, including a
            # last-entry failure.
            [{"cart_id": "K1", "order_id": "O1"}, {"cart_id": "MISSING", "order_id": "O2"}],
            [{"cart_id": "K1", "order_id": "O1"}, {"cart_id": "K2", "order_id": "OEXIST"}],
        ]
        for checkouts in invalid:
            with self.subTest(checkouts=checkouts):
                with self.assertRaises(ValueError):
                    self.app.checkout_cart_batch(checkouts)
        self.assertEqual(self.app.path.read_bytes(), before)
        # All carts survive, no orders appear, no history sequence consumed.
        for cart_id in ("K1", "K2", "K3"):
            self.app.get_cart(cart_id)
        with self.assertRaises(ValueError):
            self.app.get("O1")

    def test_extra_fields_are_ignored(self):
        self.app.restock("T", 5)
        self.app.save_cart("K3", [{"sku": "T", "quantity": 1}])
        result = self.app.checkout_cart_batch([
            {"cart_id": "K3", "order_id": "O3", "quantity": 99, "note": "x"},
        ])
        self.assertEqual(result[0]["order_id"], "O3")

    def test_last_entry_stock_failure_rejects_entire_batch(self):
        self.app.restock("T", 4)
        self.app.save_cart("K1", [{"sku": "T", "quantity": 2}])
        self.app.save_cart("K2", [{"sku": "T", "quantity": 3}])
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.checkout_cart_batch([
                {"cart_id": "K1", "order_id": "O1"},
                {"cart_id": "K2", "order_id": "O2"},
            ])
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.stock("T")["reserved"], 0)
        self.app.get_cart("K1")
        self.app.get_cart("K2")

    def test_case_sensitive_ids_and_independent_namespaces(self):
        self.app.restock("T", 10)
        self.app.save_cart("K1", [{"sku": "T", "quantity": 1}])
        self.app.save_cart("k1", [{"sku": "T", "quantity": 1}])
        # Same text may occupy both id spaces in one checkout.
        result = self.app.checkout_cart_batch([
            {"cart_id": "K1", "order_id": "X"},
            {"cart_id": "k1", "order_id": "x"},
        ])
        self.assertEqual([order["order_id"] for order in result], ["X", "x"])
        with self.assertRaises(ValueError):
            self.app.get_cart("K1")
        with self.assertRaises(ValueError):
            self.app.get_cart("k1")

    def test_same_text_allowed_for_cart_and_order_namespaces(self):
        self.app.restock("T", 10)
        # An existing order shares its id with a cart: neither blocks the other.
        self.app.place("SAME", [{"sku": "C", "quantity": 1}])
        self.app.save_cart("SAME", [{"sku": "T", "quantity": 2}])
        # The cart checks out into an order whose id equals the other cart's id.
        self.app.save_cart("OTHER", [{"sku": "T", "quantity": 1}])
        result = self.app.checkout_cart_batch([
            {"cart_id": "SAME", "order_id": "OTHER"},
        ])
        self.assertEqual(result[0]["order_id"], "OTHER")
        with self.assertRaises(ValueError):
            self.app.get_cart("SAME")
        self.assertEqual(self.app.get("SAME")["status"], "placed")

    def test_unselected_carts_orders_and_reservations_unchanged(self):
        self.app.restock("T", 10)
        self.app.save_cart("K1", [{"sku": "T", "quantity": 1}])
        self.app.save_cart("KEEP", [{"sku": "T", "quantity": 2}])
        self.app.place("OLD", [{"sku": "T", "quantity": 3}])
        old_order = self.app.get("OLD")
        self.app.checkout_cart_batch([{"cart_id": "K1", "order_id": "O1"}])
        self.assertEqual(self.app.get_cart("KEEP"),
                         {"cart_id": "KEEP", "lines": [{"sku": "T", "quantity": 2}]})
        self.assertEqual(self.app.get("OLD"), old_order)
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["reservations"]["OLD"], {"T": 3})
        self.assertEqual(raw["reservations"]["O1"], {"T": 1})

    def test_history_and_stock_events_chain_in_input_order(self):
        self.app.restock("T", 6)
        self.app.save_cart("K1", [{"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 1}])
        self.app.save_cart("K2", [{"sku": "T", "quantity": 3}])
        result = self.app.checkout_cart_batch([
            {"cart_id": "K2", "order_id": "O2"},
            {"cart_id": "K1", "order_id": "O1"},
        ])
        by_id = {order["order_id"]: order for order in result}
        # Each new order gets a complete history with one place event.
        for order_id in ("O1", "O2"):
            history = self.app.history(order_id)
            self.assertTrue(history["complete"])
            self.assertEqual(len(history["events"]), 1)
            self.assertEqual(history["events"][0]["action"], "place")
            self.assertEqual(history["events"][0]["sequence"], 1)
            self.assertEqual(history["events"][0]["result"], by_id[order_id])
        # Shared-product stock events follow input order: O2 reserved first
        # (before 0 -> after 3), then O1 chains (before 3 -> after 6).
        events = self.app.stock_history("T")["events"]
        places = [event for event in events if event["action"] == "place"]
        self.assertEqual([event["reference_id"] for event in places], ["O2", "O1"])
        self.assertEqual(places[0]["before"],
                         {"sku": "T", "on_hand": 6, "reserved": 0, "available": 6})
        self.assertEqual(places[0]["after"],
                         {"sku": "T", "on_hand": 6, "reserved": 3, "available": 3})
        self.assertEqual(places[1]["before"], places[0]["after"])
        self.assertEqual(places[1]["after"],
                         {"sku": "T", "on_hand": 6, "reserved": 6, "available": 0})
        self.assertEqual([event["sequence"] for event in events], [1, 2, 3])
        # Persistence across reopen.
        reopened = OrderDesk(self.root)
        self.assertEqual([order["order_id"] for order in reopened.list_orders()], ["O1", "O2"])
        self.assertEqual(reopened.stock("T")["reserved"], 6)
        for cart_id in ("K1", "K2"):
            with self.assertRaises(ValueError):
                reopened.get_cart(cart_id)

    def test_legacy_data_without_carts_treated_as_empty(self):
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("carts", raw)
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            OrderDesk(self.root).checkout_cart_batch([{"cart_id": "K1", "order_id": "O2"}])
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_failure_does_not_create_directory(self):
        missing_root = self.root / "does-not-exist"
        app = OrderDesk(missing_root)
        with self.assertRaises(ValueError):
            app.checkout_cart_batch([])
        with self.assertRaises(ValueError):
            app.checkout_cart_batch([{"cart_id": "K1", "order_id": "O1"}])
        self.assertFalse(missing_root.exists())

    def test_cli_success_failure_and_outer_array_independence(self):
        self.app.restock("T", 10)
        self.app.save_cart("K1", [{"sku": "T", "quantity": 1}])
        self.app.save_cart("K2", [{"sku": "T", "quantity": 1}])
        self.app.save_cart("K3", [{"sku": "T", "quantity": 1}])
        ok_payload = self.root / "ok.json"
        ok_payload.write_text(json.dumps({"checkouts": [
            {"cart_id": "K2", "order_id": "O2"},
            {"cart_id": "K1", "order_id": "O1"},
        ]}), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "checkout-cart-batch", str(ok_payload)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        orders = json.loads(ok.stdout)
        self.assertEqual([order["order_id"] for order in orders], ["O1", "O2"])
        # One bad entry rejects the whole batch, exit code 2 on stderr.
        bad_payload = self.root / "bad.json"
        bad_payload.write_text(json.dumps({"checkouts": [
            {"cart_id": "K3", "order_id": "O3"},
            {"cart_id": "MISSING", "order_id": "O4"},
        ]}), encoding="utf-8")
        bad = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "checkout-cart-batch", str(bad_payload)],
            text=True, capture_output=True)
        self.assertEqual(bad.returncode, 2, bad.stdout)
        self.assertEqual(bad.stdout, "")
        self.assertIn("error", json.loads(bad.stderr))
        self.app = OrderDesk(self.root)
        self.app.get_cart("K3")
        with self.assertRaises(ValueError):
            self.app.get("O3")
        # The outer JSON array still runs requests independently.
        outer = self.root / "outer.json"
        outer.write_text(json.dumps([
            {"checkouts": [{"cart_id": "K3", "order_id": "O3"}]},
            {"checkouts": [{"cart_id": "MISSING", "order_id": "O4"}]},
        ]), encoding="utf-8")
        stopped = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "checkout-cart-batch", str(outer)],
            text=True, capture_output=True)
        self.assertEqual(stopped.returncode, 2, stopped.stdout)
        self.assertIn("error", json.loads(stopped.stderr))
        # The first request succeeded and survived the second failure.
        self.assertEqual(OrderDesk(self.root).get("O3")["total_cents"], 100)


if __name__ == "__main__":
    unittest.main()
