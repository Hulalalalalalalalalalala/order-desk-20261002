import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk


class FulfillmentSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)
        self.app.add_product("U", "Unrestocked", 50)

    def _ship(self, order_id, lines, carrier="DHL", tracking_no="X-1"):
        self.app.place(order_id, lines)
        self.app.ship(order_id, carrier, tracking_no)

    def _line(self, result, sku):
        rows = [line for line in result["lines"] if line["sku"] == sku]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_empty_root_returns_zeros_and_does_not_create_directory(self):
        empty = self.root / "empty"
        app = OrderDesk(empty)
        result = app.fulfillment_summary()
        self.assertEqual(result, {
            "order_count": 0,
            "lines": [],
            "totals": {"shipped": 0, "pending": 0, "received": 0, "net": 0,
                       "shipped_cents": 0, "pending_cents": 0,
                       "received_cents": 0, "net_cents": 0},
        })
        self.assertFalse(empty.exists())

    def test_shape_only_three_top_level_keys_and_nine_line_keys(self):
        self.app.restock("T", 10)
        self._ship("O1", [{"sku": "T", "quantity": 2}])
        result = self.app.fulfillment_summary()
        self.assertEqual(set(result), {"order_count", "lines", "totals"})
        self.assertEqual(result["order_count"], 1)
        self.assertEqual(set(result["lines"][0]),
                         {"sku", "shipped", "pending", "received", "net",
                          "shipped_cents", "pending_cents",
                          "received_cents", "net_cents"})
        self.assertEqual(set(result["totals"]),
                         {"shipped", "pending", "received", "net",
                          "shipped_cents", "pending_cents",
                          "received_cents", "net_cents"})

    def test_only_shipped_or_delivered_orders_count(self):
        self.app.restock("T", 20)
        self.app.place("P1", [{"sku": "T", "quantity": 1}])
        self.app.place("P2", [{"sku": "T", "quantity": 1}])
        self.app.ship("P2", "DHL", "X-1")
        self.app.cancel("P1")
        self._ship("D1", [{"sku": "T", "quantity": 3}])
        self.app.confirm_delivery("D1", "Ann", "2026-10-01")
        # Carts never count.
        self.app.save_cart("K1", [{"sku": "T", "quantity": 9}])
        result = self.app.fulfillment_summary()
        self.assertEqual(result["order_count"], 2)
        self.assertEqual(result["totals"]["shipped"], 4)
        self.assertEqual(result["totals"]["shipped_cents"], 400)

    def test_quantities_and_amounts_merge_with_deal_prices(self):
        # O1: T x5 @100 and C x1 @200; O2: T x3 (two lines) @100.
        # T returns: 2 pending, 1 received.
        self.app.restock("T", 20)
        self.app.restock("C", 20)
        self._ship("O1", [{"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 1}])
        self._ship("O2", [{"sku": "T", "quantity": 2}, {"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "RP", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O2", "RR", [{"sku": "T", "quantity": 1}])
        self.app.receive_return("RR")
        result = self.app.fulfillment_summary()
        self.assertEqual(result["order_count"], 2)
        self.assertEqual([line["sku"] for line in result["lines"]], ["C", "T"])
        self.assertEqual(self._line(result, "C"), {
            "sku": "C", "shipped": 1, "pending": 0, "received": 0, "net": 1,
            "shipped_cents": 200, "pending_cents": 0,
            "received_cents": 0, "net_cents": 200,
        })
        self.assertEqual(self._line(result, "T"), {
            "sku": "T", "shipped": 8, "pending": 2, "received": 1, "net": 7,
            "shipped_cents": 800, "pending_cents": 200,
            "received_cents": 100, "net_cents": 700,
        })
        self.assertEqual(result["totals"], {
            "shipped": 9, "pending": 2, "received": 1, "net": 8,
            "shipped_cents": 1000, "pending_cents": 200,
            "received_cents": 100, "net_cents": 900,
        })

    def test_amounts_use_saved_deal_prices_not_catalog(self):
        self.app.restock("T", 10)
        self._ship("O1", [{"sku": "T", "quantity": 2}])
        self.app.reprice_products(
            [{"sku": "T", "expected_price_cents": 100, "price_cents": 150}])
        self._ship("O2", [{"sku": "T", "quantity": 1}])
        result = self.app.fulfillment_summary()
        # 2 x 100 + 1 x 150 = 350, not 3 x 150 = 450.
        self.assertEqual(self._line(result, "T")["shipped_cents"], 350)

    def test_zero_price_and_zero_net_rows_are_kept(self):
        self.app.add_product("F", "Free", 0)
        self.app.restock("T", 5)
        self._ship("O1", [{"sku": "F", "quantity": 4}])
        self._ship("O2", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O2", "R1", [{"sku": "T", "quantity": 3}])
        self.app.receive_return("R1")
        result = self.app.fulfillment_summary()
        f = self._line(result, "F")
        self.assertEqual(f["shipped"], 4)
        self.assertEqual(f["shipped_cents"], 0)
        t = self._line(result, "T")
        self.assertEqual(t, {
            "sku": "T", "shipped": 3, "pending": 0, "received": 3, "net": 0,
            "shipped_cents": 300, "pending_cents": 0,
            "received_cents": 300, "net_cents": 0,
        })

    def test_cancelled_returns_ignored_and_amended_returns_use_latest(self):
        self.app.restock("T", 20)
        self._ship("O1", [{"sku": "T", "quantity": 4}])
        self._ship("O2", [{"sku": "T", "quantity": 4}])
        self.app.record_return("O1", "RC", [{"sku": "T", "quantity": 4}])
        self.app.cancel_return("RC")
        self.app.record_return("O2", "RA", [{"sku": "T", "quantity": 3}])
        self.app.amend_return("RA", [{"sku": "T", "quantity": 3}],
                              [{"sku": "T", "quantity": 2}])
        result = self.app.fulfillment_summary()
        t = self._line(result, "T")
        self.assertEqual((t["shipped"], t["pending"], t["received"], t["net"]),
                         (8, 2, 0, 8))
        self.assertEqual((t["shipped_cents"], t["pending_cents"],
                          t["received_cents"], t["net_cents"]),
                         (800, 200, 0, 800))

    def test_paused_unmanaged_or_missing_catalog_products_do_not_block(self):
        self.app.restock("T", 5)
        self._ship("O1", [{"sku": "T", "quantity": 1}, {"sku": "U", "quantity": 2}])
        self.app.set_product_enabled("T", False)
        order = {"order_id": "OLD", "status": "delivered",
                 "lines": [{"sku": "GONE", "quantity": 7, "unit_price_cents": 10,
                            "subtotal_cents": 70}],
                 "total_cents": 70,
                 "shipment": {"carrier": "DHL", "tracking_no": "X-1"}}
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        data["orders"]["OLD"] = order
        self._write_raw(data)
        result = OrderDesk(self.root).fulfillment_summary()
        self.assertEqual(result["order_count"], 2)
        self.assertEqual([line["sku"] for line in result["lines"]],
                         ["GONE", "T", "U"])
        self.assertEqual(self._line(result, "GONE")["shipped_cents"], 70)
        self.assertEqual(self._line(result, "U")["shipped_cents"], 100)

    def test_legacy_missing_collections_read_as_empty(self):
        order = {"order_id": "OLD", "status": "shipped",
                 "lines": [{"sku": "T", "quantity": 5, "unit_price_cents": 100,
                            "subtotal_cents": 500}],
                 "total_cents": 500,
                 "shipment": {"carrier": "DHL", "tracking_no": "X-1"}}
        self._write_raw({"orders": {"OLD": order}})
        result = OrderDesk(self.root).fulfillment_summary()
        self.assertEqual(result["order_count"], 1)
        t = self._line(result, "T")
        self.assertEqual((t["shipped"], t["pending"], t["received"], t["net"]),
                         (5, 0, 0, 5))
        self.assertEqual(t["shipped_cents"], 500)

    def test_invalid_deal_price_rejects_the_whole_query(self):
        self.app.restock("T", 10)
        self._ship("GOOD", [{"sku": "T", "quantity": 1}])
        base = {"order_id": "BAD", "status": "shipped",
                "total_cents": 0,
                "shipment": {"carrier": "DHL", "tracking_no": "X-1"}}
        variants = [
            [{"sku": "T", "quantity": 5, "subtotal_cents": 0}],
            [{"sku": "T", "quantity": 5, "unit_price_cents": -1,
              "subtotal_cents": -5}],
            [{"sku": "T", "quantity": 5, "unit_price_cents": 1.5,
              "subtotal_cents": 7}],
            [{"sku": "T", "quantity": 5, "unit_price_cents": True,
              "subtotal_cents": 5}],
            [{"sku": "T", "quantity": 2, "unit_price_cents": 100,
              "subtotal_cents": 200},
             {"sku": "T", "quantity": 3, "unit_price_cents": 120,
              "subtotal_cents": 360}],
        ]
        for index, lines in enumerate(variants):
            order = dict(base)
            order["order_id"] = "BAD" + str(index)
            order["lines"] = [dict(line) for line in lines]
            data = json.loads(self.app.path.read_text(encoding="utf-8"))
            data["orders"][order["order_id"]] = order
            self._write_raw(data)
            with self.assertRaises(ValueError):
                OrderDesk(self.root).fulfillment_summary()
        # The good order alone still summarizes once the bad ones are gone.
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        for index in range(len(variants)):
            del data["orders"]["BAD" + str(index)]
        self._write_raw(data)
        result = OrderDesk(self.root).fulfillment_summary()
        self.assertEqual(result["order_count"], 1)

    def test_non_shipped_order_with_bad_price_is_ignored(self):
        # Invalid prices on an order outside the shipped/delivered set must
        # never surface.
        self.app.restock("T", 10)
        self._ship("GOOD", [{"sku": "T", "quantity": 1}])
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        data["orders"]["PLACED"] = {
            "order_id": "PLACED", "status": "placed",
            "lines": [{"sku": "T", "quantity": 5, "unit_price_cents": -1}],
            "total_cents": -5}
        self._write_raw(data)
        result = OrderDesk(self.root).fulfillment_summary()
        self.assertEqual(result["order_count"], 1)

    def test_query_does_not_write_or_consume_sequences(self):
        self.app.restock("T", 5)
        self._ship("O1", [{"sku": "T", "quantity": 2}])
        raw = self.app.path.read_bytes()
        first = self.app.fulfillment_summary()
        self.app.fulfillment_summary()
        self.assertEqual(self.app.path.read_bytes(), raw)
        self.assertEqual(first, self.app.fulfillment_summary())
        self.assertEqual(OrderDesk(self.root).fulfillment_summary(), first)
        self.assertEqual([e["sequence"] for e in self.app.history("O1")["events"]],
                         [1, 2])

    def test_cli_success_and_failure(self):
        self.app.restock("T", 10)
        self._ship("A", [{"sku": "T", "quantity": 3}])
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "fulfillment-summary"],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual(set(result), {"order_count", "lines", "totals"})
        self.assertEqual(result["order_count"], 1)
        self.assertEqual(result["lines"][0]["shipped_cents"], 300)
        # An input file is accepted when it carries no parameters; unknown
        # parameters fail through the existing error path with exit code 2.
        payload = self.root / "p.json"
        payload.write_text(json.dumps({}), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "fulfillment-summary", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        payload.write_text(json.dumps({"unexpected": 1}), encoding="utf-8")
        failed = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "fulfillment-summary", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2, failed.stdout)
        self.assertIn("error", json.loads(failed.stderr))
        # A bad stored price also exits 2.
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        data["orders"]["A"]["lines"][0]["unit_price_cents"] = True
        self._write_raw(data)
        failed = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "fulfillment-summary"],
            text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2, failed.stdout)
        self.assertIn("error", json.loads(failed.stderr))

    def _write_raw(self, data):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "data.json").write_text(json.dumps(data), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
