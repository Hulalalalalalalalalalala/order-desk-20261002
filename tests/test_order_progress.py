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
        self.app.add_product("M", "Mug", 300)

    def _by_sku(self, result):
        return {line["sku"]: line for line in result["lines"]}

    def test_result_shape_matches_get_and_history(self):
        self.app.restock("T", 10)
        order = self.app.place("O1", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])
        result = self.app.order_progress("O1")
        self.assertEqual(set(result), {"order", "history", "lines"})
        self.assertEqual(result["order"], self.app.get("O1"))
        self.assertEqual(result["history"], self.app.history("O1"))
        self.assertEqual(result["order"], order)
        self.assertEqual([line["sku"] for line in result["lines"]], ["C", "T"])
        for line in result["lines"]:
            self.assertEqual(set(line),
                             {"sku", "ordered", "reserved", "needed", "shipped",
                              "pending", "received", "remaining", "net"})

    def test_placed_managed_and_unmanaged_lines(self):
        # C is unmanaged: neither reservation field may ever be populated.
        self.app.restock("T", 10)
        self.app.place("O1", [
            {"sku": "T", "quantity": 2}, {"sku": "T", "quantity": 1},
            {"sku": "C", "quantity": 4},
        ])
        lines = self._by_sku(self.app.order_progress("O1"))
        self.assertEqual(lines["T"], {
            "sku": "T", "ordered": 3, "reserved": 3, "needed": 0,
            "shipped": 0, "pending": 0, "received": 0, "remaining": 0, "net": 0,
        })
        self.assertEqual(lines["C"], {
            "sku": "C", "ordered": 4, "reserved": 0, "needed": 0,
            "shipped": 0, "pending": 0, "received": 0, "remaining": 0, "net": 0,
        })

    def test_placed_gap_uses_actual_reservation_not_available_stock(self):
        # Five ordered, two reservations released: the gap is 2 even though
        # plenty of stock sits available -- needed never offsets availability.
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 5}])
        self.app.release_reservation("O1", [{"sku": "T", "quantity": 2}])
        line = self._by_sku(self.app.order_progress("O1"))["T"]
        self.assertEqual((line["ordered"], line["reserved"], line["needed"]), (5, 3, 2))
        # More restocking does not close the gap; only an actual top-up does.
        self.app.restock("T", 4)
        line = self._by_sku(self.app.order_progress("O1"))["T"]
        self.assertEqual((line["ordered"], line["reserved"], line["needed"]), (5, 3, 2))
        self.app.reserve_order("O1")
        line = self._by_sku(self.app.order_progress("O1"))["T"]
        self.assertEqual((line["ordered"], line["reserved"], line["needed"]), (5, 5, 0))

    def test_shipped_returns_example_remaining_and_net(self):
        # Five sent, two waiting to come back, one back in stock:
        # remaining returnable is 2 and net sent is 4.
        self.app.place("O1", [{"sku": "T", "quantity": 5}])
        self.app.restock("T", 5)
        self.app.reserve_order("O1")
        self.app.ship("O1", "DHL", "1")
        self.app.record_return("O1", "RP", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "RR", [{"sku": "T", "quantity": 1}])
        self.app.receive_return("RR")
        line = self._by_sku(self.app.order_progress("O1"))["T"]
        self.assertEqual(line, {
            "sku": "T", "ordered": 5, "reserved": 0, "needed": 0,
            "shipped": 5, "pending": 2, "received": 1, "remaining": 2, "net": 4,
        })

    def test_delivered_status_counts_as_shipped(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.ship("O1", "DHL", "1")
        self.app.confirm_delivery("O1", "Ann", "2026-10-03")
        line = self._by_sku(self.app.order_progress("O1"))["T"]
        self.assertEqual((line["shipped"], line["remaining"], line["net"]), (2, 2, 2))

    def test_cancelled_order_has_only_ordered_quantities(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.cancel("O1")
        line = self._by_sku(self.app.order_progress("O1"))["T"]
        self.assertEqual(line, {
            "sku": "T", "ordered": 2, "reserved": 0, "needed": 0,
            "shipped": 0, "pending": 0, "received": 0, "remaining": 0, "net": 0,
        })

    def test_stray_reservation_record_after_shipping_is_ignored(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.ship("O1", "DHL", "1")
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        raw["reservations"] = {"O1": {"T": 2}}
        self.app.path.write_text(json.dumps(raw), encoding="utf-8")
        line = self._by_sku(self.app.order_progress("O1"))["T"]
        self.assertEqual((line["shipped"], line["reserved"], line["needed"]), (2, 0, 0))

    def test_amend_reservation_and_return_operations_are_reflected(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 4}])
        self.app.amend("O1", [{"sku": "T", "quantity": 6}])
        line = self._by_sku(self.app.order_progress("O1"))["T"]
        self.assertEqual((line["ordered"], line["reserved"], line["needed"]), (6, 6, 0))
        self.app.release_reservation("O1", [{"sku": "T", "quantity": 2}])
        line = self._by_sku(self.app.order_progress("O1"))["T"]
        self.assertEqual((line["ordered"], line["reserved"], line["needed"]), (6, 4, 2))
        self.app.ship("O1", "DHL", "1")
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        line = self._by_sku(self.app.order_progress("O1"))["T"]
        self.assertEqual((line["pending"], line["remaining"], line["net"]), (3, 3, 6))
        self.app.amend_return("R1", [{"sku": "T", "quantity": 3}],
                              [{"sku": "T", "quantity": 1}])
        line = self._by_sku(self.app.order_progress("O1"))["T"]
        self.assertEqual((line["pending"], line["remaining"], line["net"]), (1, 5, 6))
        self.app.receive_return("R1")
        line = self._by_sku(self.app.order_progress("O1"))["T"]
        self.assertEqual((line["pending"], line["received"], line["remaining"], line["net"]),
                         (0, 1, 5, 5))
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 2}])
        self.app.cancel_return("R2")
        line = self._by_sku(self.app.order_progress("O1"))["T"]
        self.assertEqual((line["pending"], line["received"], line["remaining"], line["net"]),
                         (0, 1, 5, 5))

    def test_duplicate_skus_merge_and_zero_rows_keep_all_fields(self):
        self.app.restock("M", 2)
        self.app.place("O1", [
            {"sku": "M", "quantity": 1}, {"sku": "M", "quantity": 1},
            {"sku": "T", "quantity": 1},
        ])
        self.app.release_reservation("O1", [{"sku": "M", "quantity": 1}])
        lines = self._by_sku(self.app.order_progress("O1"))
        self.assertEqual(lines["M"]["ordered"], 2)
        self.assertEqual(lines["M"]["needed"], 1)
        zero = lines["T"]
        self.assertEqual([zero[k] for k in
                          ("reserved", "needed", "shipped", "pending", "received", "remaining", "net")],
                         [0, 0, 0, 0, 0, 0, 0])

    def test_paused_and_catalog_missing_products_still_query(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}, {"sku": "C", "quantity": 2}])
        self.app.set_product_enabled("C", False)
        lines = self._by_sku(self.app.order_progress("O1"))
        self.assertEqual(lines["C"]["ordered"], 2)
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        del raw["products"]["C"]
        self.app.path.write_text(json.dumps(raw), encoding="utf-8")
        lines = self._by_sku(self.app.order_progress("O1"))
        self.assertEqual(lines["C"]["ordered"], 2)
        self.assertEqual(lines["C"]["reserved"], 0)

    def test_legacy_data_without_collections_reads_as_empty(self):
        order = {"order_id": "OLD", "status": "shipped",
                 "lines": [{"sku": "T", "quantity": 2}, {"sku": "T", "quantity": 3}],
                 "total_cents": 500}
        data = {"products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
                "orders": {"OLD": order}}
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        result = OrderDesk(self.root).order_progress("OLD")
        self.assertEqual(result["history"],
                         {"order_id": "OLD", "status": "shipped", "complete": False, "events": []})
        line = result["lines"][0]
        self.assertEqual(line, {
            "sku": "T", "ordered": 5, "reserved": 0, "needed": 0,
            "shipped": 5, "pending": 0, "received": 0, "remaining": 5, "net": 5,
        })

    def test_legacy_returns_without_receipts_are_pending(self):
        order = {"order_id": "OLD", "status": "shipped",
                 "lines": [{"sku": "T", "quantity": 5}], "total_cents": 500}
        data = {"products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
                "orders": {"OLD": order},
                "returns": {"OLD": [
                    {"order_id": "OLD", "return_id": "L1",
                     "lines": [{"sku": "T", "quantity": 2}]}]}}
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        line = OrderDesk(self.root).order_progress("OLD")["lines"][0]
        self.assertEqual((line["pending"], line["received"], line["remaining"], line["net"]),
                         (2, 0, 3, 5))

    def test_legacy_managed_placed_order_without_reservation_record(self):
        order = {"order_id": "OLD", "status": "placed",
                 "lines": [{"sku": "T", "quantity": 3}], "total_cents": 300}
        data = {"products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
                "inventory": {"T": {"on_hand": 4, "reserved": 0}},
                "orders": {"OLD": order}}
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        line = OrderDesk(self.root).order_progress("OLD")["lines"][0]
        self.assertEqual((line["ordered"], line["reserved"], line["needed"]), (3, 0, 3))

    def test_input_validation(self):
        self.app.place("O1", [{"sku": "C", "quantity": 1}])
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
        self.app.order_progress("O1")
        self.app.order_progress("O1")
        self.assertEqual(self.app.path.read_bytes(), raw)
        self.assertEqual(len(self.app.history("O1")["events"]), 1)
        # Reopening with unchanged data gives the same result.
        self.assertEqual(OrderDesk(self.root).order_progress("O1"),
                         self.app.order_progress("O1"))

    def test_unknown_order_creates_no_directory(self):
        empty = self.root / "empty"
        app = OrderDesk(empty)
        with self.assertRaises(ValueError):
            app.order_progress("ghost")
        self.assertFalse(empty.exists())

    def test_cli_success_failure_and_array(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.place("O2", [{"sku": "C", "quantity": 1}])
        payload = self.root / "p.json"
        payload.write_text(json.dumps({"order_id": " O1 "}), encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                             "order-progress", str(payload)], text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual(set(result), {"order", "history", "lines"})
        self.assertEqual(result["order"]["order_id"], "O1")
        for bad in ("unknown", "   "):
            payload.write_text(json.dumps({"order_id": bad}), encoding="utf-8")
            failed = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                                     "order-progress", str(payload)], text=True, capture_output=True)
            self.assertEqual(failed.returncode, 2, failed.stdout)
            self.assertIn("error", json.loads(failed.stderr))
        # An outer array executes item by item; an unknown middle id leaves the
        # whole batch a failure (nothing is printed) but data is unchanged.
        payload.write_text(json.dumps([
            {"order_id": "O1"}, {"order_id": "missing"}, {"order_id": "O2"},
        ]), encoding="utf-8")
        run = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                              "order-progress", str(payload)], text=True, capture_output=True)
        self.assertEqual(run.returncode, 2, run.stdout)
        self.assertEqual([e["action"] for e in OrderDesk(self.root).history("O1")["events"]],
                         ["place"])

if __name__ == "__main__":
    unittest.main()
