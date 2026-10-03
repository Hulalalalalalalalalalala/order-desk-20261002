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

    def test_stock_example_with_partial_reservations(self):
        self.app.restock("T", 10)
        # Two orders demand 3 and 4 but hold only 2 and 3 reserved.
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.place("O2", [{"sku": "T", "quantity": 4}])
        self.app.release_reservation("O1", [{"sku": "T", "quantity": 1}])
        self.app.release_reservation("O2", [{"sku": "T", "quantity": 1}])
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

    def test_stock_matches_stock_query_managed_and_unmanaged(self):
        self.app.restock("T", 7)
        self.app.place("O1", [{"sku": "T", "quantity": 2}, {"sku": "U", "quantity": 4}])
        managed = self.app.reservation_audit("T")
        self.assertEqual(managed["stock"], self.app.stock("T"))
        unmanaged = self.app.reservation_audit("U")
        self.assertEqual(unmanaged["stock"], self.app.stock("U"))
        self.assertEqual(unmanaged["stock"], {"sku": "U", "on_hand": None, "reserved": 0, "available": None})
        self.assertEqual(unmanaged["allocated"], 0)
        self.assertEqual(unmanaged["difference"], 0)
        self.assertEqual(unmanaged["orders"], [
            {"order_id": "O1", "quantity": 4, "reserved": 0, "unreserved": 4},
        ])

    def test_orders_sorted_and_filtered_by_status_and_sku(self):
        self.app.restock("T", 20)
        self.app.restock("C", 5)
        self.app.place("O3", [{"sku": "T", "quantity": 1}, {"sku": "T", "quantity": 1}, {"sku": "C", "quantity": 2}])
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.place("O2", [{"sku": "C", "quantity": 3}])
        self.app.cancel("O2")
        self.app.place("O4", [{"sku": "T", "quantity": 1}])
        self.app.ship("O4", "DHL", "TRK1")
        # A saved cart and a return never enter the detail even with matching sku.
        self.app.save_cart("CART1", [{"sku": "T", "quantity": 9}])
        self.app.place("O5", [{"sku": "T", "quantity": 1}])
        self.app.ship("O5", "DHL", "TRK2")
        self.app.record_return("O5", "R1", [{"sku": "T", "quantity": 1}])
        result = self.app.reservation_audit("T")
        # Duplicate T lines in O3 merge to 2; O2 (cancelled) and O4/O5
        # (shipped) are absent; the coffee-only order never appears.
        self.assertEqual([row["order_id"] for row in result["orders"]], ["O1", "O3"])
        self.assertEqual(result["orders"][1]["quantity"], 2)
        self.assertEqual(result["allocated"], 4)
        self.assertEqual(result["difference"], 0)

    def test_no_matching_orders_is_empty_array(self):
        self.app.restock("T", 5)
        self.app.restock("C", 5)
        self.app.place("O1", [{"sku": "C", "quantity": 1}])
        result = self.app.reservation_audit("T")
        self.assertEqual(result["orders"], [])
        self.assertEqual(result["allocated"], 0)
        self.assertEqual(result["difference"], 0)
        self.assertEqual(result["stock"], {"sku": "T", "on_hand": 5, "reserved": 0, "available": 5})

    def test_paused_product_is_still_auditable(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.set_product_enabled("T", False)
        result = self.app.reservation_audit(" T ")
        self.assertEqual(result["orders"][0]["order_id"], "O1")
        self.assertEqual(result["stock"]["reserved"], 2)

    def test_invalid_or_unknown_sku_raises(self):
        self.app.restock("T", 5)
        for bad in (None, 42, "", "   "):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.app.reservation_audit(bad)
        with self.assertRaises(ValueError):
            self.app.reservation_audit("t")  # case sensitive, unknown
        with self.assertRaises(ValueError):
            self.app.reservation_audit("missing")

    def test_legacy_mismatch_reports_actual_signed_difference_without_fixing(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        # Books hold 2 more units than the order details explain.
        raw["inventory"]["T"]["reserved"] = 4
        self.app._write(raw)
        result = OrderDesk(self.root).reservation_audit("T")
        self.assertEqual(result["stock"]["reserved"], 4)
        self.assertEqual(result["allocated"], 2)
        self.assertEqual(result["difference"], 2)
        self.assertEqual(result["orders"][0]["reserved"], 2)
        # Negative difference: details exceed the books.
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        raw["inventory"]["T"]["reserved"] = 1
        self.app._write(raw)
        result = OrderDesk(self.root).reservation_audit("T")
        self.assertEqual(result["allocated"], 2)
        self.assertEqual(result["difference"], -1)
        # Nothing was corrected on disk.
        saved = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["inventory"]["T"]["reserved"], 1)
        self.assertEqual(saved["reservations"]["O1"]["T"], 2)

    def test_reservation_without_matching_current_line_counts_only_as_difference(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 5}])
        self.app.place("O2", [{"sku": "T", "quantity": 2}])
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        # Legacy stray attribution: O2's current lines no longer contain T, yet
        # a record survives. It is not displayed but stays on the books.
        raw["orders"]["O2"]["lines"] = [
            {"sku": "C", "quantity": 2, "unit_price_cents": 200, "subtotal_cents": 400}
        ]
        raw["orders"]["O2"]["total_cents"] = 400
        self.app._write(raw)
        result = OrderDesk(self.root).reservation_audit("T")
        self.assertEqual([row["order_id"] for row in result["orders"]], ["O1"])
        self.assertEqual(result["allocated"], 5)
        self.assertEqual(result["difference"], 2)

    def test_unmanaged_product_ignores_stray_reservation_records(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}, {"sku": "U", "quantity": 4}])
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        raw["reservations"]["O1"]["U"] = 3
        self.app._write(raw)
        result = OrderDesk(self.root).reservation_audit("U")
        self.assertEqual(result["stock"], {"sku": "U", "on_hand": None, "reserved": 0, "available": None})
        self.assertEqual(result["allocated"], 0)
        self.assertEqual(result["difference"], 0)
        self.assertEqual(result["orders"], [
            {"order_id": "O1", "quantity": 4, "reserved": 0, "unreserved": 4},
        ])

    def test_legacy_data_without_collections(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}, {"sku": "U", "quantity": 4}])
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        del raw["inventory"]
        raw.pop("reservations", None)
        del raw["orders"]
        self.app._write(raw)
        reopened = OrderDesk(self.root)
        tea = reopened.reservation_audit("T")
        self.assertEqual(tea["orders"], [])
        self.assertEqual(tea["allocated"], 0)
        self.assertEqual(tea["difference"], 0)
        self.assertEqual(tea["stock"], {"sku": "T", "on_hand": None, "reserved": 0, "available": None})

    def test_reflects_reserve_release_transfer_and_amend(self):
        # Ordered while unmanaged, then managed and topped up.
        self.app.place("O1", [{"sku": "U", "quantity": 10}])
        self.app.restock("U", 15)
        before = self.app.reservation_audit("U")
        self.assertEqual(before["orders"][0]["reserved"], 0)
        self.assertEqual(before["orders"][0]["unreserved"], 10)
        self.app.reserve_order("O1")
        topped = self.app.reservation_audit("U")
        self.assertEqual(topped["allocated"], 10)
        self.assertEqual(topped["difference"], 0)
        self.assertEqual(topped["orders"], [
            {"order_id": "O1", "quantity": 10, "reserved": 10, "unreserved": 0},
        ])
        # Release reflects immediately.
        self.app.release_reservation("O1", [{"sku": "U", "quantity": 4}])
        released = self.app.reservation_audit("U")
        self.assertEqual(released["stock"]["reserved"], 6)
        self.assertEqual(released["allocated"], 6)
        self.assertEqual(released["orders"][0]["reserved"], 6)
        self.assertEqual(released["orders"][0]["unreserved"], 4)
        # A second order with headroom after its own release can receive a
        # transfer; attribution moves without changing the reserved total.
        self.app.place("O2", [{"sku": "U", "quantity": 8}])
        self.app.release_reservation("O2", [{"sku": "U", "quantity": 3}])
        self.app.transfer_reservation("O1", "O2", [{"sku": "U", "quantity": 3}])
        transferred = self.app.reservation_audit("U")
        self.assertEqual(transferred["allocated"], 11)
        self.assertEqual(transferred["difference"], 0)
        self.assertEqual(transferred["orders"], [
            {"order_id": "O1", "quantity": 10, "reserved": 3, "unreserved": 7},
            {"order_id": "O2", "quantity": 8, "reserved": 8, "unreserved": 0},
        ])
        # Amend changes the merged ordered quantity and reservation together.
        self.app.amend("O2", [{"sku": "U", "quantity": 2}])
        amended = self.app.reservation_audit("U")
        self.assertEqual(amended["orders"], [
            {"order_id": "O1", "quantity": 10, "reserved": 3, "unreserved": 7},
            {"order_id": "O2", "quantity": 2, "reserved": 2, "unreserved": 0},
        ])
        self.assertEqual(amended["allocated"], 5)
        self.assertEqual(amended["difference"], 0)

    def test_query_is_read_only_repeatable_and_uses_no_sequences(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        before = self.app.path.read_bytes()
        first = self.app.reservation_audit("T")
        second = self.app.reservation_audit("T")
        reopened = OrderDesk(self.root).reservation_audit("T")
        self.assertEqual(first, second)
        self.assertEqual(first, reopened)
        self.assertEqual(self.app.path.read_bytes(), before)
        # A failed query never creates a nonexistent root.
        fresh = self.root / "fresh"
        with self.assertRaises(ValueError):
            OrderDesk(fresh).reservation_audit("T")
        self.assertFalse(fresh.exists())

    def test_cli_success_failure_and_array(self):
        self.app.restock("T", 10)
        self.app.place("A", [{"sku": "T", "quantity": 3}])
        self.app.place("B", [{"sku": "T", "quantity": 4}])
        payload = self.root / "audit.json"
        payload.write_text(json.dumps({"sku": " T "}), encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "reservation-audit", str(payload)],
                            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        value = json.loads(ok.stdout)
        self.assertEqual(set(value), {"stock", "allocated", "difference", "orders"})
        self.assertEqual(value["stock"], {"sku": "T", "on_hand": 10, "reserved": 7, "available": 3})
        self.assertEqual([row["order_id"] for row in value["orders"]], ["A", "B"])
        # Invalid query: error JSON on stderr, exit 2, nothing printed.
        before = (self.root / "data.json").read_bytes()
        payload.write_text(json.dumps({"sku": "nope"}), encoding="utf-8")
        failed = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "reservation-audit", str(payload)],
                                text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(failed.stdout, "")
        self.assertIn("error", json.loads(failed.stderr))
        self.assertEqual((self.root / "data.json").read_bytes(), before)
        # Array input is handled item by item and stops at the first error.
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([{"sku": "T"}, {"sku": "nope"}]), encoding="utf-8")
        stopped = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "reservation-audit", str(batch)],
                                 text=True, capture_output=True)
        self.assertEqual(stopped.returncode, 2, stopped.stdout)
        self.assertEqual(stopped.stdout, "")
        self.assertIn("error", json.loads(stopped.stderr))
        # An array of valid queries prints one audit each, in order.
        batch.write_text(json.dumps([{"sku": "T"}, {"sku": "C"}]), encoding="utf-8")
        multi = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root), "reservation-audit", str(batch)],
                               text=True, capture_output=True)
        self.assertEqual(multi.returncode, 0, multi.stderr)
        audits = json.loads(multi.stdout)
        self.assertEqual([audit["stock"]["sku"] for audit in audits], ["T", "C"])


if __name__ == "__main__":
    unittest.main()
