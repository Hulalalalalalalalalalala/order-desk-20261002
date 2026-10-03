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

    def _ship(self, order_id, skus, carrier="DHL", tracking="X-1"):
        for sku, quantity in skus:
            self.app.restock(sku, quantity)
        self.app.place(order_id, [{"sku": sku, "quantity": q} for sku, q in skus])
        self.app.ship(order_id, carrier, tracking)

    def _line(self, result, sku):
        rows = [line for line in result["lines"] if line["sku"] == sku]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_result_shape_and_normalized_query_echoed(self):
        self._ship("O1", [("T", 2)])
        result = self.app.shipment_orders("  DHL ", " X-1\t")
        self.assertEqual(set(result), {"carrier", "tracking_no", "orders", "lines"})
        self.assertEqual(result["carrier"], "DHL")
        self.assertEqual(result["tracking_no"], "X-1")
        self.assertEqual([o["order"]["order_id"] for o in result["orders"]], ["O1"])

    def test_orders_match_order_progress_and_sort_by_order_id(self):
        self._ship("O2", [("T", 1)])
        self._ship("O1", [("C", 2)])
        result = self.app.shipment_orders("DHL", "X-1")
        self.assertEqual([o["order"]["order_id"] for o in result["orders"]], ["O1", "O2"])
        for entry in result["orders"]:
            order_id = entry["order"]["order_id"]
            self.assertEqual(entry, self.app.order_progress(order_id))

    def test_only_shipped_or_delivered_match(self):
        self.app.restock("T", 10)
        self.app.place("P1", [{"sku": "T", "quantity": 1}])
        self.app.place("P2", [{"sku": "T", "quantity": 1}])
        self.app.cancel("P2")
        self._ship("O1", [("T", 1)])
        self._ship("O2", [("T", 1)])
        self.app.confirm_delivery("O2", "Ann", "2026-10-01")
        result = self.app.shipment_orders("DHL", "X-1")
        self.assertEqual([o["order"]["order_id"] for o in result["orders"]], ["O1", "O2"])

    def test_matching_is_exact_and_case_sensitive_on_both_fields(self):
        self._ship("O1", [("T", 1)], carrier="DHL", tracking="X-1")
        for carrier, tracking in (("dhl", "X-1"), ("DHL", "x-1"), ("DH", "X-1"),
                                  ("DHL", "X-1X"), ("DHLX", "X-1")):
            result = self.app.shipment_orders(carrier, tracking)
            self.assertEqual(result["orders"], [])
            self.assertEqual(result["lines"], [])
        # Stored values were already trimmed at ship time; padded query matches.
        self.assertEqual(len(self.app.shipment_orders(" DHL ", " X-1 ")["orders"]), 1)

    def test_shared_tracking_number_returns_all_orders(self):
        self._ship("O1", [("T", 1)])
        self._ship("O2", [("T", 2)])
        self._ship("O3", [("T", 3)], tracking="OTHER")
        result = self.app.shipment_orders("DHL", "X-1")
        self.assertEqual([o["order"]["order_id"] for o in result["orders"]], ["O1", "O2"])

    def test_lines_merge_skus_across_orders_sorted(self):
        self._ship("O1", [("T", 3), ("C", 1)])
        self._ship("O2", [("T", 2), ("C", 1)])
        result = self.app.shipment_orders("DHL", "X-1")
        self.assertEqual([line["sku"] for line in result["lines"]], ["C", "T"])
        t = self._line(result, "T")
        self.assertEqual(t, {"sku": "T", "shipped": 5, "pending": 0,
                             "received": 0, "remaining": 5, "net": 5})
        c = self._line(result, "C")
        self.assertEqual(c, {"sku": "C", "shipped": 2, "pending": 0,
                             "received": 0, "remaining": 2, "net": 2})

    def test_lines_sum_pending_received_remaining_and_net(self):
        # Two orders ship eight of T in total; two pending, one received:
        # remaining five, net seven.
        self._ship("O1", [("T", 5)])
        self._ship("O2", [("T", 3)])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O2", "R2", [{"sku": "T", "quantity": 1}])
        self.app.receive_return("R2")
        t = self._line(self.app.shipment_orders("DHL", "X-1"), "T")
        self.assertEqual(t, {"sku": "T", "shipped": 8, "pending": 2,
                             "received": 1, "remaining": 5, "net": 7})

    def test_cancelled_return_not_counted_and_amended_uses_latest(self):
        self._ship("O1", [("T", 5)])
        self.app.record_return("O1", "R0", [{"sku": "T", "quantity": 2}])
        self.app.cancel_return("R0")
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.app.amend_return("R1", [{"sku": "T", "quantity": 3}],
                              [{"sku": "T", "quantity": 1}])
        t = self._line(self.app.shipment_orders("DHL", "X-1"), "T")
        self.assertEqual((t["pending"], t["received"], t["remaining"], t["net"]),
                         (1, 0, 4, 5))

    def test_corrected_shipment_moves_order_between_groups(self):
        self._ship("O1", [("T", 1)])
        self._ship("O2", [("T", 1)])
        self.app.correct_shipment(
            "O1", {"carrier": "DHL", "tracking_no": "X-1"},
            {"carrier": "UPS", "tracking_no": "U-9"})
        old = self.app.shipment_orders("DHL", "X-1")
        self.assertEqual([o["order"]["order_id"] for o in old["orders"]], ["O2"])
        new = self.app.shipment_orders("UPS", "U-9")
        self.assertEqual([o["order"]["order_id"] for o in new["orders"]], ["O1"])
        # History snapshots keep the old info but are never matched.
        self.assertEqual(self.app.history("O1")["events"][1]["action"], "ship")

    def test_no_match_returns_empty_arrays_with_query(self):
        self._ship("O1", [("T", 1)])
        result = self.app.shipment_orders("UPS", "X-1")
        self.assertEqual(result, {"carrier": "UPS", "tracking_no": "X-1",
                                  "orders": [], "lines": []})

    def test_empty_store_returns_empty_arrays(self):
        result = self.app.shipment_orders("DHL", "X-1")
        self.assertEqual(result, {"carrier": "DHL", "tracking_no": "X-1",
                                  "orders": [], "lines": []})

    def test_validation_rejects_non_string_or_blank(self):
        for bad in (None, 123, 1.5, b"DHL", ["DHL"], {"x": 1}, "", "   ", "\t\n"):
            with self.assertRaises(ValueError):
                self.app.shipment_orders(bad, "X-1")
            with self.assertRaises(ValueError):
                self.app.shipment_orders("DHL", bad)

    def test_legacy_orders_with_unusable_shipment_are_skipped(self):
        base = {"order_id": "OLD", "status": "shipped",
                "lines": [{"sku": "T", "quantity": 1, "unit_price_cents": 100,
                           "subtotal_cents": 100}],
                "total_cents": 100}
        variants = [
            dict(base),  # no shipment at all
            dict(base, order_id="OLD2", shipment="DHL/X-1"),  # not an object
            dict(base, order_id="OLD3", shipment={"carrier": "DHL"}),  # missing field
            dict(base, order_id="OLD4", shipment={"carrier": "DHL", "tracking_no": 7}),
            dict(base, order_id="OLD5", shipment={"carrier": "  ", "tracking_no": "X-1"}),
        ]
        good = dict(base, order_id="OLD6",
                    shipment={"carrier": " DHL ", "tracking_no": " X-1 "})
        data = {"orders": {o["order_id"]: o for o in variants + [good]}}
        self._write_raw(data)
        result = OrderDesk(self.root).shipment_orders("DHL", "X-1")
        self.assertEqual([o["order"]["order_id"] for o in result["orders"]], ["OLD6"])

    def test_query_does_not_write_or_consume_sequences(self):
        self._ship("O1", [("T", 1)])
        raw = self.app.path.read_bytes()
        first = self.app.shipment_orders("DHL", "X-1")
        self.app.shipment_orders(" DHL ", " X-1 ")
        self.app.shipment_orders("UPS", "none")
        self.assertEqual(self.app.path.read_bytes(), raw)
        self.assertEqual(first, self.app.shipment_orders("DHL", "X-1"))
        self.assertEqual([e["sequence"] for e in self.app.history("O1")["events"]],
                         [1, 2])

    def test_query_on_missing_root_creates_no_directory(self):
        empty = self.root / "empty"
        result = OrderDesk(empty).shipment_orders("DHL", "X-1")
        self.assertEqual(result["orders"], [])
        self.assertFalse(empty.exists())
        with self.assertRaises(ValueError):
            OrderDesk(self.root / "other").shipment_orders(" ", "X-1")
        self.assertFalse((self.root / "other").exists())

    def test_persists_across_reopen(self):
        self._ship("O1", [("T", 2)])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.assertEqual(OrderDesk(self.root).shipment_orders("DHL", "X-1"),
                         self.app.shipment_orders("DHL", "X-1"))

    def test_paused_or_unmanaged_products_do_not_block_query(self):
        self._ship("O1", [("T", 1)])
        self.app.set_product_enabled("T", False)
        result = self.app.shipment_orders("DHL", "X-1")
        self.assertEqual(len(result["orders"]), 1)
        # Order carrying a sku that left the catalog.
        order = {"order_id": "OLD", "status": "shipped",
                 "lines": [{"sku": "GONE", "quantity": 2, "unit_price_cents": 10,
                            "subtotal_cents": 20}],
                 "total_cents": 20,
                 "shipment": {"carrier": "DHL", "tracking_no": "X-1"}}
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        data["orders"]["OLD"] = order
        self._write_raw(data)
        result = OrderDesk(self.root).shipment_orders("DHL", "X-1")
        self.assertEqual([o["order"]["order_id"] for o in result["orders"]],
                         ["O1", "OLD"])
        self.assertEqual(self._line(result, "GONE")["shipped"], 2)

    def _write_raw(self, data):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "data.json").write_text(json.dumps(data), encoding="utf-8")

    def test_cli_success_and_failure(self):
        self._ship("O1", [("T", 2)])
        payload = self.root / "p.json"
        payload.write_text(json.dumps({"carrier": " DHL ", "tracking_no": "X-1"}),
                           encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "shipment-orders", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual(set(result), {"carrier", "tracking_no", "orders", "lines"})
        self.assertEqual(result["orders"][0]["order"]["order_id"], "O1")
        for bad in ({"carrier": "  ", "tracking_no": "X-1"},
                    {"carrier": "DHL"},
                    {"carrier": "DHL", "tracking_no": 5}):
            payload.write_text(json.dumps(bad), encoding="utf-8")
            failed = subprocess.run(
                [sys.executable, "-m", "order_desk", "--root", str(self.root),
                 "shipment-orders", str(payload)],
                text=True, capture_output=True)
            self.assertEqual(failed.returncode, 2, failed.stdout)
            self.assertIn("error", json.loads(failed.stderr))

    def test_cli_array_executes_each_item(self):
        self._ship("O1", [("T", 1)])
        self._ship("O2", [("T", 2)], carrier="UPS", tracking="U-9")
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([
            {"carrier": "DHL", "tracking_no": "X-1"},
            {"carrier": "UPS", "tracking_no": "U-9"},
            {"carrier": "NONE", "tracking_no": "N"},
        ]), encoding="utf-8")
        run = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "shipment-orders", str(batch)],
            text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        results = json.loads(run.stdout)
        self.assertEqual([r["orders"][0]["order"]["order_id"] for r in results[:2]],
                         ["O1", "O2"])
        self.assertEqual(results[2]["orders"], [])


if __name__ == "__main__":
    unittest.main()
