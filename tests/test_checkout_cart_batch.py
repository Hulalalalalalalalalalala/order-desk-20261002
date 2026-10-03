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

    def save_carts(self):
        self.app.save_cart("K1", [{"sku": "T", "quantity": 2}, {"sku": "Z", "quantity": 1}])
        self.app.save_cart("K2", [{"sku": "C", "quantity": 1}])

    def test_batch_settles_every_cart_and_returns_orders_sorted(self):
        self.app.restock("T", 5)
        self.save_carts()
        # Input order is not the sorted order; extra fields are ignored.
        result = self.app.checkout_cart_batch([
            {"cart_id": " K2 ", "order_id": "O2", "note": "ignored"},
            {"cart_id": "K1", "order_id": "O1"},
        ])
        self.assertEqual([order["order_id"] for order in result], ["O1", "O2"])
        self.assertEqual(result[0], {
            "order_id": "O1",
            "status": "placed",
            "lines": [
                {"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200},
                {"sku": "Z", "quantity": 1, "unit_price_cents": 0, "subtotal_cents": 0},
            ],
            "total_cents": 200,
        })
        self.assertEqual(result[1], {
            "order_id": "O2",
            "status": "placed",
            "lines": [
                {"sku": "C", "quantity": 1, "unit_price_cents": 200, "subtotal_cents": 200},
            ],
            "total_cents": 200,
        })
        self.assertEqual(result[0], self.app.get("O1"))
        self.assertEqual(result[1], self.app.get("O2"))
        # Every selected cart is gone; querying it now raises.
        for cart_id in ("K1", "K2"):
            with self.subTest(cart_id=cart_id):
                with self.assertRaises(ValueError):
                    self.app.get_cart(cart_id)
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 5, "reserved": 2, "available": 3})
        # Each order starts its own complete history with a single place event.
        for order in result:
            history = self.app.history(order["order_id"])
            self.assertTrue(history["complete"])
            self.assertEqual([event["action"] for event in history["events"]], ["place"])
            self.assertEqual(history["events"][0]["sequence"], 1)
            self.assertEqual(history["events"][0]["result"], order)

    def test_shared_stock_must_cover_combined_demand(self):
        self.app.restock("T", 5)
        self.app.save_cart("K1", [{"sku": "T", "quantity": 3}])
        self.app.save_cart("K2", [{"sku": "T", "quantity": 3}])
        before = self.app.path.read_bytes()
        # Two carts need six of five available: the whole batch is rejected.
        with self.assertRaises(ValueError):
            self.app.checkout_cart_batch([
                {"cart_id": "K1", "order_id": "O1"},
                {"cart_id": "K2", "order_id": "O2"},
            ])
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.get_cart("K1"),
                         {"cart_id": "K1", "lines": [{"sku": "T", "quantity": 3}]})
        self.assertEqual(self.app.get_cart("K2"),
                         {"cart_id": "K2", "lines": [{"sku": "T", "quantity": 3}]})
        self.app.restock("T", 1)
        # Six available: both orders are created and six are reserved.
        result = self.app.checkout_cart_batch([
            {"cart_id": "K1", "order_id": "O1"},
            {"cart_id": "K2", "order_id": "O2"},
        ])
        self.assertEqual([order["order_id"] for order in result], ["O1", "O2"])
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 6, "reserved": 6, "available": 0})
        # Shared-product stock events chain in input order.
        events = self.app.stock_history("T")["events"]
        places = [event for event in events if event["action"] == "place"]
        self.assertEqual([event["reference_id"] for event in places], ["O1", "O2"])
        self.assertEqual(places[0]["before"]["reserved"], 0)
        self.assertEqual(places[0]["after"]["reserved"], 3)
        self.assertEqual(places[1]["before"]["reserved"], 3)
        self.assertEqual(places[1]["after"]["reserved"], 6)
        self.assertEqual([event["sequence"] for event in places], [3, 4])

    def test_existing_reservations_reduce_the_shared_margin(self):
        self.app.restock("T", 6)
        # An order outside the batch already holds two of six.
        self.app.place("O0", [{"sku": "T", "quantity": 2}])
        self.app.save_cart("K1", [{"sku": "T", "quantity": 3}])
        self.app.save_cart("K2", [{"sku": "T", "quantity": 2}])
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.checkout_cart_batch([
                {"cart_id": "K1", "order_id": "O1"},
                {"cart_id": "K2", "order_id": "O2"},
            ])
        self.assertEqual(self.app.path.read_bytes(), before)
        self.app.release_reservation("O0", [{"sku": "T", "quantity": 1}])
        result = self.app.checkout_cart_batch([
            {"cart_id": "K1", "order_id": "O1"},
            {"cart_id": "K2", "order_id": "O2"},
        ])
        self.assertEqual(len(result), 2)
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 6, "reserved": 6, "available": 0})

    def test_unmanaged_products_are_unlimited_and_unreserved(self):
        self.app.save_cart("K1", [{"sku": "T", "quantity": 50}])
        self.app.save_cart("K2", [{"sku": "T", "quantity": 60}])
        result = self.app.checkout_cart_batch([
            {"cart_id": "K1", "order_id": "O1"},
            {"cart_id": "K2", "order_id": "O2"},
        ])
        self.assertEqual([order["total_cents"] for order in result], [5000, 6000])
        # Never auto-managed, no reservations, no stock events.
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": None, "reserved": 0, "available": None})
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("reservations", raw)
        self.assertNotIn("inventory", raw)

    def test_paused_or_missing_product_rejects_whole_batch(self):
        self.app.save_cart("K1", [{"sku": "C", "quantity": 1}])
        self.app.save_cart("K2", [{"sku": "T", "quantity": 1}])
        self.app.set_product_enabled("T", False)
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.checkout_cart_batch([
                {"cart_id": "K1", "order_id": "O1"},
                {"cart_id": "K2", "order_id": "O2"},
            ])
        self.assertEqual(self.app.path.read_bytes(), before)
        # A cart whose product left the catalog (legacy data) also rejects.
        self.app.set_product_enabled("T", True)
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        raw["carts"]["K2"]["lines"] = [{"sku": "X", "quantity": 1}]
        self.app.path.write_text(json.dumps(raw), encoding="utf-8")
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.checkout_cart_batch([
                {"cart_id": "K1", "order_id": "O1"},
                {"cart_id": "K2", "order_id": "O2"},
            ])
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_invalid_requests_leave_no_trace(self):
        self.app.restock("T", 5)
        self.app.restock("C", 3)
        self.save_carts()
        self.app.place("O9", [{"sku": "C", "quantity": 1}])
        before = self.app.path.read_bytes()
        bad_requests = [
            [],
            "x",
            42,
            None,
            {},
            ["x"],
            [42],
            [None],
            [{"cart_id": "K1"}],
            [{"order_id": "O1"}],
            [{"cart_id": "  ", "order_id": "O1"}],
            [{"cart_id": 3, "order_id": "O1"}],
            [{"cart_id": "K1", "order_id": "  "}],
            [{"cart_id": "K1", "order_id": None}],
            [{"cart_id": "K1", "order_id": "O1"}, {"cart_id": " K1 ", "order_id": "O2"}],
            [{"cart_id": "K1", "order_id": "O1"}, {"cart_id": "K2", "order_id": " O1 "}],
            [{"cart_id": "K9", "order_id": "O1"}],
            [{"cart_id": "K1", "order_id": "O9"}],
            # The failure is the last entry: earlier entries stay unapplied.
            [{"cart_id": "K1", "order_id": "O1"}, {"cart_id": "K9", "order_id": "O2"}],
            [{"cart_id": "K1", "order_id": "O1"}, {"cart_id": "K2", "order_id": "O9"}],
        ]
        for checkouts in bad_requests:
            with self.subTest(checkouts=checkouts):
                with self.assertRaises(ValueError):
                    self.app.checkout_cart_batch(checkouts)
        self.assertEqual(self.app.path.read_bytes(), before)
        # Carts, the existing order and its reservation are all intact.
        self.assertEqual(self.app.get_cart("K1")["lines"][0]["sku"], "T")
        self.assertEqual(self.app.get("O9")["status"], "placed")
        self.assertEqual(self.app.stock("C")["reserved"], 1)

    def test_failed_batch_never_creates_directory(self):
        fresh = self.root / "fresh"
        app = OrderDesk(fresh)
        for checkouts in (
            [],
            [{"cart_id": "K1", "order_id": "O1"}],
            [{"cart_id": "K1"}],
        ):
            with self.subTest(checkouts=checkouts):
                with self.assertRaises(ValueError):
                    app.checkout_cart_batch(checkouts)
        self.assertFalse(fresh.exists())

    def test_id_namespaces_and_normalization(self):
        self.save_carts()
        # Cart and order ids live in independent spaces and may share text.
        result = self.app.checkout_cart_batch([
            {"cart_id": "K1", "order_id": "K1"},
            {"cart_id": "K2", "order_id": "K2"},
        ])
        self.assertEqual([order["order_id"] for order in result], ["K1", "K2"])
        # Ids are case sensitive on both sides.
        self.app.save_cart("k1", [{"sku": "C", "quantity": 1}])
        self.assertEqual(self.app.get_cart("k1")["cart_id"], "k1")
        with self.assertRaises(ValueError):
            self.app.checkout_cart_batch([{"cart_id": "k1", "order_id": "K1"}])

    def test_unselected_carts_and_existing_orders_are_untouched(self):
        self.save_carts()
        self.app.save_cart("K3", [{"sku": "Z", "quantity": 4}])
        self.app.place("O0", [{"sku": "C", "quantity": 1}])
        before_cart = self.app.get_cart("K3")
        before_order = self.app.get("O0")
        self.app.checkout_cart_batch([{"cart_id": "K1", "order_id": "O1"}])
        self.assertEqual(self.app.get_cart("K3"), before_cart)
        self.assertEqual(self.app.get("O0"), before_order)

    def test_persistence_across_reopen(self):
        self.app.restock("T", 5)
        self.save_carts()
        self.app.save_cart("K3", [{"sku": "Z", "quantity": 1}])
        result = self.app.checkout_cart_batch([
            {"cart_id": "K1", "order_id": "O1"},
            {"cart_id": "K2", "order_id": "O2"},
        ])
        reopened = OrderDesk(self.root)
        self.assertEqual([reopened.get("O1"), reopened.get("O2")], result)
        for cart_id in ("K1", "K2"):
            with self.subTest(cart_id=cart_id):
                with self.assertRaises(ValueError):
                    reopened.get_cart(cart_id)
        self.assertEqual(reopened.get_cart("K3"),
                         {"cart_id": "K3", "lines": [{"sku": "Z", "quantity": 1}]})
        self.assertEqual(reopened.stock("T")["reserved"], 2)
        self.assertEqual(reopened.history("O1")["events"][0]["result"], result[0])

    def test_legacy_data_without_carts_reads_as_empty(self):
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("carts", raw)
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            OrderDesk(self.root).checkout_cart_batch([{"cart_id": "K1", "order_id": "O2"}])
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_legacy_product_without_enabled_flag_sells(self):
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("enabled", raw["products"]["T"])
        self.app.save_cart("K1", [{"sku": "T", "quantity": 1}])
        result = self.app.checkout_cart_batch([{"cart_id": "K1", "order_id": "O1"}])
        self.assertEqual(result[0]["total_cents"], 100)

    def test_cli_checkout_cart_batch(self):
        self.save_carts()
        payload = self.root / "batch.json"
        payload.write_text(json.dumps({"checkouts": [
            {"cart_id": "K2", "order_id": "O2"},
            {"cart_id": "K1", "order_id": "O1"},
        ]}), encoding="utf-8")
        out = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                              "checkout-cart-batch", str(payload)], text=True, capture_output=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        result = json.loads(out.stdout)
        self.assertEqual([order["order_id"] for order in result], ["O1", "O2"])
        again = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                                "checkout-cart-batch", str(payload)], text=True, capture_output=True)
        self.assertEqual(again.returncode, 2)
        self.assertEqual(again.stdout, "")
        self.assertIn("error", json.loads(again.stderr))

    def test_cli_outer_array_rows_stay_independent(self):
        self.save_carts()
        payload = self.root / "rows.json"
        payload.write_text(json.dumps([
            {"checkouts": [{"cart_id": "K1", "order_id": "O1"}]},
            {"checkouts": [{"cart_id": "K9", "order_id": "O2"}]},
        ]), encoding="utf-8")
        failed = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                                 "checkout-cart-batch", str(payload)], text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(failed.stdout, "")
        self.assertIn("unknown cart", json.loads(failed.stderr)["error"])
        # The first row succeeded and stays settled; K2 was never touched.
        self.assertEqual(self.app.get("O1")["status"], "placed")
        with self.assertRaises(ValueError):
            self.app.get_cart("K1")
        self.assertEqual(self.app.get_cart("K2"),
                         {"cart_id": "K2", "lines": [{"sku": "C", "quantity": 1}]})


if __name__ == "__main__":
    unittest.main()
