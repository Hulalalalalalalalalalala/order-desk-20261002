import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk


class ShipmentOrdersTests(unittest.TestCase):
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

    def test_shape_and_normalized_inputs(self):
        self.app.restock("T", 10)
        self._ship("O1", [{"sku": "T", "quantity": 2}])
        result = self.app.shipment_orders("  DHL  ", "  X-1  ")
        self.assertEqual(set(result), {"carrier", "tracking_no", "orders", "lines"})
        self.assertEqual(result["carrier"], "DHL")
        self.assertEqual(result["tracking_no"], "X-1")

    def test_no_match_still_returns_the_combination_with_empty_arrays(self):
        self.app.restock("T", 10)
        self._ship("O1", [{"sku": "T", "quantity": 2}])
        result = self.app.shipment_orders("DHL", "nope")
        self.assertEqual(result, {"carrier": "DHL", "tracking_no": "nope",
                                  "orders": [], "lines": []})

    def test_empty_root_does_not_create_directory(self):
        empty = self.root / "empty"
        app = OrderDesk(empty)
        result = app.shipment_orders("DHL", "X-1")
        self.assertEqual(result["orders"], [])
        self.assertEqual(result["lines"], [])
        self.assertFalse(empty.exists())

    def test_validation_rejects_non_string_or_blank(self):
        for bad in (None, 123, 1.5, b"DHL", ["DHL"], {"x": 1}, "   ", "\t\n"):
            with self.assertRaises(ValueError):
                self.app.shipment_orders(bad, "X-1")
            with self.assertRaises(ValueError):
                self.app.shipment_orders("DHL", bad)

    def test_orders_sorted_and_each_equals_order_progress(self):
        self.app.restock("T", 20)
        self.app.restock("C", 20)
        self._ship("B", [{"sku": "T", "quantity": 2}], "DHL", "SAME")
        self._ship("A", [{"sku": "C", "quantity": 1},
                         {"sku": "T", "quantity": 3}], " DHL ", " SAME ")
        self.app.confirm_delivery("A", "Ann", "2026-10-01")
        # A third order on a different shipment stays out.
        self._ship("Z", [{"sku": "T", "quantity": 1}], "UPS", "OTHER")
        result = self.app.shipment_orders("DHL", "SAME")
        self.assertEqual([row["order"]["order_id"] for row in result["orders"]], ["A", "B"])
        for row in result["orders"]:
            self.assertEqual(row, self.app.order_progress(row["order"]["order_id"]))
        # Delivered order included.
        self.assertEqual(result["orders"][0]["order"]["status"], "delivered")

    def test_matching_is_trimmed_exact_and_case_sensitive(self):
        self.app.restock("T", 10)
        self._ship("O1", [{"sku": "T", "quantity": 1}], "DHL", "AbC")
        self.assertEqual(self.app.shipment_orders("dhl", "AbC")["orders"], [])
        self.assertEqual(self.app.shipment_orders("DHL", "abc")["orders"], [])
        hit = self.app.shipment_orders(" DHL ", " AbC ")
        self.assertEqual([o["order"]["order_id"] for o in hit["orders"]], ["O1"])

    def test_only_shipped_or_delivered_orders_qualify(self):
        self.app.restock("T", 10)
        self.app.place("P1", [{"sku": "T", "quantity": 1}])
        self.app.place("P2", [{"sku": "T", "quantity": 1}])
        self.app.ship("P2", "DHL", "X-1")
        self.app.cancel("P1")
        # Legacy placed/cancelled orders carrying a shipment-shaped field.
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        data["orders"]["P1"]["shipment"] = {"carrier": "DHL", "tracking_no": "X-1"}
        placed = {"order_id": "P3", "status": "placed",
                  "lines": [{"sku": "T", "quantity": 1, "unit_price_cents": 100,
                             "subtotal_cents": 100}],
                  "total_cents": 100,
                  "shipment": {"carrier": "DHL", "tracking_no": "X-1"}}
        data["orders"]["P3"] = placed
        self._write_raw(data)
        result = OrderDesk(self.root).shipment_orders("DHL", "X-1")
        self.assertEqual([o["order"]["order_id"] for o in result["orders"]], ["P2"])

    def test_legacy_bad_shipments_are_skipped_not_fabricated(self):
        self.app.restock("T", 20)
        self._ship("GOOD", [{"sku": "T", "quantity": 1}], "DHL", "X-1")
        base = {"order_id": "BAD", "status": "shipped",
                "lines": [{"sku": "T", "quantity": 5, "unit_price_cents": 100,
                           "subtotal_cents": 500}],
                "total_cents": 500}
        variants = [
            None,
            "DHL X-1",
            {},
            {"carrier": "DHL"},
            {"tracking_no": "X-1"},
            {"carrier": 5, "tracking_no": "X-1"},
            {"carrier": "DHL", "tracking_no": None},
            {"carrier": "   ", "tracking_no": "X-1"},
            {"carrier": "DHL", "tracking_no": "\t"},
        ]
        for index, shipment in enumerate(variants):
            order = dict(base)
            order["order_id"] = "BAD" + str(index)
            order["lines"] = [dict(line) for line in base["lines"]]
            if shipment is not None:
                order["shipment"] = shipment
            data = json.loads(self.app.path.read_text(encoding="utf-8"))
            data["orders"][order["order_id"]] = order
            self._write_raw(data)
        result = OrderDesk(self.root).shipment_orders("DHL", "X-1")
        self.assertEqual([o["order"]["order_id"] for o in result["orders"]], ["GOOD"])

    def test_legacy_whitespace_shipment_is_trimmed_before_matching(self):
        order = {"order_id": "OLD", "status": "shipped",
                 "lines": [{"sku": "T", "quantity": 5, "unit_price_cents": 100,
                            "subtotal_cents": 500}],
                 "total_cents": 500,
                 "shipment": {"carrier": "  DHL  ", "tracking_no": " X-1 "}}
        self._write_raw({"orders": {"OLD": order}})
        app = OrderDesk(self.root)
        self.assertEqual([o["order"]["order_id"] for o in app.shipment_orders("DHL", "X-1")["orders"]],
                         ["OLD"])
        self.assertEqual(app.shipment_orders("  DHL", "X-1  ")["orders"][0],
                         app.order_progress("OLD"))

    def test_history_is_not_matched_only_current_shipment(self):
        self.app.restock("T", 10)
        self._ship("O1", [{"sku": "T", "quantity": 2}], "DHL", "OLD-1")
        self.app.correct_shipment(
            "O1", {"carrier": "DHL", "tracking_no": "OLD-1"},
            {"carrier": "UPS", "tracking_no": "NEW-2"})
        app = OrderDesk(self.root)
        self.assertEqual(app.shipment_orders("DHL", "OLD-1")["orders"], [])
        hit = app.shipment_orders("UPS", "NEW-2")
        self.assertEqual([o["order"]["order_id"] for o in hit["orders"]], ["O1"])
        # A later correction moves it again; the old combination loses it.
        self.app.correct_shipment(
            "O1", {"carrier": "UPS", "tracking_no": "NEW-2"},
            {"carrier": "UPS", "tracking_no": "NEW-3"})
        app = OrderDesk(self.root)
        self.assertEqual(app.shipment_orders("UPS", "NEW-2")["orders"], [])
        self.assertEqual([o["order"]["order_id"]
                          for o in app.shipment_orders("UPS", "NEW-3")["orders"]], ["O1"])

    def test_merged_lines_sum_fulfillment_fields_across_orders(self):
        # Two orders, eight units shipped overall: two pending return, one
        # received. Remaining returnable is five, net shipped seven.
        self.app.restock("T", 20)
        self.app.restock("C", 20)
        self._ship("O1", [{"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 1}],
                   "DHL", "SAME")
        self._ship("O2", [{"sku": "T", "quantity": 2}, {"sku": "T", "quantity": 1}],
                   "DHL", "SAME")
        self.app.record_return("O1", "RP", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O2", "RR", [{"sku": "T", "quantity": 1}])
        self.app.receive_return("RR")
        result = self.app.shipment_orders("DHL", "SAME")
        t = self._line(result, "T")
        self.assertEqual(set(t), {"sku", "shipped", "pending", "received", "remaining", "net"})
        self.assertEqual(t, {"sku": "T", "shipped": 8, "pending": 2, "received": 1,
                             "remaining": 5, "net": 7})
        c = self._line(result, "C")
        self.assertEqual(c, {"sku": "C", "shipped": 1, "pending": 0, "received": 0,
                             "remaining": 1, "net": 1})
        self.assertEqual([line["sku"] for line in result["lines"]], ["C", "T"])

    def test_lines_are_union_of_current_order_skus_with_zero_rows_kept(self):
        self.app.restock("T", 10)
        self.app.restock("C", 10)
        self._ship("O1", [{"sku": "T", "quantity": 3}], "DHL", "SAME")
        self._ship("O2", [{"sku": "C", "quantity": 2}], "DHL", "SAME")
        # Everything of O1 comes back: its totals are zero but the merged row
        # survives because O2 did not ship T.
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.app.receive_return("R1")
        result = self.app.shipment_orders("DHL", "SAME")
        self.assertEqual([line["sku"] for line in result["lines"]], ["C", "T"])
        self.assertEqual(self._line(result, "T"),
                         {"sku": "T", "shipped": 3, "pending": 0, "received": 3,
                          "remaining": 0, "net": 0})

    def test_cancelled_returns_ignored_and_amended_returns_use_latest(self):
        self.app.restock("T", 20)
        self._ship("O1", [{"sku": "T", "quantity": 4}], "DHL", "SAME")
        self._ship("O2", [{"sku": "T", "quantity": 4}], "DHL", "SAME")
        self.app.record_return("O1", "RC", [{"sku": "T", "quantity": 4}])
        self.app.cancel_return("RC")
        self.app.record_return("O2", "RA", [{"sku": "T", "quantity": 3}])
        self.app.amend_return("RA", [{"sku": "T", "quantity": 3}],
                              [{"sku": "T", "quantity": 2}])
        result = self.app.shipment_orders("DHL", "SAME")
        t = self._line(result, "T")
        self.assertEqual((t["shipped"], t["pending"], t["received"],
                          t["remaining"], t["net"]), (8, 2, 0, 6, 8))

    def test_paused_unmanaged_or_missing_catalog_products_do_not_block(self):
        self.app.restock("T", 5)
        self._ship("O1", [{"sku": "T", "quantity": 1}, {"sku": "U", "quantity": 2}],
                   "DHL", "SAME")
        self.app.set_product_enabled("T", False)
        result = self.app.shipment_orders("DHL", "SAME")
        self.assertEqual([line["sku"] for line in result["lines"]], ["T", "U"])
        order = {"order_id": "OLD", "status": "delivered",
                 "lines": [{"sku": "GONE", "quantity": 7, "unit_price_cents": 10,
                            "subtotal_cents": 70}],
                 "total_cents": 70,
                 "shipment": {"carrier": "DHL", "tracking_no": "SAME"}}
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        data["orders"]["OLD"] = order
        self._write_raw(data)
        result = OrderDesk(self.root).shipment_orders("DHL", "SAME")
        self.assertEqual([o["order"]["order_id"] for o in result["orders"]], ["O1", "OLD"])
        self.assertEqual(self._line(result, "GONE")["shipped"], 7)

    def test_legacy_missing_collections_read_as_empty(self):
        order = {"order_id": "OLD", "status": "shipped",
                 "lines": [{"sku": "T", "quantity": 5, "unit_price_cents": 100,
                            "subtotal_cents": 500}],
                 "total_cents": 500,
                 "shipment": {"carrier": "DHL", "tracking_no": "X-1"}}
        self._write_raw({"orders": {"OLD": order}})
        result = OrderDesk(self.root).shipment_orders("DHL", "X-1")
        self.assertEqual(len(result["orders"]), 1)
        self.assertEqual(result["orders"][0]["history"]["events"], [])
        self.assertEqual(self._line(result, "T")["shipped"], 5)

    def test_query_does_not_write_or_consume_sequences(self):
        self.app.restock("T", 5)
        self._ship("O1", [{"sku": "T", "quantity": 2}], "DHL", "X-1")
        raw = self.app.path.read_bytes()
        first = self.app.shipment_orders(" DHL ", " X-1 ")
        self.app.shipment_orders("DHL", "other")
        self.assertEqual(self.app.path.read_bytes(), raw)
        self.assertEqual(first, self.app.shipment_orders("DHL", "X-1"))
        self.assertEqual([e["sequence"] for e in self.app.history("O1")["events"]], [1, 2])
        self.assertEqual(OrderDesk(self.root).shipment_orders("DHL", "X-1"), first)

    def test_invalid_query_creates_no_directory(self):
        empty = self.root / "empty"
        app = OrderDesk(empty)
        with self.assertRaises(ValueError):
            app.shipment_orders(None, "X-1")
        with self.assertRaises(ValueError):
            app.shipment_orders("DHL", " ")
        self.assertFalse(empty.exists())

    def test_cli_success_failure_and_array(self):
        self.app.restock("T", 10)
        self._ship("A", [{"sku": "T", "quantity": 3}], "DHL", "SAME")
        self._ship("B", [{"sku": "T", "quantity": 1}], "DHL", "SAME")
        payload = self.root / "p.json"
        payload.write_text(json.dumps({"carrier": " DHL ", "tracking_no": " SAME "}),
                           encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "shipment-orders", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual(set(result), {"carrier", "tracking_no", "orders", "lines"})
        self.assertEqual(result["carrier"], "DHL")
        self.assertEqual([o["order"]["order_id"] for o in result["orders"]], ["A", "B"])
        self.assertEqual(result["lines"][0]["shipped"], 4)
        for bad in ({"carrier": 123, "tracking_no": "SAME"},
                    {"carrier": "DHL", "tracking_no": "  "}):
            payload.write_text(json.dumps(bad), encoding="utf-8")
            failed = subprocess.run(
                [sys.executable, "-m", "order_desk", "--root", str(self.root),
                 "shipment-orders", str(payload)],
                text=True, capture_output=True)
            self.assertEqual(failed.returncode, 2, failed.stdout)
            self.assertIn("error", json.loads(failed.stderr))
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([
            {"carrier": "DHL", "tracking_no": "SAME"},
            {"carrier": "UPS", "tracking_no": "NONE"},
        ]), encoding="utf-8")
        run = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "shipment-orders", str(batch)],
            text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        results = json.loads(run.stdout)
        self.assertEqual([o["order"]["order_id"] for o in results[0]["orders"]], ["A", "B"])
        self.assertEqual(results[1]["orders"], [])
        self.assertEqual(results[1]["lines"], [])

    def _write_raw(self, data):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "data.json").write_text(json.dumps(data), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
