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

    def _line(self, result, sku):
        rows = [line for line in result["lines"] if line["sku"] == sku]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def _ship(self, order_id, lines, carrier="DHL", tracking_no="X-1"):
        self.app.place(order_id, lines)
        self.app.ship(order_id, carrier, tracking_no)

    def _write_raw(self, data):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "data.json").write_text(json.dumps(data), encoding="utf-8")

    def test_shape_contains_only_documented_fields(self):
        self.app.restock("T", 10)
        self._ship("O1", [{"sku": "T", "quantity": 2}])
        result = self.app.fulfillment_summary()
        self.assertEqual(set(result), {"order_count", "lines", "totals"})
        self.assertEqual(result["order_count"], 1)
        for line in result["lines"]:
            self.assertEqual(set(line),
                             {"sku", "shipped", "pending", "received", "net",
                              "shipped_cents", "pending_cents",
                              "received_cents", "net_cents"})
        self.assertEqual(set(result["totals"]),
                         {"shipped", "pending", "received", "net",
                          "shipped_cents", "pending_cents",
                          "received_cents", "net_cents"})

    def test_empty_root_is_all_zero_and_creates_nothing(self):
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

    def test_only_shipped_and_delivered_orders_count(self):
        self.app.restock("T", 30)
        self.app.place("P1", [{"sku": "T", "quantity": 2}])
        self.app.place("P2", [{"sku": "T", "quantity": 4}])
        self.app.cancel("P2")
        self._ship("S1", [{"sku": "T", "quantity": 3}])
        self._ship("D1", [{"sku": "T", "quantity": 5}], tracking_no="X-2")
        self.app.confirm_delivery("D1", "Ann", "2026-10-01")
        # Carts never count.
        self.app.save_cart("K1", [{"sku": "T", "quantity": 9}])
        result = self.app.fulfillment_summary()
        self.assertEqual(result["order_count"], 2)
        t = self._line(result, "T")
        self.assertEqual((t["shipped"], t["pending"], t["received"], t["net"]),
                         (8, 0, 0, 8))
        self.assertEqual(t["shipped_cents"], 800)

    def test_lines_merge_across_orders_sorted_with_duplicate_skus(self):
        self.app.restock("T", 20)
        self.app.restock("C", 20)
        self._ship("O1", [
            {"sku": "T", "quantity": 2},
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 1},
        ])
        self._ship("O2", [{"sku": "C", "quantity": 2}], tracking_no="X-2")
        result = self.app.fulfillment_summary()
        self.assertEqual([line["sku"] for line in result["lines"]], ["C", "T"])
        c = self._line(result, "C")
        self.assertEqual(c["shipped"], 3)
        self.assertEqual(c["shipped_cents"], 600)
        t = self._line(result, "T")
        self.assertEqual(t["shipped"], 3)
        self.assertEqual(t["shipped_cents"], 300)

    def test_shipped_equals_ordered_not_stock_deduction(self):
        # Only 3 on hand cannot ship 5 under normal rules, but a legacy shipped
        # order records the deal regardless: shipped quantity must read as the
        # ordered quantity, never derived from inventory.
        order = {"order_id": "OLD", "status": "shipped",
                 "lines": [{"sku": "T", "quantity": 5, "unit_price_cents": 100,
                            "subtotal_cents": 500}],
                 "total_cents": 500}
        data = {"products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
                "inventory": {"T": {"on_hand": 0, "reserved": 0}},
                "orders": {"OLD": order}}
        self._write_raw(data)
        t = self._line(OrderDesk(self.root).fulfillment_summary(), "T")
        self.assertEqual(t["shipped"], 5)
        self.assertEqual(t["shipped_cents"], 500)

    def test_pending_received_cancelled_amended_returns_and_net(self):
        self.app.restock("T", 20)
        self._ship("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R0", [{"sku": "T", "quantity": 2}])
        self.app.cancel_return("R0")  # cancelled never counts
        self.app.record_return("O1", "RP", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "RR", [{"sku": "T", "quantity": 1}])
        self.app.amend_return("RR", [{"sku": "T", "quantity": 1}],
                              [{"sku": "T", "quantity": 3}])
        self.app.receive_return("RR")  # amended quantity is received
        result = self.app.fulfillment_summary()
        t = self._line(result, "T")
        # Shipped 5; pending 2; received 3; net only subtracts received.
        self.assertEqual((t["shipped"], t["pending"], t["received"], t["net"]),
                         (5, 2, 3, 2))
        self.assertEqual((t["shipped_cents"], t["pending_cents"],
                          t["received_cents"], t["net_cents"]),
                         (500, 200, 300, 200))
        self.assertEqual(result["totals"], {
            "shipped": 5, "pending": 2, "received": 3, "net": 2,
            "shipped_cents": 500, "pending_cents": 200,
            "received_cents": 300, "net_cents": 200,
        })

    def test_amounts_use_stored_deal_prices_not_catalog(self):
        self.app.restock("T", 20)
        self._ship("O1", [{"sku": "T", "quantity": 2}])  # 2 x 100 = 200
        self.app.reprice_products([
            {"sku": "T", "expected_price_cents": 100, "price_cents": 150}])
        self._ship("O2", [{"sku": "T", "quantity": 2}], tracking_no="X-2")
        result = self.app.fulfillment_summary()
        t = self._line(result, "T")
        self.assertEqual(t["shipped"], 4)
        # Old order priced at 100, new at 150: 200 + 300, not 4 x 150.
        self.assertEqual(t["shipped_cents"], 500)
        self.assertEqual(t["net_cents"], 500)
        self.assertEqual(result["totals"]["shipped_cents"], 500)

    def test_zero_price_items_and_zero_net_rows_are_kept(self):
        self.app.restock("T", 10)
        self.app.reprice_products([
            {"sku": "T", "expected_price_cents": 100, "price_cents": 0}])
        self._ship("O1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.app.receive_return("R1")
        result = self.app.fulfillment_summary()
        t = self._line(result, "T")
        self.assertEqual((t["shipped"], t["received"], t["net"]), (3, 3, 0))
        self.assertEqual((t["shipped_cents"], t["received_cents"], t["net_cents"]),
                         (0, 0, 0))

    def test_paused_unmanaged_or_missing_catalog_products_do_not_block(self):
        self.app.restock("T", 10)
        self._ship("O1", [{"sku": "T", "quantity": 1}, {"sku": "U", "quantity": 2}])
        self.app.set_product_enabled("T", False)
        result = self.app.fulfillment_summary()
        self.assertEqual([line["sku"] for line in result["lines"]], ["T", "U"])
        order = {"order_id": "OLD", "status": "delivered",
                 "lines": [{"sku": "GONE", "quantity": 7, "unit_price_cents": 10,
                            "subtotal_cents": 70}],
                 "total_cents": 70}
        data = {"products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
                "orders": {"OLD": order}}
        self._write_raw(data)
        result = OrderDesk(self.root).fulfillment_summary()
        self.assertEqual(result["order_count"], 1)
        g = self._line(result, "GONE")
        self.assertEqual((g["shipped"], g["shipped_cents"]), (7, 70))

    def test_returns_of_legacy_missing_collections_read_as_empty(self):
        order = {"order_id": "OLD", "status": "shipped",
                 "lines": [{"sku": "T", "quantity": 5, "unit_price_cents": 100,
                            "subtotal_cents": 500}],
                 "total_cents": 500}
        data = {"products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
                "orders": {"OLD": order}}
        self._write_raw(data)
        result = OrderDesk(self.root).fulfillment_summary()
        t = self._line(result, "T")
        self.assertEqual((t["shipped"], t["pending"], t["received"], t["net"]),
                         (5, 0, 0, 5))

    def test_invalid_deal_price_in_included_order_rejects_whole_summary(self):
        valid_order = {"order_id": "GOOD", "status": "shipped",
                       "lines": [{"sku": "T", "quantity": 1,
                                  "unit_price_cents": 100, "subtotal_cents": 100}],
                       "total_cents": 100}
        base = {"products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}}}
        for bad_price in (None, -1, 1.5, True):
            bad_order = {"order_id": "BAD", "status": "shipped",
                         "lines": [{"sku": "T", "quantity": 1,
                                    "unit_price_cents": bad_price,
                                    "subtotal_cents": 100}],
                         "total_cents": 100}
            data = dict(base, orders={"GOOD": valid_order, "BAD": bad_order})
            self._write_raw(data)
            with self.assertRaises(ValueError):
                OrderDesk(self.root).fulfillment_summary()

    def test_inconsistent_prices_for_same_sku_in_one_order_rejects(self):
        order = {"order_id": "OLD", "status": "shipped",
                 "lines": [
                     {"sku": "T", "quantity": 1, "unit_price_cents": 100,
                      "subtotal_cents": 100},
                     {"sku": "T", "quantity": 1, "unit_price_cents": 120,
                      "subtotal_cents": 120},
                 ],
                 "total_cents": 220}
        data = {"products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
                "orders": {"OLD": order}}
        self._write_raw(data)
        with self.assertRaises(ValueError):
            OrderDesk(self.root).fulfillment_summary()

    def test_bad_price_in_nonincluded_order_does_not_reject(self):
        good = {"order_id": "GOOD", "status": "shipped",
                "lines": [{"sku": "T", "quantity": 1, "unit_price_cents": 100,
                           "subtotal_cents": 100}],
                "total_cents": 100}
        bad = {"order_id": "BAD", "status": "placed",
               "lines": [{"sku": "T", "quantity": 1,
                          "unit_price_cents": True, "subtotal_cents": 1}],
               "total_cents": 1}
        data = {"products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
                "orders": {"GOOD": good, "BAD": bad}}
        self._write_raw(data)
        result = OrderDesk(self.root).fulfillment_summary()
        self.assertEqual(result["order_count"], 1)
        self.assertEqual(result["lines"][0]["shipped_cents"], 100)

    def test_totals_sum_every_line(self):
        self.app.restock("T", 20)
        self.app.restock("C", 20)
        self._ship("O1", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])
        self._ship("O2", [{"sku": "T", "quantity": 3}], tracking_no="X-2")
        result = self.app.fulfillment_summary()
        self.assertEqual(result["totals"], {
            "shipped": 6, "pending": 0, "received": 0, "net": 6,
            "shipped_cents": 5 * 100 + 1 * 200,
            "pending_cents": 0,
            "received_cents": 0,
            "net_cents": 5 * 100 + 1 * 200,
        })

    def test_query_does_not_write_or_consume_sequences(self):
        self.app.restock("T", 10)
        self._ship("O1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        raw = self.app.path.read_bytes()
        first = self.app.fulfillment_summary()
        self.app.fulfillment_summary()
        self.assertEqual(self.app.path.read_bytes(), raw)
        self.assertEqual(self.app.fulfillment_summary(), first)
        # Sequences untouched.
        self.assertEqual([e["sequence"] for e in self.app.history("O1")["events"]],
                         [1, 2, 3])

    def test_failed_query_does_not_write(self):
        order = {"order_id": "OLD", "status": "shipped",
                 "lines": [{"sku": "T", "quantity": 1}],
                 "total_cents": 0}
        data = {"products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
                "orders": {"OLD": order}}
        self._write_raw(data)
        raw = (self.root / "data.json").read_bytes()
        with self.assertRaises(ValueError):
            OrderDesk(self.root).fulfillment_summary()
        self.assertEqual((self.root / "data.json").read_bytes(), raw)

    def test_persists_across_reopen(self):
        self.app.restock("T", 10)
        self._ship("O1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.assertEqual(OrderDesk(self.root).fulfillment_summary(),
                         self.app.fulfillment_summary())

    def test_cli_success_without_input_file(self):
        self.app.restock("T", 5)
        self._ship("O1", [{"sku": "T", "quantity": 2}])
        run = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "fulfillment-summary"],
            text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout)
        self.assertEqual(set(result), {"order_count", "lines", "totals"})
        self.assertEqual(result["order_count"], 1)
        self.assertEqual(result["lines"][0]["shipped_cents"], 200)

    def test_cli_empty_payload_object_works(self):
        run = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "fulfillment-summary"],
            text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["order_count"], 0)

    def test_cli_failure_returns_2_with_error_json(self):
        order = {"order_id": "OLD", "status": "shipped",
                 "lines": [{"sku": "T", "quantity": 1}],
                 "total_cents": 0}
        data = {"products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
                "orders": {"OLD": order}}
        self._write_raw(data)
        run = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "fulfillment-summary"],
            text=True, capture_output=True)
        self.assertEqual(run.returncode, 2, run.stdout)
        self.assertIn("error", json.loads(run.stderr))

    def test_other_public_entrypoints_unchanged(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.ship("O1", "DHL", "1")
        # shipment-orders still carries order progress; summary stays separate.
        shipment = self.app.shipment_orders("DHL", "1")
        self.assertEqual([o["order"]["order_id"] for o in shipment["orders"]], ["O1"])
        progress = self.app.order_progress("O1")
        self.assertEqual(set(progress), {"order", "history", "lines"})


if __name__ == "__main__":
    unittest.main()
