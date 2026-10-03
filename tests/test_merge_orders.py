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
        self.app.add_product("U", "Unmanaged", 50)

    def _managed_pair(self):
        # S holds T:2 and C:1, G holds T:3; every product is managed.
        self.app.restock("T", 10)
        self.app.restock("C", 10)
        self.app.place("S", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])
        self.app.place("G", [{"sku": "T", "quantity": 3}])

    def _write_orders(self, source_lines, target_lines):
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "orders": {
                "S": {"order_id": "S", "status": "placed", "lines": source_lines,
                      "total_cents": sum(l.get("subtotal_cents", 0) for l in source_lines)},
                "G": {"order_id": "G", "status": "placed", "lines": target_lines,
                      "total_cents": sum(l.get("subtotal_cents", 0) for l in target_lines)},
            },
        }
        self.app.path.write_text(json.dumps(data), encoding="utf-8")

    def test_merges_lines_amounts_and_status(self):
        self._managed_pair()
        result = self.app.merge_orders(" S ", "G")
        self.assertEqual(set(result), {"source", "target"})
        source = result["source"]
        self.assertEqual(source["order_id"], "S")
        self.assertEqual(source["status"], "cancelled")
        # The source keeps its original lines and amounts.
        self.assertEqual(source["lines"], [
            {"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200},
            {"sku": "C", "quantity": 1, "unit_price_cents": 200, "subtotal_cents": 200},
        ])
        self.assertEqual(source["total_cents"], 400)
        target = result["target"]
        self.assertEqual(target["order_id"], "G")
        self.assertEqual(target["status"], "placed")
        # Source rows follow the target's own rows; duplicate skus stay split.
        self.assertEqual(target["lines"], [
            {"sku": "T", "quantity": 3, "unit_price_cents": 100, "subtotal_cents": 300},
            {"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200},
            {"sku": "C", "quantity": 1, "unit_price_cents": 200, "subtotal_cents": 200},
        ])
        self.assertEqual(target["total_cents"], 700)
        # Both entries follow the full get structure.
        self.assertEqual(result["source"], self.app.get("S"))
        self.assertEqual(result["target"], self.app.get("G"))

    def test_duplicate_rows_and_line_order_preserved(self):
        self.app.place("S", [{"sku": "T", "quantity": 1},
                             {"sku": "C", "quantity": 1},
                             {"sku": "T", "quantity": 2}])
        self.app.place("G", [{"sku": "C", "quantity": 2}])
        result = self.app.merge_orders("S", "G")
        self.assertEqual([(l["sku"], l["quantity"]) for l in result["target"]["lines"]],
                         [("C", 2), ("T", 1), ("C", 1), ("T", 2)])
        self.assertEqual(result["target"]["total_cents"], 900)

    def test_reservations_move_without_touching_stock(self):
        self._managed_pair()
        self.app.place("H", [{"sku": "T", "quantity": 1}])
        before_t = self.app.stock("T")
        before_c = self.app.stock("C")
        self.app.merge_orders("S", "G")
        # Totals, availability and other orders are untouched.
        self.assertEqual(self.app.stock("T"), before_t)
        self.assertEqual(self.app.stock("C"), before_c)
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("S", data["reservations"])
        self.assertEqual(data["reservations"]["G"], {"T": 5, "C": 1})
        self.assertEqual(data["reservations"]["H"], {"T": 1})
        # Ownership changed only: no stock history is recorded.
        self.assertEqual([e["action"] for e in self.app.stock_history("T")["events"]],
                         ["restock", "place", "place", "place"])
        self.assertEqual([e["action"] for e in self.app.stock_history("C")["events"]],
                         ["restock", "place"])

    def test_unmanaged_products_not_auto_managed(self):
        self.app.place("S", [{"sku": "U", "quantity": 2}])
        self.app.place("G", [{"sku": "U", "quantity": 1}])
        self.app.merge_orders("S", "G")
        self.assertEqual(self.app.stock("U"),
                         {"sku": "U", "on_hand": None, "reserved": 0, "available": None})
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("inventory", data)
        self.assertNotIn("reservations", data)

    def test_stray_unmanaged_attribution_reads_as_zero(self):
        data = {
            "products": {"U": {"sku": "U", "name": "Unmanaged", "price_cents": 50}},
            "orders": {
                "S": {"order_id": "S", "status": "placed",
                      "lines": [{"sku": "U", "quantity": 2, "unit_price_cents": 50,
                                 "subtotal_cents": 100}],
                      "total_cents": 100},
                "G": {"order_id": "G", "status": "placed",
                      "lines": [{"sku": "U", "quantity": 1, "unit_price_cents": 50,
                                 "subtotal_cents": 50}],
                      "total_cents": 50},
            },
            "reservations": {"S": {"U": 2}},
        }
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        self.app.merge_orders("S", "G")
        saved = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("inventory", saved)
        self.assertNotIn("S", saved.get("reservations", {}))
        self.assertNotIn("G", saved.get("reservations", {}))

    def test_paused_repriced_and_catalog_missing_do_not_block(self):
        self.app.place("S", [{"sku": "T", "quantity": 1}])
        self.app.place("G", [{"sku": "C", "quantity": 1}])
        self.app.place("J", [{"sku": "C", "quantity": 1}])
        self.app.set_product_enabled("T", False)
        self.app.reprice_products([{"sku": "C", "expected_price_cents": 200, "price_cents": 250}])
        result = self.app.merge_orders("S", "G")
        # Deal prices are preserved, never repriced against the catalog.
        self.assertEqual(result["target"]["lines"], [
            {"sku": "C", "quantity": 1, "unit_price_cents": 200, "subtotal_cents": 200},
            {"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100},
        ])
        # A product missing from the catalog does not block the merge either.
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        del data["products"]["T"]
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        merged = self.app.merge_orders("G", "J")
        self.assertEqual(merged["target"]["status"], "placed")
        self.assertEqual([l["unit_price_cents"] for l in merged["target"]["lines"]],
                         [200, 200, 100])

    def test_zero_price_lines_are_kept(self):
        self.app.add_product("Z", "Free", 0)
        self.app.place("S", [{"sku": "Z", "quantity": 2}])
        self.app.place("G", [{"sku": "T", "quantity": 1}])
        result = self.app.merge_orders("S", "G")
        self.assertEqual(result["target"]["lines"][1],
                         {"sku": "Z", "quantity": 2, "unit_price_cents": 0, "subtotal_cents": 0})
        self.assertEqual(result["target"]["total_cents"], 100)

    def test_input_validation(self):
        self._managed_pair()
        bad_payloads = [
            {"source_id": "  ", "target_id": "G"},
            {"source_id": 1, "target_id": "G"},
            {"source_id": None, "target_id": "G"},
            {"source_id": "S", "target_id": ""},
            {"source_id": "S", "target_id": "  "},
            {"source_id": "S", "target_id": 2},
            {"source_id": "S", "target_id": "S"},
            {"source_id": " S ", "target_id": "S"},
            {"source_id": "nope", "target_id": "G"},
            {"source_id": "S", "target_id": "nope"},
            # Ids are trimmed and case sensitive.
            {"source_id": "s", "target_id": "G"},
            {"source_id": "S", "target_id": "g"},
        ]
        for payload in bad_payloads:
            with self.assertRaises(ValueError, msg=payload):
                self.app.merge_orders(**payload)
        self.app.cancel("S")
        with self.assertRaises(ValueError):
            self.app.merge_orders("S", "G")
        with self.assertRaises(ValueError):
            self.app.merge_orders("G", "S")
        self.app.ship("G", "DHL", "1")
        with self.assertRaises(ValueError):
            self.app.merge_orders("G", "S")

    def test_deal_price_validation(self):
        good = {"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100}
        bad_lines = [
            {"sku": "T", "quantity": 1, "subtotal_cents": 100},
            {"sku": "T", "quantity": 1, "unit_price_cents": None, "subtotal_cents": 100},
            {"sku": "T", "quantity": 1, "unit_price_cents": -1, "subtotal_cents": -1},
            {"sku": "T", "quantity": 1, "unit_price_cents": 1.5, "subtotal_cents": 1},
            {"sku": "T", "quantity": 1, "unit_price_cents": True, "subtotal_cents": 1},
            {"sku": "T", "quantity": 1, "unit_price_cents": "100", "subtotal_cents": 100},
        ]
        for bad in bad_lines:
            for source_lines, target_lines in (([bad], [good]), ([good], [bad])):
                self._write_orders(source_lines, target_lines)
                with self.assertRaises(ValueError, msg=(source_lines, target_lines)):
                    self.app.merge_orders("S", "G")

    def test_inconsistent_merged_prices_rejected(self):
        self._write_orders(
            [{"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100}],
            [{"sku": "T", "quantity": 1, "unit_price_cents": 150, "subtotal_cents": 150}],
        )
        raw = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.merge_orders("S", "G")
        self.assertEqual(self.app.path.read_bytes(), raw)
        # Inconsistency inside one order's own rows rejects the merge too.
        self._write_orders(
            [{"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100}],
            [{"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100},
             {"sku": "T", "quantity": 2, "unit_price_cents": 150, "subtotal_cents": 300}],
        )
        with self.assertRaises(ValueError):
            self.app.merge_orders("S", "G")

    def test_rejected_merge_writes_nothing(self):
        self._managed_pair()
        raw = self.app.path.read_bytes()
        events_before = {oid: len(self.app.history(oid)["events"]) for oid in ("S", "G")}
        for payload in ({"source_id": "S", "target_id": "S"},
                        {"source_id": "nope", "target_id": "G"},
                        {"source_id": "S", "target_id": "nope"}):
            with self.assertRaises(ValueError, msg=payload):
                self.app.merge_orders(**payload)
        self.assertEqual(self.app.path.read_bytes(), raw)
        for order_id in ("S", "G"):
            self.assertEqual(len(self.app.history(order_id)["events"]), events_before[order_id])

    def test_failure_creates_no_file_or_directory(self):
        empty = self.root / "empty"
        app = OrderDesk(empty)
        with self.assertRaises(ValueError):
            app.merge_orders("ghost-1", "ghost-2")
        self.assertFalse(empty.exists())

    def test_both_orders_get_events_with_full_snapshot(self):
        self._managed_pair()
        result = self.app.merge_orders("S", "G")
        for order_id in ("S", "G"):
            history = self.app.history(order_id)
            self.assertTrue(history["complete"])
            self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                             [(1, "place"), (2, "merge-orders")])
            self.assertEqual(set(history["events"][1]), {"sequence", "action", "result"})
            self.assertEqual(history["events"][1]["result"], result)
        self.assertEqual(self.app.history("S")["status"], "cancelled")
        self.assertEqual(self.app.history("G")["status"], "placed")

    def test_legacy_orders_start_history_at_one_incomplete(self):
        line = {"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100}
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "inventory": {"T": {"on_hand": 7, "reserved": 3}},
            "orders": {
                "S": {"order_id": "S", "status": "placed",
                      "lines": [dict(line, quantity=2, subtotal_cents=200)], "total_cents": 200},
                "G": {"order_id": "G", "status": "placed", "lines": [line], "total_cents": 100},
            },
            "reservations": {"S": {"T": 2}, "G": {"T": 1}},
        }
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        result = self.app.merge_orders("S", "G")
        for order_id in ("S", "G"):
            history = self.app.history(order_id)
            self.assertFalse(history["complete"])
            self.assertEqual(history["events"], [
                {"sequence": 1, "action": "merge-orders", "result": result},
            ])
        saved = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("S", saved["reservations"])
        self.assertEqual(saved["reservations"]["G"], {"T": 3})
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 7, "reserved": 3, "available": 4})

    def test_repeat_request_rejected(self):
        self._managed_pair()
        self.app.merge_orders("S", "G")
        # The source is cancelled now, so the identical request is rejected.
        with self.assertRaises(ValueError):
            self.app.merge_orders("S", "G")
        with self.assertRaises(ValueError):
            self.app.merge_orders("G", "S")

    def test_queries_reflect_merged_content(self):
        self._managed_pair()
        self.app.merge_orders("S", "G")
        pick = self.app.pick_list(["G"])
        self.assertEqual(pick["lines"], [
            {"sku": "C", "quantity": 1, "reserved": 1, "available": 9, "shortfall": 0,
             "orders": [{"order_id": "G", "quantity": 1, "reserved": 1}]},
            {"sku": "T", "quantity": 5, "reserved": 5, "available": 5, "shortfall": 0,
             "orders": [{"order_id": "G", "quantity": 5, "reserved": 5}]},
        ])
        audit = self.app.reservation_audit("T")
        self.assertEqual(audit["allocated"], 5)
        self.assertEqual(audit["difference"], 0)
        self.assertEqual(audit["orders"],
                         [{"order_id": "G", "quantity": 5, "reserved": 5, "unreserved": 0}])
        progress = self.app.order_progress("G")
        self.assertEqual({line["sku"]: line["reserved"] for line in progress["lines"]},
                         {"C": 1, "T": 5})

    def test_later_operations_follow_existing_rules(self):
        self._managed_pair()
        self.app.merge_orders("S", "G")
        # The merged target ships only what it actually holds.
        self.app.ship("G", "DHL", "1")
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 5, "reserved": 0, "available": 5})
        self.assertEqual(self.app.stock("C"),
                         {"sku": "C", "on_hand": 9, "reserved": 0, "available": 9})

    def test_source_can_be_reopened(self):
        self._managed_pair()
        self.app.merge_orders("S", "G")
        reopened = self.app.reopen_order("S")
        self.assertEqual(reopened["status"], "placed")
        self.assertEqual([(l["sku"], l["quantity"]) for l in reopened["lines"]],
                         [("T", 2), ("C", 1)])
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(data["reservations"]["S"], {"T": 2, "C": 1})

    def test_carts_and_other_orders_untouched(self):
        self._managed_pair()
        self.app.save_cart("K", [{"sku": "T", "quantity": 1}])
        self.app.place("H", [{"sku": "C", "quantity": 1}])
        before_cart = self.app.get_cart("K")
        before_h = self.app.get("H")
        self.app.merge_orders("S", "G")
        self.assertEqual(self.app.get_cart("K"), before_cart)
        self.assertEqual(self.app.get("H"), before_h)

    def test_persists_across_reopen(self):
        self._managed_pair()
        result = self.app.merge_orders("S", "G")
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.get("S"), result["source"])
        self.assertEqual(reopened.get("G"), result["target"])
        for order_id in ("S", "G"):
            events = reopened.history(order_id)["events"]
            self.assertEqual(events[-1]["action"], "merge-orders")
            self.assertEqual(events[-1]["result"], result)
        self.assertEqual(reopened.stock("T")["reserved"], 5)
        self.assertEqual(reopened.stock("C")["reserved"], 1)

    def test_cli_success_failure_and_array_independence(self):
        self.app.restock("T", 10)
        self.app.place("X1", [{"sku": "T", "quantity": 2}])
        self.app.place("X2", [{"sku": "T", "quantity": 3}])
        self.app.place("X3", [{"sku": "T", "quantity": 1}])
        payload = self.root / "merge.json"
        payload.write_text(json.dumps({"source_id": " X1 ", "target_id": "X2"}),
                           encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                             "merge-orders", str(payload)],
                            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        value = json.loads(ok.stdout)
        self.assertEqual(set(value), {"source", "target"})
        self.assertEqual(value["source"]["status"], "cancelled")
        self.assertEqual(value["source"]["order_id"], "X1")
        self.assertEqual(value["target"]["total_cents"], 500)
        payload.write_text(json.dumps([
            {"source_id": "X3", "target_id": "X2"},
            {"source_id": "X1", "target_id": "X2"},
        ]), encoding="utf-8")
        run = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                              "merge-orders", str(payload)],
                             text=True, capture_output=True)
        self.assertEqual(run.returncode, 2, run.stdout)
        reopened = OrderDesk(self.root)
        # The first array item succeeded; the failed second item (source
        # already cancelled) did not roll it back or consume another sequence.
        self.assertEqual([e["action"] for e in reopened.history("X2")["events"]],
                         ["place", "merge-orders", "merge-orders"])
        self.assertEqual(reopened.get("X3")["status"], "cancelled")
        self.assertEqual(reopened.stock("T")["reserved"], 6)

if __name__ == "__main__":
    unittest.main()
