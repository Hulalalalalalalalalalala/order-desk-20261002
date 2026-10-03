import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk


class MergeOrdersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)
        self.app.add_product("Z", "Zero", 0)
        self.app.add_product("U", "Unmanaged", 50)

    def _pair(self):
        # Both orders placed while everything is managed, each holding real
        # reservations; a third order holds reservations that must not move.
        self.app.restock("T", 20)
        self.app.restock("C", 20)
        self.app.place("S", [
            {"sku": "T", "quantity": 1},
            {"sku": "C", "quantity": 2},
            {"sku": "T", "quantity": 2},
        ])
        self.app.place("G", [{"sku": "T", "quantity": 4}, {"sku": "C", "quantity": 1}])
        self.app.place("O", [{"sku": "T", "quantity": 3}])

    def test_merges_lines_status_and_amount(self):
        self._pair()
        result = self.app.merge_orders(" S ", "G")
        self.assertEqual(set(result), {"source", "target"})
        source = result["source"]
        target = result["target"]
        self.assertEqual(source["order_id"], "S")
        self.assertEqual(source["status"], "cancelled")
        # Source keeps its original lines and amount.
        self.assertEqual(source["total_cents"], 700)
        self.assertEqual(source["lines"], [
            {"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100},
            {"sku": "C", "quantity": 2, "unit_price_cents": 200, "subtotal_cents": 400},
            {"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200},
        ])
        self.assertEqual(target["order_id"], "G")
        self.assertEqual(target["status"], "placed")
        # Source rows are appended after the target's own rows, keeping order,
        # duplicate skus, quantities, deal prices and subtotals.
        self.assertEqual(target["lines"], [
            {"sku": "T", "quantity": 4, "unit_price_cents": 100, "subtotal_cents": 400},
            {"sku": "C", "quantity": 1, "unit_price_cents": 200, "subtotal_cents": 200},
            {"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100},
            {"sku": "C", "quantity": 2, "unit_price_cents": 200, "subtotal_cents": 400},
            {"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200},
        ])
        # Total is the sum of the two original amounts: 600 + 700.
        self.assertEqual(target["total_cents"], 1300)
        # The returned snapshots match subsequent get calls exactly.
        self.assertEqual(self.app.get("S"), source)
        self.assertEqual(self.app.get("G"), target)

    def test_deal_prices_are_never_repriced_and_zero_rows_keep(self):
        self.app.restock("T", 20)
        self.app.restock("Z", 20)
        self.app.place("S", [{"sku": "T", "quantity": 2}, {"sku": "Z", "quantity": 3}])
        self.app.place("G", [{"sku": "T", "quantity": 1}])
        self.app.reprice_products([
            {"sku": "T", "expected_price_cents": 100, "price_cents": 150},
            {"sku": "Z", "expected_price_cents": 0, "price_cents": 0},
        ])
        result = self.app.merge_orders("S", "G")
        self.assertEqual(result["target"]["lines"], [
            {"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100},
            {"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200},
            {"sku": "Z", "quantity": 3, "unit_price_cents": 0, "subtotal_cents": 0},
        ])
        self.assertEqual(result["target"]["total_cents"], 300)

    def test_different_deal_prices_across_orders_are_allowed(self):
        self.app.restock("T", 20)
        self.app.place("S", [{"sku": "T", "quantity": 2}])
        self.app.reprice_products([
            {"sku": "T", "expected_price_cents": 100, "price_cents": 300},
        ])
        self.app.place("G", [{"sku": "T", "quantity": 1}])
        result = self.app.merge_orders("S", "G")
        self.assertEqual(result["target"]["lines"], [
            {"sku": "T", "quantity": 1, "unit_price_cents": 300, "subtotal_cents": 300},
            {"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200},
        ])
        self.assertEqual(result["target"]["total_cents"], 500)

    def test_reservations_sum_by_sku_onto_target(self):
        self._pair()
        before_t = self.app.stock("T")
        before_c = self.app.stock("C")
        self.app.merge_orders("S", "G")
        # On hand, total reserved and availability never change per product.
        self.assertEqual(self.app.stock("T"), before_t)
        self.assertEqual(self.app.stock("C"), before_c)
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("S", data["reservations"])
        # S held T:3, C:2; G held T:4, C:1 -> G now holds T:7, C:3.
        self.assertEqual(data["reservations"]["G"], {"T": 7, "C": 3})
        # The other order's attribution is untouched.
        self.assertEqual(data["reservations"]["O"], {"T": 3})

    def test_unmanaged_products_hold_no_transferred_reservation(self):
        # S is ordered while T/U are unmanaged (no reservations); T is managed
        # afterwards and G takes real reservations.
        self.app.place("S", [{"sku": "T", "quantity": 6}, {"sku": "U", "quantity": 2}])
        self.app.restock("T", 5)
        self.app.place("G", [{"sku": "T", "quantity": 3}])
        result = self.app.merge_orders("S", "G")
        self.assertEqual(result["target"]["lines"], [
            {"sku": "T", "quantity": 3, "unit_price_cents": 100, "subtotal_cents": 300},
            {"sku": "T", "quantity": 6, "unit_price_cents": 100, "subtotal_cents": 600},
            {"sku": "U", "quantity": 2, "unit_price_cents": 50, "subtotal_cents": 100},
        ])
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("S", data["reservations"])
        # Only G's real T reservation survives; U is never managed.
        self.assertEqual(data["reservations"]["G"], {"T": 3})
        self.assertEqual(self.app.stock("T")["reserved"], 3)

    def test_missing_collections_read_as_empty(self):
        data = {
            "products": {
                "T": {"sku": "T", "name": "Tea", "price_cents": 100},
                "U": {"sku": "U", "name": "Unmanaged", "price_cents": 50},
            },
            "orders": {
                "S": {"order_id": "S", "status": "placed",
                      "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100,
                                 "subtotal_cents": 200},
                                {"sku": "U", "quantity": 1, "unit_price_cents": 50,
                                 "subtotal_cents": 50}],
                      "total_cents": 250},
                "G": {"order_id": "G", "status": "placed",
                      "lines": [{"sku": "T", "quantity": 1, "unit_price_cents": 100,
                                 "subtotal_cents": 100}],
                      "total_cents": 100},
            },
        }
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        result = app.merge_orders("S", "G")
        self.assertEqual(result["target"]["total_cents"], 350)
        saved = json.loads(app.path.read_text(encoding="utf-8"))
        # No inventory/reservations collections are fabricated.
        self.assertNotIn("inventory", saved)
        self.assertNotIn("reservations", saved)

    def test_paused_and_catalog_missing_products_do_not_block(self):
        self.app.restock("T", 10)
        self.app.place("S", [{"sku": "T", "quantity": 2}])
        self.app.place("G", [{"sku": "T", "quantity": 1}])
        self.app.set_product_enabled("T", False)
        before = self.app.stock("T")
        self.app.merge_orders("S", "G")
        self.assertEqual(self.app.stock("T"), before)
        self.assertEqual(self.app.get("G")["status"], "placed")

    def test_input_validation(self):
        self._pair()
        bad = [
            {"source_id": "  ", "target_id": "G"},
            {"source_id": 1, "target_id": "G"},
            {"source_id": None, "target_id": "G"},
            {"source_id": "S", "target_id": "  "},
            {"source_id": "S", "target_id": 3},
            {"source_id": "S", "target_id": "S"},
            {"source_id": "nope", "target_id": "G"},
            {"source_id": "S", "target_id": "nope"},
        ]
        for payload in bad:
            with self.assertRaises(ValueError, msg=payload):
                self.app.merge_orders(**payload)
        # Ids are trimmed and case sensitive.
        with self.assertRaises(ValueError):
            self.app.merge_orders("s", "G")
        self.app.cancel("O")
        self.app.ship("G", "DHL", "1")
        with self.assertRaises(ValueError):
            self.app.merge_orders("O", "S")
        with self.assertRaises(ValueError):
            self.app.merge_orders("S", "O")
        with self.assertRaises(ValueError):
            self.app.merge_orders("G", "S")

    def test_invalid_deal_prices_reject_the_whole_merge(self):
        def order(oid, lines, total):
            return {"order_id": oid, "status": "placed", "lines": lines, "total_cents": total}

        line = {"sku": "T", "quantity": 1, "subtotal_cents": 100}
        cases = {
            "missing": dict(line),
            "negative": {**line, "unit_price_cents": -1},
            "float": {**line, "unit_price_cents": 1.5},
            "boolean": {**line, "unit_price_cents": True},
            "string": {**line, "unit_price_cents": "100"},
        }
        for label, bad_line in cases.items():
            data = {
                "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
                "inventory": {"T": {"on_hand": 9, "reserved": 2}},
                "orders": {
                    "S": order("S", [bad_line], 100),
                    "G": order("G", [{"sku": "T", "quantity": 1, "unit_price_cents": 100,
                                      "subtotal_cents": 100}], 100),
                },
                "reservations": {"S": {"T": 1}, "G": {"T": 1}},
            }
            self.root.mkdir(parents=True, exist_ok=True)
            OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError, msg=label):
                OrderDesk(self.root).merge_orders("S", "G")
        # Inconsistent deal prices for the same sku within one order also reject.
        inconsistent = order("S", [
            {"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100},
            {"sku": "T", "quantity": 1, "unit_price_cents": 200, "subtotal_cents": 200},
        ], 300)
        data["orders"]["S"] = inconsistent
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            OrderDesk(self.root).merge_orders("S", "G")

    def test_rejected_merge_writes_nothing(self):
        self._pair()
        raw = self.app.path.read_bytes()
        events_s = len(self.app.history("S")["events"])
        with self.assertRaises(ValueError):
            self.app.merge_orders("S", "S")
        self.assertEqual(self.app.path.read_bytes(), raw)
        self.assertEqual(len(self.app.history("S")["events"]), events_s)
        self.assertEqual(self.app.get("S")["status"], "placed")

    def test_failure_creates_no_file_or_directory(self):
        empty = self.root / "empty"
        app = OrderDesk(empty)
        with self.assertRaises(ValueError):
            app.merge_orders("ghost-1", "ghost-2")
        self.assertFalse(empty.exists())

    def test_both_orders_get_merge_events_with_full_snapshot(self):
        self._pair()
        result = self.app.merge_orders("S", "G")
        for order_id in ("S", "G"):
            history = self.app.history(order_id)
            self.assertTrue(history["complete"])
            self.assertEqual(history["events"][-1]["action"], "merge-orders")
            self.assertEqual(history["events"][-1]["sequence"], 2)
            self.assertEqual(set(history["events"][-1]), {"sequence", "action", "result"})
            self.assertEqual(history["events"][-1]["result"], result)
        # No stock events are recorded by a merge.
        self.assertEqual([e["action"] for e in self.app.stock_history("T")["events"]],
                         ["restock", "place", "place", "place"])

    def test_legacy_orders_start_history_at_one_incomplete(self):
        line = {"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100}
        order = {"status": "placed", "lines": [line], "total_cents": 100}
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "inventory": {"T": {"on_hand": 7, "reserved": 3}},
            "orders": {"OLD": dict(order, order_id="OLD"),
                       "NEW": dict(order, order_id="NEW")},
            "reservations": {"OLD": {"T": 1}, "NEW": {"T": 2}},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        result = app.merge_orders("OLD", "NEW")
        for order_id in ("OLD", "NEW"):
            history = app.history(order_id)
            self.assertFalse(history["complete"])
            self.assertEqual(history["events"], [
                {"sequence": 1, "action": "merge-orders", "result": result},
            ])
        self.assertEqual(app.stock("T"),
                         {"sku": "T", "on_hand": 7, "reserved": 3, "available": 4})

    def test_persists_across_reopen(self):
        self._pair()
        result = self.app.merge_orders("S", "G")
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.get("S"), result["source"])
        self.assertEqual(reopened.get("G"), result["target"])
        for order_id in ("S", "G"):
            events = reopened.history(order_id)["events"]
            self.assertEqual(events[-1]["action"], "merge-orders")
            self.assertEqual(events[-1]["result"], result)
        self.assertEqual(reopened.stock("T")["reserved"], 10)
        self.assertEqual(reopened.stock("C")["reserved"], 3)

    def test_pick_and_fulfillment_queries_show_merged_content(self):
        self._pair()
        self.app.merge_orders("S", "G")
        # Only placed orders are picked: G now carries the merged quantities.
        pick = self.app.pick_list(["G", "O"])
        rows = {line["sku"]: line for line in pick["lines"]}
        self.assertEqual(rows["T"]["quantity"], 10)  # 7 merged + 3 other order
        self.assertEqual(rows["T"]["reserved"], 10)
        self.assertEqual(rows["C"]["quantity"], 3)
        self.assertEqual(rows["C"]["reserved"], 3)
        detail = {row["order_id"]: row for row in rows["T"]["orders"]}
        self.assertEqual(detail["G"], {"order_id": "G", "quantity": 7, "reserved": 7})
        self.assertEqual(detail["O"], {"order_id": "O", "quantity": 3, "reserved": 3})
        audit_t = self.app.reservation_audit("T")
        g_row = next(row for row in audit_t["orders"] if row["order_id"] == "G")
        self.assertEqual(g_row, {"order_id": "G", "quantity": 7, "reserved": 7, "unreserved": 0})
        self.assertTrue(all(row["order_id"] != "S" for row in audit_t["orders"]))
        # Worklist: G is fully reserved and ready to ship; S has no tasks.
        worklist = {entry["order_id"]: entry["tasks"]
                    for entry in self.app.order_worklist("all")}
        self.assertEqual(worklist["G"], ["ship"])
        self.assertEqual(worklist["S"], [])

    def test_later_cancel_ship_reopen_and_amend_follow_rules(self):
        self._pair()
        self.app.merge_orders("S", "G")
        # Shipping G deducts only the merged actual reservations (T:7, C:3).
        self.app.ship("G", "DHL", "TRK")
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 13, "reserved": 3, "available": 10})
        self.assertEqual(self.app.stock("C"),
                         {"sku": "C", "on_hand": 17, "reserved": 0, "available": 17})
        # The cancelled source can be reopened under its original id.
        self.app.reopen_order("S")
        self.assertEqual(self.app.get("S")["status"], "placed")
        self.assertEqual(self.app.get("S")["total_cents"], 700)

    def test_repeat_request_rejected_because_source_cancelled(self):
        self._pair()
        self.app.merge_orders("S", "G")
        with self.assertRaises(ValueError):
            self.app.merge_orders("S", "G")
        with self.assertRaises(ValueError):
            self.app.merge_orders("G", "S")

    def test_carts_other_orders_and_returns_keep_unchanged(self):
        self._pair()
        self.app.save_cart("K", [{"sku": "T", "quantity": 5}])
        self.app.merge_orders("S", "G")
        self.assertEqual(self.app.get_cart("K"),
                         {"cart_id": "K", "lines": [{"sku": "T", "quantity": 5}]})
        self.assertEqual(self.app.get("O")["status"], "placed")

    def test_cli_success_failure_and_array_independence(self):
        self.app.restock("T", 20)
        self.app.place("X1", [{"sku": "T", "quantity": 2}])
        self.app.place("X2", [{"sku": "T", "quantity": 3}])
        self.app.place("X3", [{"sku": "T", "quantity": 1}])
        payload = self.root / "mg.json"
        payload.write_text(json.dumps({"source_id": " X1 ", "target_id": "X2"}),
                           encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                             "merge-orders", str(payload)],
                            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        value = json.loads(ok.stdout)
        self.assertEqual(set(value), {"source", "target"})
        self.assertEqual(value["source"]["status"], "cancelled")
        self.assertEqual(value["target"]["lines"], [
            {"sku": "T", "quantity": 3, "unit_price_cents": 100, "subtotal_cents": 300},
            {"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200},
        ])
        # Outer array: the first item succeeds; the later failing item does not
        # roll it back and aborts the run with exit code 2.
        payload.write_text(json.dumps([
            {"source_id": "X3", "target_id": "X2"},
            {"source_id": "X1", "target_id": "X2"},
        ]), encoding="utf-8")
        run = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                              "merge-orders", str(payload)],
                             text=True, capture_output=True)
        self.assertEqual(run.returncode, 2, run.stdout)
        reopened = OrderDesk(self.root)
        # X3 was merged into X2 by the second, independent array entry.
        self.assertEqual(reopened.get("X3")["status"], "cancelled")
        self.assertEqual([line["quantity"] for line in reopened.get("X2")["lines"]],
                         [3, 2, 1])


if __name__ == "__main__":
    unittest.main()
