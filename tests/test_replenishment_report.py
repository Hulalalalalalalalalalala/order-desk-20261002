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
        self.app.add_product("U", "Unmanaged", 0)

    def by_sku(self, result):
        return {row["sku"]: row for row in result}

    # ------------------------------------------------------------------ math

    def test_example_shortfalls(self):
        # Still need 5, 1 available, 2 receivable pending returns: the two
        # shortfalls are 4 and 2.
        self._load({
            "products": {
                "T": {"sku": "T", "name": "Tea", "price_cents": 100},
            },
            "inventory": {"T": {"on_hand": 1, "reserved": 0}},
            "orders": {
                "O1": {"order_id": "O1", "status": "placed",
                       "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100,
                                  "subtotal_cents": 200}], "total_cents": 200},
                "O2": {"order_id": "O2", "status": "placed",
                       "lines": [{"sku": "T", "quantity": 3, "unit_price_cents": 100,
                                  "subtotal_cents": 300}], "total_cents": 300},
                "OS": {"order_id": "OS", "status": "shipped",
                       "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100,
                                  "subtotal_cents": 200}], "total_cents": 200,
                       "shipment": {"carrier": "DHL", "tracking_no": "Z"}},
            },
            "returns": {"OS": [
                {"order_id": "OS", "return_id": "R1",
                 "lines": [{"sku": "T", "quantity": 2}]},
            ]},
        })
        result = self.app.replenishment_report()
        self.assertEqual([row["sku"] for row in result], ["T"])
        row = result[0]
        self.assertEqual(set(row),
                         {"sku", "stock", "needed", "pending", "shortfall",
                          "projected_shortfall", "orders", "returns"})
        self.assertEqual(row["stock"], {"sku": "T", "on_hand": 1, "reserved": 0, "available": 1})
        self.assertEqual(row["needed"], 5)
        self.assertEqual(row["pending"], 2)
        self.assertEqual(row["shortfall"], 4)
        self.assertEqual(row["projected_shortfall"], 2)

    def test_shortfalls_zero_when_available_covers_demand(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 4}])
        self.app.release_reservation("O1", [{"sku": "T", "quantity": 2}])
        row = self.app.replenishment_report()[0]
        # need 2, 8 available and no pending returns.
        self.assertEqual((row["needed"], row["pending"]), (2, 0))
        self.assertEqual((row["shortfall"], row["projected_shortfall"]), (0, 0))

    def test_projected_shortfall_never_negative_when_pending_covers_gap(self):
        self._load({
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "inventory": {"T": {"on_hand": 0, "reserved": 0}},
            "orders": {
                "O1": {"order_id": "O1", "status": "placed",
                       "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100,
                                  "subtotal_cents": 200}], "total_cents": 200},
                "OS": {"order_id": "OS", "status": "shipped",
                       "lines": [{"sku": "T", "quantity": 9, "unit_price_cents": 100,
                                  "subtotal_cents": 900}], "total_cents": 900,
                       "shipment": {"carrier": "DHL", "tracking_no": "Z"}},
            },
            "returns": {"OS": [
                {"order_id": "OS", "return_id": "R1",
                 "lines": [{"sku": "T", "quantity": 9}]},
            ]},
        })
        row = self.app.replenishment_report()[0]
        self.assertEqual((row["shortfall"], row["projected_shortfall"]), (2, 0))

    # --------------------------------------------------------------- filtering

    def test_only_managed_products_with_positive_need_listed_sorted(self):
        self.app.restock("T", 10)
        self.app.restock("C", 10)
        self.app.add_product("A", "Apple", 50)
        # T and C still need reservation; A appears only in a cart; U is an
        # unmanaged placed-order line; an order fully reserved contributes
        # nothing and must not be listed.
        self.app.save_cart("CART1", [{"sku": "A", "quantity": 4}])
        self.app.place("O1", [{"sku": "T", "quantity": 2}, {"sku": "U", "quantity": 5}])
        self.app.place("O2", [{"sku": "C", "quantity": 1}])
        self.app.place("O3", [{"sku": "T", "quantity": 3}])
        self.app.release_reservation("O1", [{"sku": "T", "quantity": 1}])
        self.app.release_reservation("O2", [{"sku": "C", "quantity": 1}])
        result = self.app.replenishment_report()
        self.assertEqual([row["sku"] for row in result], ["C", "T"])
        self.assertEqual(self.by_sku(result)["T"]["needed"], 1)
        self.assertEqual(self.by_sku(result)["C"]["needed"], 1)

    def test_non_placed_orders_and_carts_do_not_contribute(self):
        self.app.restock("T", 20)
        self.app.save_cart("CART1", [{"sku": "T", "quantity": 9}])
        self.app.place("SHIP", [{"sku": "T", "quantity": 2}])
        self.app.ship("SHIP", "DHL", "X")
        self.app.place("CANC", [{"sku": "T", "quantity": 2}])
        self.app.cancel("CANC")
        self.app.place("O1", [{"sku": "T", "quantity": 4}])
        result = self.app.replenishment_report()
        self.assertEqual([row["sku"] for row in result], [])

    def test_empty_roots_and_no_need_return_empty_list(self):
        self.assertEqual(self.app.replenishment_report(), [])
        empty_root = Path(self.temp.name) / "empty"
        self.assertEqual(OrderDesk(empty_root).replenishment_report(), [])
        self.assertFalse(empty_root.exists())
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.assertEqual(self.app.replenishment_report(), [])

    # ----------------------------------------------------------- orders detail

    def test_orders_detail_matches_reservation_audit_sorted(self):
        self.app.restock("T", 20)
        self.app.place("O3", [{"sku": "T", "quantity": 2}, {"sku": "T", "quantity": 2}])
        self.app.place("O1", [{"sku": "T", "quantity": 4}])
        self.app.place("O2", [{"sku": "T", "quantity": 3}])
        self.app.release_reservation("O1", [{"sku": "T", "quantity": 2}])
        self.app.release_reservation("O2", [{"sku": "T", "quantity": 1}])
        self.app.place("O4", [{"sku": "T", "quantity": 1}])
        row = self.by_sku(self.app.replenishment_report())["T"]
        self.assertEqual(row["orders"], [
            {"order_id": "O1", "quantity": 4, "reserved": 2, "unreserved": 2},
            {"order_id": "O2", "quantity": 3, "reserved": 2, "unreserved": 1},
        ])
        for order in row["orders"]:
            self.assertEqual(set(order), {"order_id", "quantity", "reserved", "unreserved"})
        # O3 (merged 4, fully reserved) and O4 (fully reserved) are absent.
        audit = self.app.reservation_audit("T")
        for order in row["orders"]:
            self.assertIn(order, audit["orders"])

    def test_missing_reservation_record_reads_as_zero(self):
        self._load({
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "inventory": {"T": {"on_hand": 3, "reserved": 0}},
            "orders": {"OLD": {"order_id": "OLD", "status": "placed",
                               "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100,
                                          "subtotal_cents": 200}], "total_cents": 200}},
        })
        row = self.app.replenishment_report()[0]
        self.assertEqual(row["orders"], [
            {"order_id": "OLD", "quantity": 2, "reserved": 0, "unreserved": 2},
        ])

    def test_stock_matches_stock_query(self):
        self.app.restock("T", 6)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.release_reservation("O1", [{"sku": "T", "quantity": 1}])
        row = self.by_sku(self.app.replenishment_report())["T"]
        self.assertEqual(row["stock"], self.app.stock("T"))

    # ---------------------------------------------------------- returns detail

    def _shipped_with_t(self, order_id, quantity=5):
        self.app.restock("T", max(quantity, 1))
        self.app.place(order_id, [{"sku": "T", "quantity": quantity}])
        self.app.ship(order_id, "DHL", "TRK-" + order_id)

    def test_returns_detail_sorted_with_worklist_shape(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.release_reservation("O1", [{"sku": "T", "quantity": 3}])
        self._shipped_with_t("S1")
        self._shipped_with_t("S2")
        self.app.record_return("S2", "R2", [{"sku": "T", "quantity": 1}])
        self.app.record_return("S1", "R1", [
            {"sku": "T", "quantity": 1}, {"sku": "T", "quantity": 1},
        ])
        row = self.by_sku(self.app.replenishment_report())["T"]
        self.assertEqual([entry["return_id"] for entry in row["returns"]], ["R1", "R2"])
        worklist = {e["return_id"]: e for e in self.app.return_worklist()}
        for entry in row["returns"]:
            self.assertEqual(entry, worklist[entry["return_id"]])
            self.assertEqual(set(entry),
                             {"order_id", "return_id", "stage", "lines",
                              "can_receive", "blockers"})
        self.assertEqual(row["returns"][0]["lines"], [{"sku": "T", "quantity": 2}])
        self.assertEqual(row["pending"], 3)

    def test_received_cancelled_and_amended_returns(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.release_reservation("O1", [{"sku": "T", "quantity": 2}])
        self._shipped_with_t("S1")
        self.app.record_return("S1", "RD", [{"sku": "T", "quantity": 1}])
        self.app.record_return("S1", "RC", [{"sku": "T", "quantity": 1}])
        self.app.record_return("S1", "RP", [{"sku": "T", "quantity": 1}])
        self.app.receive_return("RD")
        self.app.cancel_return("RC")
        row = self.by_sku(self.app.replenishment_report())["T"]
        self.assertEqual([e["return_id"] for e in row["returns"]], ["RP"])
        self.assertEqual(row["pending"], 1)
        # An amended registration counts at its latest quantity.
        self.app.amend_return("RP", [{"sku": "T", "quantity": 1}],
                             [{"sku": "T", "quantity": 2}])
        row = self.by_sku(self.app.replenishment_report())["T"]
        self.assertEqual(row["returns"][0]["lines"], [{"sku": "T", "quantity": 2}])
        self.assertEqual(row["pending"], 2)

    def test_blocked_registration_kept_in_detail_but_whole_amount_excluded(self):
        self._load({
            "products": {
                "T": {"sku": "T", "name": "Tea", "price_cents": 100},
                "U": {"sku": "U", "name": "Unmanaged", "price_cents": 0},
            },
            "inventory": {"T": {"on_hand": 1, "reserved": 0}},
            "orders": {
                "O1": {"order_id": "O1", "status": "placed",
                       "lines": [{"sku": "T", "quantity": 5, "unit_price_cents": 100,
                                  "subtotal_cents": 500}], "total_cents": 500},
                "S1": {"order_id": "S1", "status": "shipped",
                       "lines": [
                           {"sku": "T", "quantity": 2, "unit_price_cents": 100,
                            "subtotal_cents": 200},
                           {"sku": "U", "quantity": 1, "unit_price_cents": 0,
                            "subtotal_cents": 0},
                       ], "total_cents": 200,
                       "shipment": {"carrier": "DHL", "tracking_no": "Z"}},
                "S2": {"order_id": "S2", "status": "delivered",
                       "lines": [{"sku": "T", "quantity": 1, "unit_price_cents": 100,
                                  "subtotal_cents": 100}], "total_cents": 100,
                       "shipment": {"carrier": "DHL", "tracking_no": "Y"},
                       "delivery": {"recipient": "Ann", "delivered_on": "2026-09-01"}},
            },
            "returns": {
                # R-GOOD is fully receivable; R-BLOCKED also carries an
                # unmanaged product, so its T unit contributes no pending at
                # all, but the registration with its blocker reason is kept.
                "S2": [{"order_id": "S2", "return_id": "R-GOOD",
                        "lines": [{"sku": "T", "quantity": 1}]}],
                "S1": [{"order_id": "S1", "return_id": "R-BLOCKED", "lines": [
                    {"sku": "T", "quantity": 2}, {"sku": "U", "quantity": 1},
                ]}],
            },
        })
        row = self.by_sku(self.app.replenishment_report())["T"]
        self.assertEqual([e["return_id"] for e in row["returns"]], ["R-BLOCKED", "R-GOOD"])
        blocked = row["returns"][0]
        self.assertFalse(blocked["can_receive"])
        self.assertEqual(blocked["blockers"], [{"sku": "U", "reason": "unmanaged"}])
        self.assertTrue(row["returns"][1]["can_receive"])
        self.assertEqual(row["pending"], 1)
        self.assertEqual(row["shortfall"], 4)
        self.assertEqual(row["projected_shortfall"], 3)

    def test_return_without_candidate_sku_is_neither_shown_nor_validated(self):
        self._load({
            "products": {
                "T": {"sku": "T", "name": "Tea", "price_cents": 100},
                "U": {"sku": "U", "name": "Unmanaged", "price_cents": 0},
            },
            "inventory": {"T": {"on_hand": 0, "reserved": 0}},
            "orders": {
                "O1": {"order_id": "O1", "status": "placed",
                       "lines": [{"sku": "T", "quantity": 1, "unit_price_cents": 100,
                                  "subtotal_cents": 100}], "total_cents": 100},
                "GONE": {"order_id": "GONE", "status": "shipped",
                         "lines": [{"sku": "U", "quantity": 2, "unit_price_cents": 0,
                                    "subtotal_cents": 0}], "total_cents": 0,
                         "shipment": {"carrier": "DHL", "tracking_no": "Z"}},
            },
            # A legacy return whose order vanished and which carries only a
            # non-candidate sku must not affect the query at all.
            "returns": {"GONE": [
                {"order_id": "GONE", "return_id": "R-GONE",
                 "lines": [{"sku": "U", "quantity": 2}]},
            ]},
        })
        result = self.app.replenishment_report()
        self.assertEqual([row["sku"] for row in result], ["T"])
        self.assertEqual(result[0]["returns"], [])
        self.assertEqual(result[0]["pending"], 0)

    def test_paused_sales_do_not_block_query(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.release_reservation("O1", [{"sku": "T", "quantity": 1}])
        self.app.set_product_enabled("T", False)
        row = self.app.replenishment_report()[0]
        self.assertEqual(row["sku"], "T")
        self.assertEqual(row["needed"], 1)

    # ----------------------------------------------------------------- errors

    def test_missing_catalog_for_candidate_raises(self):
        self._load({
            "products": {"KEEP": {"sku": "KEEP", "name": "Kept", "price_cents": 1}},
            "inventory": {"GONE": {"on_hand": 2, "reserved": 0}},
            "orders": {"O1": {"order_id": "O1", "status": "placed",
                              "lines": [{"sku": "GONE", "quantity": 2, "unit_price_cents": 5,
                                         "subtotal_cents": 10}], "total_cents": 10}},
        })
        with self.assertRaises(ValueError):
            self.app.replenishment_report()

    def test_detail_return_with_bad_order_rejects_whole_query(self):
        base = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "inventory": {"T": {"on_hand": 0, "reserved": 0}},
            "orders": {
                "O1": {"order_id": "O1", "status": "placed",
                       "lines": [{"sku": "T", "quantity": 1, "unit_price_cents": 100,
                                  "subtotal_cents": 100}], "total_cents": 100},
                "PLACED": {"order_id": "PLACED", "status": "placed",
                           "lines": [{"sku": "T", "quantity": 1, "unit_price_cents": 100,
                                      "subtotal_cents": 100}], "total_cents": 100},
            },
        }
        for bad_order, record in (
            ("GONE", {"order_id": "GONE", "return_id": "RG",
                      "lines": [{"sku": "T", "quantity": 1}]}),
            ("PLACED", {"order_id": "PLACED", "return_id": "RP",
                        "lines": [{"sku": "T", "quantity": 1}]}),
        ):
            data = copy_json(base)
            data["returns"] = {bad_order: [record]}
            self._load(data)
            with self.assertRaises(ValueError):
                self.app.replenishment_report()

    # ------------------------------------------------------ read-only / staleness

    def test_query_creates_no_files_and_changes_nothing(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.release_reservation("O1", [{"sku": "T", "quantity": 1}])
        self._shipped_with_t("S1")
        self.app.record_return("S1", "R1", [{"sku": "T", "quantity": 1}])
        before = self.app.path.read_bytes()
        first = self.app.replenishment_report()
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(OrderDesk(self.root).replenishment_report(), first)
        events = self.app.history("O1")["events"]
        self.app.replenishment_report()
        self.assertEqual(self.app.history("O1")["events"], events)
        empty_root = Path(self.temp.name) / "empty2"
        self.assertEqual(OrderDesk(empty_root).replenishment_report(), [])
        self.assertFalse(empty_root.exists())

    def test_reflects_latest_results_after_operations(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.release_reservation("O1", [{"sku": "T", "quantity": 2}])
        self._shipped_with_t("S1", quantity=2)
        self.app.record_return("S1", "R1", [{"sku": "T", "quantity": 2}])
        row = self.by_sku(self.app.replenishment_report())["T"]
        self.assertEqual((row["needed"], row["pending"], row["shortfall"],
                          row["projected_shortfall"]), (2, 2, 0, 0))
        # Receiving the return removes it from pending and raises availability;
        # the item stays listed (still 2 unreserved) but both gaps are zero.
        self.app.receive_return("R1")
        row = self.by_sku(self.app.replenishment_report())["T"]
        self.assertEqual((row["needed"], row["pending"]), (2, 0))
        self.assertEqual((row["shortfall"], row["projected_shortfall"]), (0, 0))
        # A stock count below the demand surfaces a fresh shortfall again.
        self.app.count_stock("CNT1", [{"sku": "T", "on_hand": 1}])
        row = self.by_sku(self.app.replenishment_report())["T"]
        self.assertEqual(row["needed"], 2)
        self.assertEqual(row["shortfall"], 1)
        self.assertEqual(row["projected_shortfall"], 1)
        # Restocking closes the gap but the item stays listed until the
        # reservation itself is topped up.
        self.app.restock("T", 1)
        row = self.by_sku(self.app.replenishment_report())["T"]
        self.assertEqual((row["shortfall"], row["projected_shortfall"]), (0, 0))
        self.app.reserve_order("O1")
        self.assertEqual(self.app.replenishment_report(), [])

    # -------------------------------------------------------------------- CLI

    def test_cli_success_failure_and_array_dispatch(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.release_reservation("O1", [{"sku": "T", "quantity": 1}])
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "replenishment-report"],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual([row["sku"] for row in result], ["T"])
        # An input object is accepted too; the command takes no business args.
        payload = self.root / "q.json"
        payload.write_text("{}", encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "replenishment-report", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        # Outer arrays execute row by row.
        payload.write_text(json.dumps([{}, {}]), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "replenishment-report", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout), [result, result])
        # A candidate missing from the catalog exits 2 with an error object.
        bad_root = Path(self.temp.name) / "bad"
        bad_root.mkdir()
        (bad_root / "data.json").write_text(json.dumps({
            "inventory": {"X": {"on_hand": 1, "reserved": 0}},
            "orders": {"O1": {"order_id": "O1", "status": "placed",
                              "lines": [{"sku": "X", "quantity": 1,
                                         "unit_price_cents": 1, "subtotal_cents": 1}],
                              "total_cents": 1}},
        }), encoding="utf-8")
        failed = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(bad_root),
             "replenishment-report"],
            text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2, failed.stdout)
        self.assertIn("error", json.loads(failed.stderr))

    def _load(self, data):
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        self.app = OrderDesk(self.root)


def copy_json(value):
    return json.loads(json.dumps(value))


if __name__ == "__main__":
    unittest.main()
