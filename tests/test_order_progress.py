import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk


class OrderProgressTests(unittest.TestCase):
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

    def test_result_shape_and_order_history_embedded_verbatim(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        result = self.app.order_progress("O1")
        self.assertEqual(set(result), {"order", "history", "lines"})
        self.assertEqual(result["order"], self.app.get("O1"))
        self.assertEqual(result["history"], self.app.history("O1"))
        for line in result["lines"]:
            self.assertEqual(set(line),
                             {"sku", "ordered", "reserved", "needed", "shipped",
                              "pending", "received", "remaining", "net"})

    def test_placed_order_merges_duplicate_skus_sorted_with_zero_values(self):
        self.app.restock("T", 10)
        self.app.restock("C", 3)
        self.app.place("O1", [
            {"sku": "T", "quantity": 2},
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 1},
        ])
        result = self.app.order_progress("O1")
        self.assertEqual([line["sku"] for line in result["lines"]], ["C", "T"])
        t = self._line(result, "T")
        self.assertEqual(t, {"sku": "T", "ordered": 3, "reserved": 3, "needed": 0,
                             "shipped": 0, "pending": 0, "received": 0,
                             "remaining": 0, "net": 0})
        c = self._line(result, "C")
        self.assertEqual(c["ordered"], 1)
        self.assertEqual(c["reserved"], 1)
        self.assertEqual(c["needed"], 0)

    def test_placed_unmanaged_sku_has_no_reservation_or_need(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 2}, {"sku": "U", "quantity": 4}])
        result = self.app.order_progress("O1")
        u = self._line(result, "U")
        self.assertEqual(u["ordered"], 4)
        self.assertEqual(u["reserved"], 0)
        self.assertEqual(u["needed"], 0)
        t = self._line(result, "T")
        self.assertEqual((t["reserved"], t["needed"]), (2, 0))

    def test_placed_partial_reservation_needed_does_not_net_available_stock(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 5}])
        self.app.release_reservation("O1", [{"sku": "T", "quantity": 2}])
        # Plenty is available again, but needed is measured against this order's
        # own reservation only.
        self.assertEqual(self.app.stock("T")["available"], 7)
        result = self.app.order_progress("O1")
        t = self._line(result, "T")
        self.assertEqual((t["ordered"], t["reserved"], t["needed"]), (5, 3, 2))

    def test_product_managed_after_place_then_reserved_is_reflected(self):
        # Placed while U is unmanaged: nothing reserved.
        self.app.place("O1", [{"sku": "U", "quantity": 4}])
        before = self._line(self.app.order_progress("O1"), "U")
        self.assertEqual((before["reserved"], before["needed"]), (0, 0))
        self.app.restock("U", 10)
        mid = self._line(self.app.order_progress("O1"), "U")
        self.assertEqual((mid["reserved"], mid["needed"]), (0, 4))
        self.app.reserve_order("O1")
        after = self._line(self.app.order_progress("O1"), "U")
        self.assertEqual((after["reserved"], after["needed"]), (4, 0))

    def test_amend_is_reflected(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 5}])
        self.app.amend("O1", [{"sku": "T", "quantity": 2}])
        t = self._line(self.app.order_progress("O1"), "T")
        self.assertEqual((t["ordered"], t["reserved"], t["needed"]), (2, 2, 0))

    def test_cancelled_order_has_zero_reservation_fields(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.cancel("O1")
        result = self.app.order_progress("O1")
        t = self._line(result, "T")
        self.assertEqual((t["ordered"], t["reserved"], t["needed"], t["shipped"]),
                         (3, 0, 0, 0))
        self.assertEqual(result["order"]["status"], "cancelled")
        self.assertEqual(result["history"]["status"], "cancelled")

    def test_shipped_example_pending_received_remaining_and_net(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 5}])
        self.app.ship("O1", "DHL", "X-1")
        self.app.record_return("O1", "RP", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "RR", [{"sku": "T", "quantity": 1}])
        self.app.receive_return("RR")
        result = self.app.order_progress("O1")
        t = self._line(result, "T")
        # Shipped five, two pending, one received: remaining two, net four.
        self.assertEqual(t, {"sku": "T", "ordered": 5, "reserved": 0, "needed": 0,
                             "shipped": 5, "pending": 2, "received": 1,
                             "remaining": 2, "net": 4})

    def test_delivered_status_counts_as_shipped(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.ship("O1", "DHL", "X-1")
        self.app.confirm_delivery("O1", "Ann", "2026-10-01")
        t = self._line(self.app.order_progress("O1"), "T")
        self.assertEqual(t["shipped"], 3)
        self.assertEqual(t["remaining"], 3)
        self.assertEqual(t["net"], 3)

    def test_shipped_without_returns(self):
        self.app.restock("C", 5)
        self.app.place("O1", [{"sku": "C", "quantity": 2}])
        self.app.ship("O1", "DHL", "1")
        c = self._line(self.app.order_progress("O1"), "C")
        self.assertEqual((c["shipped"], c["pending"], c["received"],
                          c["remaining"], c["net"]), (2, 0, 0, 2, 2))

    def test_cancelled_return_is_excluded_and_amended_return_uses_latest_lines(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 5}])
        self.app.ship("O1", "DHL", "1")
        self.app.record_return("O1", "R0", [{"sku": "T", "quantity": 2}])
        self.app.cancel_return("R0")
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.app.amend_return("R1", [{"sku": "T", "quantity": 3}],
                              [{"sku": "T", "quantity": 1}])
        t = self._line(self.app.order_progress("O1"), "T")
        self.assertEqual((t["pending"], t["received"], t["remaining"], t["net"]),
                         (1, 0, 4, 5))
        self.app.receive_return("R1")
        t = self._line(self.app.order_progress("O1"), "T")
        self.assertEqual((t["pending"], t["received"], t["remaining"], t["net"]),
                         (0, 1, 4, 4))

    def test_duplicate_skus_inside_a_return_merge(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 5}])
        self.app.ship("O1", "DHL", "1")
        # Registrations already store merged lines, but two records still add.
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        t = self._line(self.app.order_progress("O1"), "T")
        self.assertEqual((t["pending"], t["remaining"]), (2, 3))

    def test_paused_or_missing_catalog_products_do_not_block_query(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 1}, {"sku": "U", "quantity": 2}])
        self.app.set_product_enabled("T", False)
        result = self.app.order_progress("O1")
        self.assertEqual([line["sku"] for line in result["lines"]], ["T", "U"])
        # Legacy order carrying a sku absent from the current catalog.
        order = {"order_id": "OLD", "status": "placed",
                 "lines": [{"sku": "GONE", "quantity": 7, "unit_price_cents": 10,
                            "subtotal_cents": 70}],
                 "total_cents": 70}
        data = {"products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
                "orders": {"OLD": order}}
        self._write_raw(data)
        line = self._line(OrderDesk(self.root).order_progress("OLD"), "GONE")
        self.assertEqual((line["ordered"], line["reserved"], line["needed"]), (7, 0, 0))

    def test_legacy_missing_collections_read_as_empty_not_derived_from_history(self):
        # Shipped legacy order with returns but no receipts bucket and no
        # history: returns are pending, history is empty and not fabricated.
        order = {"order_id": "OLD", "status": "shipped",
                 "lines": [{"sku": "T", "quantity": 5, "unit_price_cents": 100,
                            "subtotal_cents": 500}],
                 "total_cents": 500}
        data = {"products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
                "orders": {"OLD": order},
                        "returns": {"OLD": [
                            {"order_id": "OLD", "return_id": "L1",
                             "lines": [{"sku": "T", "quantity": 2}]}]}}
        self._write_raw(data)
        app = OrderDesk(self.root)
        result = app.order_progress("OLD")
        t = self._line(result, "T")
        self.assertEqual((t["shipped"], t["pending"], t["received"],
                          t["remaining"], t["net"]), (5, 2, 0, 3, 5))
        self.assertEqual(result["history"],
                         {"order_id": "OLD", "status": "shipped",
                          "complete": False, "events": []})

    def test_legacy_stray_reservation_for_unmanaged_sku_reads_zero(self):
        order = {"order_id": "OLD", "status": "placed",
                 "lines": [{"sku": "T", "quantity": 3, "unit_price_cents": 100,
                            "subtotal_cents": 300}],
                 "total_cents": 300}
        data = {"products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
                "orders": {"OLD": order},
                "reservations": {"OLD": {"T": 3}}}
        self._write_raw(data)
        t = self._line(OrderDesk(self.root).order_progress("OLD"), "T")
        self.assertEqual((t["reserved"], t["needed"]), (0, 0))

    def test_legacy_receipt_counts_as_received(self):
        order = {"order_id": "OLD", "status": "delivered",
                 "lines": [{"sku": "T", "quantity": 5, "unit_price_cents": 100,
                            "subtotal_cents": 500}],
                 "total_cents": 500}
        receipt = {"order_id": "OLD", "return_id": "L1",
                   "lines": [{"sku": "T", "quantity": 1,
                              "before": {"sku": "T", "on_hand": 4, "reserved": 0, "available": 4},
                              "after": {"sku": "T", "on_hand": 5, "reserved": 0, "available": 5}}]}
        data = {"products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
                                "inventory": {"T": {"on_hand": 5, "reserved": 0}},
                                "orders": {"OLD": order},
                                "returns": {"OLD": [
                                    {"order_id": "OLD", "return_id": "L1",
                                     "lines": [{"sku": "T", "quantity": 1}]}]},
                                "return_receipts": {"L1": receipt}}
        self._write_raw(data)
        t = self._line(OrderDesk(self.root).order_progress("OLD"), "T")
        self.assertEqual((t["shipped"], t["pending"], t["received"],
                          t["remaining"], t["net"]), (5, 0, 1, 4, 4))

    def _write_raw(self, data):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "data.json").write_text(json.dumps(data), encoding="utf-8")

    def test_validation_trims_and_is_case_sensitive(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        for bad in (None, 123, 1.5, b"O1", ["O1"], {"x": 1}, "   ", "\t\n"):
            with self.assertRaises(ValueError):
                self.app.order_progress(bad)
        with self.assertRaises(ValueError):
            self.app.order_progress("unknown")
        self.assertEqual(self.app.order_progress("  O1  ")["order"]["order_id"], "O1")
        with self.assertRaises(ValueError):
            self.app.order_progress("o1")

    def test_query_does_not_write_or_consume_sequences(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        raw = self.app.path.read_bytes()
        first = self.app.order_progress("O1")
        self.app.order_progress(" O1 ")
        self.assertEqual(self.app.path.read_bytes(), raw)
        second = self.app.order_progress("O1")
        self.assertEqual(first, second)
        # History sequence untouched.
        self.assertEqual([e["sequence"] for e in self.app.history("O1")["events"]], [1])

    def test_unknown_order_creates_no_directory(self):
        empty = self.root / "empty"
        app = OrderDesk(empty)
        with self.assertRaises(ValueError):
            app.order_progress("ghost")
        self.assertFalse(empty.exists())

    def test_persists_across_reopen(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 5}])
        self.app.ship("O1", "DHL", "1")
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        self.assertEqual(OrderDesk(self.root).order_progress("O1"),
                         self.app.order_progress("O1"))

    def test_cli_order_progress_success_and_failure(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        payload = self.root / "p.json"
        payload.write_text(json.dumps({"order_id": " O1 "}), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "order-progress", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual(set(result), {"order", "history", "lines"})
        self.assertEqual(result["order"]["order_id"], "O1")
        self.assertEqual(result["lines"][0]["reserved"], 2)
        for bad in ("unknown", "   "):
            payload.write_text(json.dumps({"order_id": bad}), encoding="utf-8")
            failed = subprocess.run(
                [sys.executable, "-m", "order_desk", "--root", str(self.root),
                 "order-progress", str(payload)],
                text=True, capture_output=True)
            self.assertEqual(failed.returncode, 2, failed.stdout)
            self.assertIn("error", json.loads(failed.stderr))

    def test_cli_array_executes_each_item(self):
        self.app.restock("T", 10)
        self.app.place("A", [{"sku": "T", "quantity": 1}])
        self.app.place("B", [{"sku": "T", "quantity": 2}])
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([{"order_id": "A"}, {"order_id": "B"}]),
                        encoding="utf-8")
        run = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "order-progress", str(batch)],
            text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        results = json.loads(run.stdout)
        self.assertEqual([r["order"]["order_id"] for r in results], ["A", "B"])
        self.assertEqual(results[1]["lines"][0]["ordered"], 2)


if __name__ == "__main__":
    unittest.main()
