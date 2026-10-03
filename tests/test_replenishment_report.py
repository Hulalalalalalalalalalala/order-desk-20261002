import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk


class ReplenishmentReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)
        self.app.add_product("U", "Unrestocked", 50)

    def test_numbers_example_and_entry_shape(self):
        # Orders demand T while it is still unmanaged, then a single restock
        # leaves exactly one unit available and no reservations.
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.place("O2", [{"sku": "T", "quantity": 2}])
        self.app.place("O3", [{"sku": "T", "quantity": 2}, {"sku": "U", "quantity": 1}])
        self.app.restock("T", 1)
        # Shipped before management: no reservation to deduct, stock unchanged.
        self.app.ship("O3", "DHL", "TRK-O3")
        self.app.record_return("O3", "R1", [{"sku": "T", "quantity": 2}])
        result = self.app.replenishment_report()
        self.assertEqual(len(result), 1)
        row = result[0]
        self.assertEqual(set(row), {
            "sku", "stock", "needed", "pending", "shortfall",
            "projected_shortfall", "orders", "returns",
        })
        self.assertEqual(row["sku"], "T")
        self.assertEqual(row["stock"], {"sku": "T", "on_hand": 1, "reserved": 0, "available": 1})
        self.assertEqual(row["stock"], self.app.stock("T"))
        self.assertEqual(row["needed"], 5)
        self.assertEqual(row["pending"], 2)
        self.assertEqual(row["shortfall"], 4)
        self.assertEqual(row["projected_shortfall"], 2)
        self.assertEqual(row["orders"], [
            {"order_id": "O1", "quantity": 3, "reserved": 0, "unreserved": 3},
            {"order_id": "O2", "quantity": 2, "reserved": 0, "unreserved": 2},
        ])
        for detail in row["orders"]:
            self.assertEqual(set(detail), {"order_id", "quantity", "reserved", "unreserved"})
        self.assertEqual(row["returns"], [
            {"order_id": "O3", "return_id": "R1", "stage": "pending",
             "lines": [{"sku": "T", "quantity": 2}],
             "can_receive": True, "blockers": []},
        ])
        for entry in row["returns"]:
            self.assertEqual(set(entry),
                             {"order_id", "return_id", "stage", "lines", "can_receive", "blockers"})

    def test_skus_orders_and_returns_sorted(self):
        self.app.add_product("A", "Apple", 10)
        self.app.add_product("B", "Banana", 10)
        # Every order is placed while A and B are still unmanaged, ids
        # intentionally out of order.
        self.app.place("O3", [{"sku": "A", "quantity": 1}, {"sku": "B", "quantity": 1}])
        self.app.place("O1", [{"sku": "A", "quantity": 2}, {"sku": "B", "quantity": 2}])
        self.app.place("O2", [{"sku": "A", "quantity": 4}])
        self.app.place("S2", [{"sku": "A", "quantity": 3}, {"sku": "B", "quantity": 3}])
        self.app.place("S1", [{"sku": "A", "quantity": 1}, {"sku": "B", "quantity": 1}])
        self.app.restock("A", 2)
        self.app.restock("B", 2)
        # Shipped before management: nothing is deducted from stock.
        self.app.ship("S2", "DHL", "2")
        self.app.ship("S1", "DHL", "1")
        self.app.record_return("S2", "R2", [{"sku": "B", "quantity": 1}, {"sku": "A", "quantity": 1}])
        self.app.record_return("S1", "R9", [{"sku": "A", "quantity": 1}, {"sku": "B", "quantity": 1}])
        result = self.app.replenishment_report()
        self.assertEqual([row["sku"] for row in result], ["A", "B"])
        self.assertEqual([d["order_id"] for d in result[0]["orders"]], ["O1", "O2", "O3"])
        self.assertEqual([d["order_id"] for d in result[1]["orders"]], ["O1", "O3"])
        for row in result:
            self.assertEqual([e["return_id"] for e in row["returns"]], ["R2", "R9"])
            # Lines inside a return entry are merged and sku-sorted.
            self.assertEqual(row["returns"][0]["lines"], [
                {"sku": "A", "quantity": 1}, {"sku": "B", "quantity": 1},
            ])

    def test_duplicate_sku_lines_merge_and_missing_reservation_reads_zero(self):
        self.app.place("O1", [
            {"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}, {"sku": "T", "quantity": 3},
        ])
        self.app.restock("T", 4)
        result = self.app.replenishment_report()
        self.assertEqual([row["sku"] for row in result], ["T"])
        self.assertEqual(result[0]["orders"], [
            {"order_id": "O1", "quantity": 5, "reserved": 0, "unreserved": 5},
        ])

    def test_fully_reserved_orders_and_products_excluded(self):
        self.app.restock("C", 5)
        self.app.place("FULL", [{"sku": "C", "quantity": 3}])
        self.app.restock("T", 3)
        self.app.place("PART", [{"sku": "T", "quantity": 3}])
        self.app.release_reservation("PART", [{"sku": "T", "quantity": 1}])
        result = self.app.replenishment_report()
        self.assertEqual([row["sku"] for row in result], ["T"])
        self.assertEqual(result[0]["orders"], [
            {"order_id": "PART", "quantity": 3, "reserved": 2, "unreserved": 1},
        ])
        self.assertEqual(result[0]["shortfall"], 0)

    def test_unmanaged_products_carts_and_non_placed_orders_never_contribute(self):
        self.app.restock("T", 2)
        self.app.place("KEEP", [{"sku": "T", "quantity": 2}])
        self.app.release_reservation("KEEP", [{"sku": "T", "quantity": 2}])
        # Unmanaged demand never lists a product.
        self.app.place("UNMANAGED", [{"sku": "U", "quantity": 9}])
        # Carts never contribute.
        self.app.save_cart("CART1", [{"sku": "T", "quantity": 8}, {"sku": "U", "quantity": 8}])
        # Cancelled and shipped/delivered orders never contribute.
        self.app.place("CAN", [{"sku": "T", "quantity": 1}])
        self.app.cancel("CAN")
        self.app.place("GONE", [{"sku": "T", "quantity": 1}])
        self.app.ship("GONE", "DHL", "X")
        self.assertEqual([row["sku"] for row in self.app.replenishment_report()], ["T"])
        self.assertEqual([d["order_id"] for d in self.app.replenishment_report()[0]["orders"]],
                         ["KEEP"])

    def test_empty_when_no_demand(self):
        self.assertEqual(self.app.replenishment_report(), [])
        empty_root = Path(self.temp.name) / "empty"
        self.assertEqual(OrderDesk(empty_root).replenishment_report(), [])
        self.assertFalse(empty_root.exists())
        # Managed stock with nothing short is empty too.
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.assertEqual(self.app.replenishment_report(), [])

    def test_blocked_return_stays_visible_but_whole_registration_excluded_from_pending(self):
        self.app.add_product("X", "Xylophone", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 5}])
        # Healthy pending return of T.
        self.app.place("S1", [{"sku": "T", "quantity": 4}])
        # Pending return mixing T with an unmanaged product: blocked as a whole.
        self.app.place("S2", [{"sku": "T", "quantity": 4}, {"sku": "U", "quantity": 1}])
        # Pending return mixing T with a product that vanishes from the catalog.
        self.app.place("S3", [{"sku": "T", "quantity": 4}, {"sku": "X", "quantity": 1}])
        self.app.restock("T", 1)
        # All shipped before management (X and U stay unmanaged): no deduction.
        self.app.ship("S1", "DHL", "1")
        self.app.ship("S2", "DHL", "2")
        self.app.ship("S3", "DHL", "3")
        self.app.record_return("S1", "R1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("S2", "R2", [{"sku": "U", "quantity": 1}, {"sku": "T", "quantity": 3}])
        self.app.record_return("S3", "R3", [{"sku": "T", "quantity": 4}, {"sku": "X", "quantity": 1}])
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        del raw["products"]["X"]
        self.app._write(raw)
        row = OrderDesk(self.root).replenishment_report()[0]
        self.assertEqual([e["return_id"] for e in row["returns"]], ["R1", "R2", "R3"])
        entries = {e["return_id"]: e for e in row["returns"]}
        self.assertTrue(entries["R1"]["can_receive"])
        self.assertEqual(entries["R1"]["blockers"], [])
        self.assertFalse(entries["R2"]["can_receive"])
        self.assertEqual(entries["R2"]["blockers"], [{"sku": "U", "reason": "unmanaged"}])
        self.assertFalse(entries["R3"]["can_receive"])
        self.assertEqual(entries["R3"]["blockers"], [{"sku": "X", "reason": "unknown-product"}])
        # Only the fully receivable registration contributes pending quantity.
        self.assertEqual(row["pending"], 2)
        self.assertEqual(row["needed"], 5)
        self.assertEqual(row["shortfall"], 4)
        self.assertEqual(row["projected_shortfall"], 2)

    def test_received_and_cancelled_returns_excluded_amended_uses_latest(self):
        self.app.place("O1", [{"sku": "T", "quantity": 5}])
        self.app.place("S1", [{"sku": "T", "quantity": 9}])
        self.app.restock("T", 1)
        self.app.ship("S1", "DHL", "1")
        self.app.record_return("S1", "RDONE", [{"sku": "T", "quantity": 1}])
        self.app.record_return("S1", "RPEND", [{"sku": "T", "quantity": 3}])
        self.app.record_return("S1", "ROFF", [{"sku": "T", "quantity": 1}])
        self.app.receive_return("RDONE")
        self.app.cancel_return("ROFF")
        self.app.amend_return("RPEND",
                              [{"sku": "T", "quantity": 3}],
                              [{"sku": "T", "quantity": 2}])
        row = self.app.replenishment_report()[0]
        self.assertEqual([e["return_id"] for e in row["returns"]], ["RPEND"])
        self.assertEqual(row["pending"], 2)

    def test_paused_sales_do_not_block_query_or_returns(self):
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.place("S1", [{"sku": "T", "quantity": 2}])
        self.app.restock("T", 1)
        # Shipped before management: no deduction.
        self.app.ship("S1", "DHL", "1")
        self.app.record_return("S1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.set_product_enabled("T", False)
        row = self.app.replenishment_report()[0]
        self.assertEqual(row["needed"], 3)
        self.assertTrue(row["returns"][0]["can_receive"])
        self.assertEqual(row["returns"][0]["blockers"], [])

    def test_missing_catalog_entry_for_listed_product_raises(self):
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.restock("T", 1)
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        del raw["products"]["T"]
        self.app._write(raw)
        with self.assertRaises(ValueError):
            OrderDesk(self.root).replenishment_report()

    def test_pending_return_under_missing_or_unshipped_order_raises(self):
        base = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "inventory": {"T": {"on_hand": 1, "reserved": 0}},
            "orders": {
                "O1": {"order_id": "O1", "status": "placed",
                       "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100,
                                  "subtotal_cents": 200}],
                       "total_cents": 200},
                "PL": {"order_id": "PL", "status": "placed",
                       "lines": [{"sku": "T", "quantity": 1, "unit_price_cents": 100,
                                  "subtotal_cents": 100}],
                       "total_cents": 100},
            },
        }
        # Rogue pending return under a vanished order.
        data = json.loads(json.dumps(base))
        data["returns"] = {"GONE": [
            {"order_id": "GONE", "return_id": "RG", "lines": [{"sku": "T", "quantity": 1}]},
        ]}
        self._load(data)
        with self.assertRaises(ValueError):
            self.app.replenishment_report()
        # Rogue pending return under a placed order.
        data = json.loads(json.dumps(base))
        data["returns"] = {"PL": [
            {"order_id": "PL", "return_id": "RP", "lines": [{"sku": "T", "quantity": 1}]},
        ]}
        self._load(data)
        with self.assertRaises(ValueError):
            self.app.replenishment_report()
        # A received return under a vanished order and a cancelled return under
        # a placed order are simply absent, never errors.
        data = json.loads(json.dumps(base))
        data["returns"] = {}
        data["cancelled_returns"] = {"PL": [
            {"order_id": "PL", "return_id": "RC", "lines": [{"sku": "T", "quantity": 1}]},
        ]}
        data["return_receipts"] = {"RR": {
            "order_id": "GONE", "return_id": "RR",
            "lines": [{"sku": "T", "quantity": 1,
                       "before": {"sku": "T", "on_hand": 0, "reserved": 0, "available": 0},
                       "after": {"sku": "T", "on_hand": 1, "reserved": 0, "available": 1}}],
        }}
        self._load(data)
        row = self.app.replenishment_report()[0]
        self.assertEqual(row["returns"], [])
        self.assertEqual(row["pending"], 0)

    def test_legacy_missing_collections_read_as_empty(self):
        # Managed inventory but no orders, reservations, returns or receipts.
        self._load({"inventory": {"T": {"on_hand": 4, "reserved": 0}}})
        self.assertEqual(self.app.replenishment_report(), [])
        # Placed managed demand with no reservations collection: record reads
        # zero and the full quantity is still needed.
        self._load({
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "inventory": {"T": {"on_hand": 1, "reserved": 0}},
            "orders": {"O1": {"order_id": "O1", "status": "placed",
                              "lines": [{"sku": "T", "quantity": 3, "unit_price_cents": 100,
                                         "subtotal_cents": 300}],
                              "total_cents": 300}},
        })
        row = self.app.replenishment_report()[0]
        self.assertEqual(row["orders"], [
            {"order_id": "O1", "quantity": 3, "reserved": 0, "unreserved": 3},
        ])

    def test_reflects_restock_reservation_and_return_operations(self):
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.place("O2", [{"sku": "T", "quantity": 3}])
        self.app.place("S1", [{"sku": "T", "quantity": 2}])
        self.app.restock("T", 3)
        # Shipped while unmanaged: no stock is deducted.
        self.app.ship("S1", "DHL", "1")
        self.app.record_return("S1", "R1", [{"sku": "T", "quantity": 1}])
        row = self.app.replenishment_report()[0]
        self.assertEqual((row["needed"], row["pending"], row["shortfall"],
                          row["projected_shortfall"]), (5, 1, 2, 1))
        # Restock widens availability; both shortfalls shrink.
        self.app.restock("T", 1)
        row = self.app.replenishment_report()[0]
        self.assertEqual((row["shortfall"], row["projected_shortfall"]), (1, 0))
        # Topping up one order lowers needed; the other keeps the product listed.
        self.app.reserve_order("O1")
        row = self.app.replenishment_report()[0]
        self.assertEqual([d["order_id"] for d in row["orders"]], ["O2"])
        self.assertEqual((row["needed"], row["shortfall"], row["projected_shortfall"]), (3, 1, 0))
        # Receiving the return removes it and raises available stock.
        self.app.receive_return("R1")
        row = self.app.replenishment_report()[0]
        self.assertEqual(row["returns"], [])
        self.assertEqual(row["pending"], 0)
        self.assertEqual(row["stock"]["available"], 3)
        # Cancelling a pending return removes projected restock as well.
        self.app.record_return("S1", "R2", [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.replenishment_report()[0]["pending"], 1)
        self.app.cancel_return("R2")
        self.assertEqual(self.app.replenishment_report()[0]["pending"], 0)

    def test_query_is_read_only_repeatable_and_uses_no_sequences(self):
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.restock("T", 1)
        before = self.app.path.read_bytes()
        first = self.app.replenishment_report()
        second = self.app.replenishment_report()
        reopened = OrderDesk(self.root).replenishment_report()
        self.assertEqual(first, second)
        self.assertEqual(first, reopened)
        self.assertEqual(self.app.path.read_bytes(), before)
        events = self.app.history("O1")["events"]
        self.app.replenishment_report()
        self.assertEqual(self.app.history("O1")["events"], events)
        # A failing query neither creates a root nor writes a file.
        fresh = self.root / "fresh"
        OrderDesk(fresh).replenishment_report()
        self.assertFalse(fresh.exists())

    def test_cli_success_failure_and_array(self):
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.restock("T", 1)
        # No input file: parameterless query.
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "replenishment-report"],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual(result[0]["sku"], "T")
        self.assertEqual(result[0]["needed"], 3)
        # An explicit empty object works the same.
        payload = self.root / "q.json"
        payload.write_text("{}", encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "replenishment-report", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout), result)
        # Array input is dispatched row by row.
        payload.write_text(json.dumps([{}, {}]), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "replenishment-report", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout), [result, result])
        # Bad legacy state: error JSON on stderr, exit 2, file untouched.
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        del raw["products"]["T"]
        self.app._write(raw)
        before = self.app.path.read_bytes()
        failed = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "replenishment-report"],
            text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2, failed.stdout)
        self.assertEqual(failed.stdout, "")
        self.assertIn("error", json.loads(failed.stderr))
        self.assertEqual(self.app.path.read_bytes(), before)

    def _load(self, data):
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        self.app = OrderDesk(self.root)


if __name__ == "__main__":
    unittest.main()
