import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk

class SplitOrderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)
        self.app.add_product("U", "Unmanaged", 50)

    def _managed_source(self):
        # S holds T:2 and C:1 with every product managed and reserved.
        self.app.restock("T", 10)
        self.app.restock("C", 10)
        self.app.place("S", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])

    def _write_source(self, lines, **extra):
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "orders": {
                "S": {"order_id": "S", "status": "placed", "lines": lines,
                      "total_cents": sum(l.get("subtotal_cents", 0) for l in lines)},
            },
        }
        data.update(extra)
        self.app.path.write_text(json.dumps(data), encoding="utf-8")

    def test_splits_lines_amounts_and_status(self):
        self._managed_source()
        result = self.app.split_order(" S ", "G", [{"sku": "T", "quantity": 1}])
        self.assertEqual(set(result), {"source", "target"})
        source = result["source"]
        self.assertEqual(source["order_id"], "S")
        self.assertEqual(source["status"], "placed")
        self.assertEqual(source["lines"], [
            {"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100},
            {"sku": "C", "quantity": 1, "unit_price_cents": 200, "subtotal_cents": 200},
        ])
        self.assertEqual(source["total_cents"], 300)
        target = result["target"]
        self.assertEqual(target["order_id"], "G")
        self.assertEqual(target["status"], "placed")
        self.assertEqual(target["lines"], [
            {"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100},
        ])
        self.assertEqual(target["total_cents"], 100)
        # Both entries follow the full get structure.
        self.assertEqual(result["source"], self.app.get("S"))
        self.assertEqual(result["target"], self.app.get("G"))
        # The two totals sum to the original total.
        self.assertEqual(source["total_cents"] + target["total_cents"], 400)

    def test_takes_from_last_row_and_keeps_duplicate_rows(self):
        self.app.place("S", [{"sku": "T", "quantity": 1},
                             {"sku": "C", "quantity": 1},
                             {"sku": "T", "quantity": 2}])
        result = self.app.split_order("S", "G", [{"sku": "T", "quantity": 2}])
        # The last T row (2) is taken whole; the first T row survives.
        self.assertEqual([(l["sku"], l["quantity"]) for l in result["source"]["lines"]],
                         [("T", 1), ("C", 1)])
        self.assertEqual([(l["sku"], l["quantity"]) for l in result["target"]["lines"]],
                         [("T", 2)])
        # A partial take splits the last row and keeps row order on both sides.
        result = self.app.split_order("S", "H", [{"sku": "C", "quantity": 1}])
        self.assertEqual([(l["sku"], l["quantity"]) for l in result["source"]["lines"]],
                         [("T", 1)])
        self.assertEqual([(l["sku"], l["quantity"]) for l in result["target"]["lines"]],
                         [("C", 1)])
        self.assertEqual(result["source"]["total_cents"], 100)
        self.assertEqual(result["target"]["total_cents"], 200)

    def test_zero_price_rows_are_kept(self):
        self.app.add_product("Z", "Free", 0)
        self.app.place("S", [{"sku": "Z", "quantity": 2}, {"sku": "T", "quantity": 1}])
        result = self.app.split_order("S", "G", [{"sku": "Z", "quantity": 1}])
        self.assertEqual(result["target"]["lines"], [
            {"sku": "Z", "quantity": 1, "unit_price_cents": 0, "subtotal_cents": 0},
        ])
        self.assertEqual(result["target"]["total_cents"], 0)
        self.assertEqual(result["source"]["lines"][0],
                         {"sku": "Z", "quantity": 1, "unit_price_cents": 0, "subtotal_cents": 0})

    def test_input_validation(self):
        self._managed_source()
        bad_payloads = [
            {"source_id": "  ", "target_id": "G", "lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": 1, "target_id": "G", "lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": None, "target_id": "G", "lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": "S", "target_id": "", "lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": "S", "target_id": "  ", "lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": "S", "target_id": 2, "lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": "S", "target_id": "S", "lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": " S ", "target_id": "S", "lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": "nope", "target_id": "G", "lines": [{"sku": "T", "quantity": 1}]},
            # Ids are trimmed and case sensitive.
            {"source_id": "s", "target_id": "G", "lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": "S", "target_id": "G", "lines": []},
            {"source_id": "S", "target_id": "G", "lines": "T"},
            {"source_id": "S", "target_id": "G", "lines": ["T"]},
            {"source_id": "S", "target_id": "G", "lines": [{"sku": "T"}]},
            {"source_id": "S", "target_id": "G", "lines": [{"quantity": 1}]},
            {"source_id": "S", "target_id": "G", "lines": [{"sku": " ", "quantity": 1}]},
            {"source_id": "S", "target_id": "G", "lines": [{"sku": "T", "quantity": 0}]},
            {"source_id": "S", "target_id": "G", "lines": [{"sku": "T", "quantity": -1}]},
            {"source_id": "S", "target_id": "G", "lines": [{"sku": "T", "quantity": 1.5}]},
            {"source_id": "S", "target_id": "G", "lines": [{"sku": "T", "quantity": True}]},
            {"source_id": "S", "target_id": "G", "lines": [{"sku": "T", "quantity": "1"}]},
            # Sku matching is trimmed and case sensitive.
            {"source_id": "S", "target_id": "G", "lines": [{"sku": "t", "quantity": 1}]},
            {"source_id": "S", "target_id": "G", "lines": [{"sku": "U", "quantity": 1}]},
            {"source_id": "S", "target_id": "G", "lines": [{"sku": "T", "quantity": 3}]},
            # Splitting the whole order away is rejected.
            {"source_id": "S", "target_id": "G",
             "lines": [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}]},
        ]
        for payload in bad_payloads:
            with self.assertRaises(ValueError, msg=payload):
                self.app.split_order(**payload)
        self.app.cancel("S")
        with self.assertRaises(ValueError):
            self.app.split_order("S", "G", [{"sku": "T", "quantity": 1}])

    def test_duplicate_skus_merge_and_extra_fields_ignored(self):
        self._managed_source()
        result = self.app.split_order(
            "S", "G",
            [{"sku": " T ", "quantity": 1, "note": "x"}, {"sku": "T", "quantity": 1}],
        )
        self.assertEqual(result["target"]["lines"], [
            {"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200},
        ])
        self.assertEqual([(l["sku"], l["quantity"]) for l in result["source"]["lines"]],
                         [("C", 1)])

    def test_target_id_may_match_a_cart(self):
        self._managed_source()
        self.app.save_cart("G", [{"sku": "T", "quantity": 1}])
        result = self.app.split_order("S", "G", [{"sku": "T", "quantity": 1}])
        self.assertEqual(result["target"]["order_id"], "G")
        # The cart itself is untouched.
        self.assertEqual(self.app.get_cart("G"),
                         {"cart_id": "G", "lines": [{"sku": "T", "quantity": 1}]})

    def test_repeat_request_rejected_by_occupied_id(self):
        self._managed_source()
        self.app.split_order("S", "G", [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.split_order("S", "G", [{"sku": "C", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.split_order("G", "S", [{"sku": "T", "quantity": 1}])

    def test_deal_price_validation(self):
        bad_lines = [
            {"sku": "T", "quantity": 1, "subtotal_cents": 100},
            {"sku": "T", "quantity": 1, "unit_price_cents": None, "subtotal_cents": 100},
            {"sku": "T", "quantity": 1, "unit_price_cents": -1, "subtotal_cents": -1},
            {"sku": "T", "quantity": 1, "unit_price_cents": 1.5, "subtotal_cents": 1},
            {"sku": "T", "quantity": 1, "unit_price_cents": True, "subtotal_cents": 1},
            {"sku": "T", "quantity": 1, "unit_price_cents": "100", "subtotal_cents": 100},
        ]
        for bad in bad_lines:
            self._write_source([bad, {"sku": "T", "quantity": 1, "unit_price_cents": 100,
                                      "subtotal_cents": 100}])
            with self.assertRaises(ValueError, msg=bad):
                self.app.split_order("S", "G", [{"sku": "T", "quantity": 1}])

    def test_inconsistent_prices_rejected(self):
        self._write_source([
            {"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100},
            {"sku": "T", "quantity": 2, "unit_price_cents": 150, "subtotal_cents": 300},
        ])
        raw = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.split_order("S", "G", [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.path.read_bytes(), raw)

    def test_paused_repriced_and_catalog_missing_do_not_block(self):
        self._managed_source()
        self.app.set_product_enabled("T", False)
        self.app.reprice_products([{"sku": "C", "expected_price_cents": 200, "price_cents": 250}])
        result = self.app.split_order("S", "G", [{"sku": "T", "quantity": 1},
                                                 {"sku": "C", "quantity": 1}])
        # Deal prices are preserved, never repriced against the catalog.
        self.assertEqual(result["target"]["lines"], [
            {"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100},
            {"sku": "C", "quantity": 1, "unit_price_cents": 200, "subtotal_cents": 200},
        ])
        # A product missing from the catalog does not block the split either.
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        del data["products"]["T"]
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        moved = self.app.split_order("G", "H", [{"sku": "T", "quantity": 1}])
        self.assertEqual(moved["target"]["status"], "placed")

    def test_insufficient_availability_does_not_block(self):
        self.app.restock("T", 1)
        self.app.place("S", [{"sku": "T", "quantity": 1}, {"sku": "U", "quantity": 1}])
        # No stock left at all; splitting the reserved order still works.
        result = self.app.split_order("S", "G", [{"sku": "T", "quantity": 1}])
        self.assertEqual(result["target"]["status"], "placed")
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(data["reservations"]["G"], {"T": 1})
        self.assertNotIn("S", data["reservations"])

    def test_reservations_split_without_touching_stock(self):
        self._managed_source()
        self.app.place("H", [{"sku": "T", "quantity": 1}])
        before_t = self.app.stock("T")
        before_c = self.app.stock("C")
        result = self.app.split_order("S", "G", [{"sku": "T", "quantity": 1},
                                                 {"sku": "C", "quantity": 1}])
        # Totals, availability and other orders are untouched.
        self.assertEqual(self.app.stock("T"), before_t)
        self.assertEqual(self.app.stock("C"), before_c)
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        # S keeps min(old, remaining): T 2->1 remaining keeps 1, C 1->0 keeps 0.
        self.assertEqual(data["reservations"]["S"], {"T": 1})
        self.assertEqual(data["reservations"]["G"], {"T": 1, "C": 1})
        self.assertEqual(data["reservations"]["H"], {"T": 1})
        # Ownership changed only: no stock history is recorded.
        self.assertEqual([e["action"] for e in self.app.stock_history("T")["events"]],
                         ["restock", "place", "place"])
        self.assertEqual([e["action"] for e in self.app.stock_history("C")["events"]],
                         ["restock", "place"])
        self.assertEqual(result["source"]["total_cents"] + result["target"]["total_cents"], 400)

    def test_reservation_attribution_dropped_when_empty(self):
        # S holds a managed T row and an unmanaged U row; splitting the whole
        # T row away empties S's reservation record and drops its attribution.
        self.app.restock("T", 10)
        self.app.place("S", [{"sku": "T", "quantity": 1}, {"sku": "U", "quantity": 1}])
        self.app.split_order("S", "G", [{"sku": "T", "quantity": 1}])
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("S", data["reservations"])
        self.assertEqual(data["reservations"]["G"], {"T": 1})
        self.assertEqual([(l["sku"], l["quantity"]) for l in self.app.get("S")["lines"]],
                         [("U", 1)])

    def test_unmanaged_products_not_auto_managed(self):
        self.app.place("S", [{"sku": "U", "quantity": 2}, {"sku": "T", "quantity": 1}])
        result = self.app.split_order("S", "G", [{"sku": "U", "quantity": 1}])
        self.assertEqual(self.app.stock("U"),
                         {"sku": "U", "on_hand": None, "reserved": 0, "available": None})
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("inventory", data)
        self.assertNotIn("reservations", data)
        self.assertEqual(result["target"]["lines"][0]["quantity"], 1)

    def test_missing_reservation_record_reads_as_none(self):
        # Legacy placed order with managed stock but no attribution record.
        self._write_source(
            [{"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200}],
            inventory={"T": {"on_hand": 5, "reserved": 0}},
        )
        result = self.app.split_order("S", "G", [{"sku": "T", "quantity": 1}])
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("reservations", data)
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 5, "reserved": 0, "available": 5})
        self.assertEqual(result["target"]["status"], "placed")

    def test_both_orders_get_events_with_full_snapshot(self):
        self._managed_source()
        result = self.app.split_order("S", "G", [{"sku": "T", "quantity": 1}])
        source_history = self.app.history("S")
        self.assertTrue(source_history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in source_history["events"]],
                         [(1, "place"), (2, "split-order")])
        target_history = self.app.history("G")
        self.assertTrue(target_history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in target_history["events"]],
                         [(1, "split-order")])
        for order_id in ("S", "G"):
            events = self.app.history(order_id)["events"]
            self.assertEqual(set(events[-1]), {"sequence", "action", "result"})
            self.assertEqual(events[-1]["result"], result)
        self.assertEqual(self.app.history("S")["status"], "placed")
        self.assertEqual(self.app.history("G")["status"], "placed")

    def test_legacy_source_starts_history_at_one_incomplete(self):
        self._write_source(
            [{"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200}],
            inventory={"T": {"on_hand": 7, "reserved": 2}},
            reservations={"S": {"T": 2}},
        )
        result = self.app.split_order("S", "G", [{"sku": "T", "quantity": 1}])
        source_history = self.app.history("S")
        self.assertFalse(source_history["complete"])
        self.assertEqual(source_history["events"], [
            {"sequence": 1, "action": "split-order", "result": result},
        ])
        target_history = self.app.history("G")
        self.assertTrue(target_history["complete"])
        self.assertEqual(target_history["events"], [
            {"sequence": 1, "action": "split-order", "result": result},
        ])
        saved = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["reservations"]["S"], {"T": 1})
        self.assertEqual(saved["reservations"]["G"], {"T": 1})
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 7, "reserved": 2, "available": 5})

    def test_rejected_split_writes_nothing(self):
        self._managed_source()
        raw = self.app.path.read_bytes()
        events_before = len(self.app.history("S")["events"])
        for payload in ({"source_id": "S", "target_id": "S", "lines": [{"sku": "T", "quantity": 1}]},
                        {"source_id": "nope", "target_id": "G", "lines": [{"sku": "T", "quantity": 1}]},
                        {"source_id": "S", "target_id": "G", "lines": [{"sku": "T", "quantity": 9}]},
                        {"source_id": "S", "target_id": "G",
                         "lines": [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}]}):
            with self.assertRaises(ValueError, msg=payload):
                self.app.split_order(**payload)
        self.assertEqual(self.app.path.read_bytes(), raw)
        self.assertEqual(len(self.app.history("S")["events"]), events_before)
        self.assertNotIn("G", json.loads(self.app.path.read_text(encoding="utf-8"))["orders"])

    def test_failure_creates_no_file_or_directory(self):
        empty = self.root / "empty"
        app = OrderDesk(empty)
        with self.assertRaises(ValueError):
            app.split_order("ghost-1", "ghost-2", [{"sku": "T", "quantity": 1}])
        self.assertFalse(empty.exists())

    def test_queries_reflect_split_content(self):
        self._managed_source()
        self.app.split_order("S", "G", [{"sku": "T", "quantity": 1}])
        pick = self.app.pick_list(["S", "G"])
        self.assertEqual(pick["lines"], [
            {"sku": "C", "quantity": 1, "reserved": 1, "available": 9, "shortfall": 0,
             "orders": [{"order_id": "S", "quantity": 1, "reserved": 1}]},
            {"sku": "T", "quantity": 2, "reserved": 2, "available": 8, "shortfall": 0,
             "orders": [{"order_id": "G", "quantity": 1, "reserved": 1},
                        {"order_id": "S", "quantity": 1, "reserved": 1}]},
        ])
        audit = self.app.reservation_audit("T")
        self.assertEqual(audit["allocated"], 2)
        self.assertEqual(audit["difference"], 0)
        self.assertEqual(audit["orders"],
                         [{"order_id": "G", "quantity": 1, "reserved": 1, "unreserved": 0},
                          {"order_id": "S", "quantity": 1, "reserved": 1, "unreserved": 0}])
        progress = self.app.order_progress("G")
        self.assertEqual({line["sku"]: line["reserved"] for line in progress["lines"]},
                         {"T": 1})

    def test_later_operations_follow_existing_rules(self):
        self._managed_source()
        self.app.split_order("S", "G", [{"sku": "T", "quantity": 1}])
        # The new order tops up, amends and ships like any placed order.
        self.app.reserve_order("G")
        self.app.amend("G", [{"sku": "T", "quantity": 2}])
        # amend reprices against the catalog: T:2 at 100.
        self.assertEqual(self.app.get("G")["total_cents"], 200)
        self.app.ship("G", "DHL", "1")
        self.assertEqual(self.app.get("G")["status"], "shipped")
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 8, "reserved": 1, "available": 7})
        # The source still reduces, cancels and its shipped peer returns.
        self.app.reduce_order("S", [{"sku": "T", "quantity": 1}])
        self.app.record_return("G", "R1", [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.get_returns("G")["records"][0]["return_id"], "R1")
        self.app.cancel("S")
        self.assertEqual(self.app.get("S")["status"], "cancelled")

    def test_carts_and_other_orders_untouched(self):
        self._managed_source()
        self.app.save_cart("K", [{"sku": "T", "quantity": 1}])
        self.app.place("H", [{"sku": "C", "quantity": 1}])
        before_cart = self.app.get_cart("K")
        before_h = self.app.get("H")
        self.app.split_order("S", "G", [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.get_cart("K"), before_cart)
        self.assertEqual(self.app.get("H"), before_h)

    def test_persists_across_reopen(self):
        self._managed_source()
        result = self.app.split_order("S", "G", [{"sku": "T", "quantity": 1}])
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.get("S"), result["source"])
        self.assertEqual(reopened.get("G"), result["target"])
        for order_id in ("S", "G"):
            events = reopened.history(order_id)["events"]
            self.assertEqual(events[-1]["action"], "split-order")
            self.assertEqual(events[-1]["result"], result)
        self.assertEqual(reopened.stock("T")["reserved"], 2)
        self.assertEqual(reopened.stock("C")["reserved"], 1)

    def test_cli_success_failure_and_array_independence(self):
        self.app.restock("T", 10)
        self.app.place("X1", [{"sku": "T", "quantity": 2}])
        self.app.place("X2", [{"sku": "T", "quantity": 3}])
        payload = self.root / "split.json"
        payload.write_text(json.dumps({"source_id": " X1 ", "target_id": "G1",
                                       "lines": [{"sku": "T", "quantity": 1}]}),
                           encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                             "split-order", str(payload)],
                            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        value = json.loads(ok.stdout)
        self.assertEqual(set(value), {"source", "target"})
        self.assertEqual(value["source"]["order_id"], "X1")
        self.assertEqual(value["source"]["status"], "placed")
        self.assertEqual(value["target"]["order_id"], "G1")
        self.assertEqual(value["target"]["total_cents"], 100)
        payload.write_text(json.dumps([
            {"source_id": "X2", "target_id": "G2", "lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": "X2", "target_id": "G2", "lines": [{"sku": "T", "quantity": 1}]},
        ]), encoding="utf-8")
        run = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                              "split-order", str(payload)],
                             text=True, capture_output=True)
        self.assertEqual(run.returncode, 2, run.stdout)
        reopened = OrderDesk(self.root)
        # The first array item succeeded; the failed second item (target id
        # now occupied) did not roll it back or consume another sequence.
        self.assertEqual([e["action"] for e in reopened.history("X2")["events"]],
                         ["place", "split-order"])
        self.assertEqual([e["action"] for e in reopened.history("G2")["events"]],
                         ["split-order"])
        self.assertEqual(reopened.get("G2")["total_cents"], 100)
        self.assertEqual(reopened.stock("T")["reserved"], 5)

if __name__ == "__main__":
    unittest.main()
