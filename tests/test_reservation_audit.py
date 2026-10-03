import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk


class ReservationAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)
        self.app.add_product("U", "Unrestocked", 50)

    def test_book_example_balances_to_zero(self):
        # Ten on hand, five reserved across two placed orders: the books and
        # the detail agree even though five units remain available.
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.place("O2", [{"sku": "T", "quantity": 4}])
        self.app.release_reservation("O1", [{"sku": "T", "quantity": 1}])
        self.app.release_reservation("O2", [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 10, "reserved": 5, "available": 5})
        result = self.app.reservation_audit("T")
        self.assertEqual(set(result), {"stock", "allocated", "difference", "orders"})
        self.assertEqual(result["stock"], {"sku": "T", "on_hand": 10, "reserved": 5, "available": 5})
        self.assertEqual(result["allocated"], 5)
        self.assertEqual(result["difference"], 0)
        self.assertEqual(result["orders"], [
            {"order_id": "O1", "quantity": 3, "reserved": 2, "unreserved": 1},
            {"order_id": "O2", "quantity": 4, "reserved": 3, "unreserved": 1},
        ])
        for row in result["orders"]:
            self.assertEqual(set(row), {"order_id", "quantity", "reserved", "unreserved"})

    def test_orders_sorted_and_duplicate_skus_merged(self):
        self.app.restock("T", 10)
        self.app.restock("C", 5)
        self.app.place("O2", [{"sku": "T", "quantity": 2}, {"sku": "T", "quantity": 1}])
        self.app.place("O1", [{"sku": "T", "quantity": 4}, {"sku": "C", "quantity": 5}])
        result = self.app.reservation_audit(" T ")
        self.assertEqual([row["order_id"] for row in result["orders"]], ["O1", "O2"])
        self.assertEqual(result["orders"][0]["quantity"], 4)
        # Duplicate T lines inside O2 merge to 3.
        self.assertEqual(result["orders"][1]["quantity"], 3)
        self.assertEqual(result["allocated"], 7)
        self.assertEqual(result["difference"], 0)
        # Auditing C only lists the order that contains it.
        coffee = self.app.reservation_audit("C")
        self.assertEqual([row["order_id"] for row in coffee["orders"]], ["O1"])

    def test_no_matching_orders_returns_empty_array(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "C", "quantity": 2}])
        result = self.app.reservation_audit("T")
        self.assertEqual(result["orders"], [])
        self.assertEqual(result["allocated"], 0)
        self.assertEqual(result["difference"], 0)
        self.assertEqual(result["stock"], self.app.stock("T"))

    def test_cancelled_shipped_and_delivered_orders_excluded(self):
        self.app.restock("T", 20)
        self.app.place("KEEP", [{"sku": "T", "quantity": 2}])
        self.app.place("GONE", [{"sku": "T", "quantity": 3}])
        self.app.place("SENT", [{"sku": "T", "quantity": 4}])
        self.app.place("DONE", [{"sku": "T", "quantity": 5}])
        self.app.cancel("GONE")
        self.app.ship("SENT", "post", "TRK1")
        self.app.ship("DONE", "post", "TRK2")
        self.app.confirm_delivery("DONE", "Pat", "2026-10-01")
        result = self.app.reservation_audit("T")
        self.assertEqual([row["order_id"] for row in result["orders"]], ["KEEP"])
        self.assertEqual(result["allocated"], 2)
        self.assertEqual(result["difference"], 0)

    def test_carts_and_returns_never_enter_detail(self):
        self.app.restock("T", 10)
        self.app.save_cart("CART1", [{"sku": "T", "quantity": 6}])
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.place("O2", [{"sku": "T", "quantity": 2}])
        self.app.ship("O2", "post", "TRK1")
        self.app.record_return("O2", "R1", [{"sku": "T", "quantity": 1}])
        result = self.app.reservation_audit("T")
        self.assertEqual([row["order_id"] for row in result["orders"]], ["O1"])
        self.assertEqual(result["allocated"], 3)

    def test_unreserved_is_quantity_minus_reserved_floored_at_zero(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.release_reservation("O1", [{"sku": "T", "quantity": 2}])
        result = self.app.reservation_audit("T")
        self.assertEqual(result["orders"][0]["unreserved"], 2)
        # Legacy data holding more reservation than ordered: unreserved is
        # zero, never negative, and the surplus shows up in the difference.
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        raw["reservations"]["O1"]["T"] = 5
        raw["inventory"]["T"]["reserved"] = 5
        self.app._write(raw)
        result = OrderDesk(self.root).reservation_audit("T")
        self.assertEqual(result["orders"][0]["reserved"], 5)
        self.assertEqual(result["orders"][0]["unreserved"], 0)
        self.assertEqual(result["allocated"], 5)
        self.assertEqual(result["difference"], 0)

    def test_difference_keeps_sign_for_inconsistent_legacy_data(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        # Book total above the detail: positive difference.
        raw["inventory"]["T"]["reserved"] = 5
        self.app._write(raw)
        result = OrderDesk(self.root).reservation_audit("T")
        self.assertEqual(result["allocated"], 3)
        self.assertEqual(result["difference"], 2)
        # Book total below the detail: negative difference. Neither query
        # repairs stock or attribution.
        raw["inventory"]["T"]["reserved"] = 1
        self.app._write(raw)
        result = OrderDesk(self.root).reservation_audit("T")
        self.assertEqual(result["allocated"], 3)
        self.assertEqual(result["difference"], -2)
        self.assertEqual(OrderDesk(self.root).stock("T")["reserved"], 1)

    def test_unmanaged_product_keeps_null_stock_semantics(self):
        self.app.place("O1", [{"sku": "U", "quantity": 4}, {"sku": "U", "quantity": 1}])
        result = self.app.reservation_audit("U")
        self.assertEqual(result["stock"], {"sku": "U", "on_hand": None, "reserved": 0, "available": None})
        self.assertEqual(result["allocated"], 0)
        self.assertEqual(result["difference"], 0)
        self.assertEqual(result["orders"], [
            {"order_id": "O1", "quantity": 5, "reserved": 0, "unreserved": 5},
        ])

    def test_managed_after_order_placed_reads_missing_record_as_zero(self):
        self.app.place("O1", [{"sku": "U", "quantity": 10}])
        self.app.restock("U", 4)
        result = self.app.reservation_audit("U")
        self.assertEqual(result["orders"], [
            {"order_id": "O1", "quantity": 10, "reserved": 0, "unreserved": 10},
        ])
        self.assertEqual(result["allocated"], 0)
        self.assertEqual(result["difference"], 0)
        # A later top-up is reflected on the next query.
        self.app.restock("U", 6)
        self.app.reserve_order("O1")
        result = self.app.reservation_audit("U")
        self.assertEqual(result["orders"], [
            {"order_id": "O1", "quantity": 10, "reserved": 10, "unreserved": 0},
        ])
        self.assertEqual(result["allocated"], 10)
        self.assertEqual(result["difference"], 0)

    def test_legacy_data_without_orders_or_reservations(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        del raw["orders"]
        raw.pop("reservations", None)
        self.app._write(raw)
        result = OrderDesk(self.root).reservation_audit("T")
        self.assertEqual(result["orders"], [])
        self.assertEqual(result["allocated"], 0)
        # The book total no longer matches any detail; the difference is
        # reported as-is, not repaired.
        self.assertEqual(result["difference"], 2)

    def test_paused_product_still_queryable(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.set_product_enabled("T", False)
        result = self.app.reservation_audit("T")
        self.assertEqual(result["allocated"], 2)
        self.assertEqual(result["orders"][0]["quantity"], 2)

    def test_operations_are_reflected_on_requery(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 4}])
        self.app.place("O2", [{"sku": "T", "quantity": 5}])
        self.app.release_reservation("O2", [{"sku": "T", "quantity": 2}])
        # Top-up, release, transfer and amend all show up on the next query.
        self.app.transfer_reservation("O1", "O2", [{"sku": "T", "quantity": 2}])
        result = self.app.reservation_audit("T")
        self.assertEqual(
            [(row["order_id"], row["reserved"]) for row in result["orders"]],
            [("O1", 2), ("O2", 5)],
        )
        self.app.release_reservation("O2", [{"sku": "T", "quantity": 1}])
        self.app.amend("O1", [{"sku": "T", "quantity": 1}])
        result = self.app.reservation_audit("T")
        self.assertEqual(result["orders"], [
            {"order_id": "O1", "quantity": 1, "reserved": 1, "unreserved": 0},
            {"order_id": "O2", "quantity": 5, "reserved": 4, "unreserved": 1},
        ])
        self.assertEqual(result["allocated"], 5)
        self.assertEqual(result["difference"], 0)
        # A cancelled order drops out entirely.
        self.app.cancel("O2")
        result = self.app.reservation_audit("T")
        self.assertEqual([row["order_id"] for row in result["orders"]], ["O1"])
        self.assertEqual(result["allocated"], 1)

    def test_invalid_sku_raises_value_error(self):
        self.app.restock("T", 5)
        for payload in [None, 42, 3.5, True, [], {}, "", "   ", "missing", "t"]:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    self.app.reservation_audit(payload)

    def test_query_is_read_only_and_repeatable(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        before = self.app.path.read_bytes()
        first = self.app.reservation_audit("T")
        second = self.app.reservation_audit("T")
        reopened = OrderDesk(self.root).reservation_audit("T")
        self.assertEqual(first, second)
        self.assertEqual(first, reopened)
        self.assertEqual(self.app.path.read_bytes(), before)
        # A failed query does not create the data directory.
        fresh = self.root / "fresh"
        with self.assertRaises(ValueError):
            OrderDesk(fresh).reservation_audit("T")
        self.assertFalse(fresh.exists())

    def test_cli_reservation_audit_success_failure_and_array(self):
        self.app.restock("T", 10)
        self.app.place("A", [{"sku": "T", "quantity": 3}])
        self.app.place("B", [{"sku": "T", "quantity": 2}])
        payload = self.root / "audit.json"
        payload.write_text(json.dumps({"sku": "T"}), encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "reservation-audit", str(payload)],
                            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        value = json.loads(ok.stdout)
        self.assertEqual(value["allocated"], 5)
        self.assertEqual(value["difference"], 0)
        self.assertEqual([row["order_id"] for row in value["orders"]], ["A", "B"])
        # Invalid query: no success output, error JSON on stderr, exit 2.
        before = (self.root / "data.json").read_bytes()
        payload.write_text(json.dumps({"sku": "nope"}), encoding="utf-8")
        failed = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "reservation-audit", str(payload)],
                                text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(failed.stdout, "")
        self.assertIn("error", json.loads(failed.stderr))
        self.assertEqual((self.root / "data.json").read_bytes(), before)
        # Array input is processed item by item and stops at the first error.
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([{"sku": "T"}, {"sku": "nope"}]), encoding="utf-8")
        stopped = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "reservation-audit", str(batch)],
                                 text=True, capture_output=True)
        self.assertEqual(stopped.returncode, 2, stopped.stdout)
        self.assertEqual(stopped.stdout, "")
        self.assertIn("error", json.loads(stopped.stderr))
        # An array of valid queries prints one audit each.
        batch.write_text(json.dumps([{"sku": "T"}, {"sku": " T "}]), encoding="utf-8")
        multi = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "reservation-audit", str(batch)],
                               text=True, capture_output=True)
        self.assertEqual(multi.returncode, 0, multi.stderr)
        audits = json.loads(multi.stdout)
        self.assertEqual([audit["allocated"] for audit in audits], [5, 5])


if __name__ == "__main__":
    unittest.main()
