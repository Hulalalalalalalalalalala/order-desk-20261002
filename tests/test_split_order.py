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
        # S holds T:4 and C:2 with full reservations; every product managed.
        self.app.restock("T", 10)
        self.app.restock("C", 10)
        self.app.place("S", [{"sku": "T", "quantity": 4}, {"sku": "C", "quantity": 2}])

    def _write_source(self, source_lines, extra=None):
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "orders": {
                "S": {"order_id": "S", "status": "placed", "lines": source_lines,
                      "total_cents": sum(l.get("subtotal_cents", 0) for l in source_lines)},
            },
        }
        if extra:
            data.update(extra)
        self.app.path.write_text(json.dumps(data), encoding="utf-8")

    def test_splits_lines_amounts_and_status(self):
        self._managed_source()
        result = self.app.split_order(" S ", " N ", [{"sku": "T", "quantity": 1}])
        self.assertEqual(set(result), {"source", "target"})
        source = result["source"]
        self.assertEqual(source["order_id"], "S")
        self.assertEqual(source["status"], "placed")
        self.assertEqual(source["lines"], [
            {"sku": "T", "quantity": 3, "unit_price_cents": 100, "subtotal_cents": 300},
            {"sku": "C", "quantity": 2, "unit_price_cents": 200, "subtotal_cents": 400},
        ])
        self.assertEqual(source["total_cents"], 700)
        target = result["target"]
        self.assertEqual(target["order_id"], "N")
        self.assertEqual(target["status"], "placed")
        self.assertEqual(target["lines"], [
            {"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100},
        ])
        self.assertEqual(target["total_cents"], 100)
        # The two totals sum to the original total.
        self.assertEqual(source["total_cents"] + target["total_cents"], 800)
        # Both entries follow the full get structure.
        self.assertEqual(result["source"], self.app.get("S"))
        self.assertEqual(result["target"], self.app.get("N"))

    def test_takes_from_last_row_backwards(self):
        self.app.place("S", [{"sku": "T", "quantity": 1},
                             {"sku": "C", "quantity": 1},
                             {"sku": "T", "quantity": 3}])
        result = self.app.split_order("S", "N", [{"sku": "T", "quantity": 2}])
        # The moved quantity comes off the last T row; both sides keep the
        # original relative row order and duplicates stay split.
        self.assertEqual([(l["sku"], l["quantity"]) for l in result["source"]["lines"]],
                         [("T", 1), ("C", 1), ("T", 1)])
        self.assertEqual([(l["sku"], l["quantity"]) for l in result["target"]["lines"]],
                         [("T", 2)])
        self.assertEqual(result["source"]["total_cents"], 400)
        self.assertEqual(result["target"]["total_cents"], 200)

    def test_duplicate_request_lines_merge(self):
        self.app.place("S", [{"sku": "T", "quantity": 3}])
        result = self.app.split_order("S", "N", [{"sku": " T ", "quantity": 1, "note": "x"},
                                                 {"sku": "T", "quantity": 1}])
        self.assertEqual(result["source"]["lines"][0]["quantity"], 1)
        self.assertEqual(result["target"]["lines"],
                         [{"sku": "T", "quantity": 2, "unit_price_cents": 100,
                           "subtotal_cents": 200}])

    def test_zero_price_rows_are_kept(self):
        self.app.add_product("Z", "Free", 0)
        self.app.place("S", [{"sku": "Z", "quantity": 2}, {"sku": "T", "quantity": 1}])
        result = self.app.split_order("S", "N", [{"sku": "Z", "quantity": 1}])
        self.assertEqual(result["target"]["lines"],
                         [{"sku": "Z", "quantity": 1, "unit_price_cents": 0,
                           "subtotal_cents": 0}])
        self.assertEqual(result["target"]["total_cents"], 0)
        self.assertEqual(result["source"]["total_cents"], 100)

    def test_input_validation(self):
        self._managed_source()
        bad_payloads = [
            {"source_id": "  ", "target_id": "N", "lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": 1, "target_id": "N", "lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": None, "target_id": "N", "lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": "S", "target_id": "", "lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": "S", "target_id": "  ", "lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": "S", "target_id": 2, "lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": "S", "target_id": "S", "lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": " S ", "target_id": "S", "lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": "nope", "target_id": "N", "lines": [{"sku": "T", "quantity": 1}]},
            # Ids are trimmed and case sensitive.
            {"source_id": "s", "target_id": "N", "lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": "S", "target_id": "N", "lines": []},
            {"source_id": "S", "target_id": "N", "lines": "T"},
            {"source_id": "S", "target_id": "N", "lines": ["T"]},
            {"source_id": "S", "target_id": "N", "lines": [{"sku": "T"}]},
            {"source_id": "S", "target_id": "N", "lines": [{"quantity": 1}]},
            {"source_id": "S", "target_id": "N", "lines": [{"sku": " ", "quantity": 1}]},
            {"source_id": "S", "target_id": "N", "lines": [{"sku": "T", "quantity": 0}]},
            {"source_id": "S", "target_id": "N", "lines": [{"sku": "T", "quantity": -1}]},
            {"source_id": "S", "target_id": "N", "lines": [{"sku": "T", "quantity": 1.5}]},
            {"source_id": "S", "target_id": "N", "lines": [{"sku": "T", "quantity": True}]},
            {"source_id": "S", "target_id": "N", "lines": [{"sku": "T", "quantity": "1"}]},
            # Sku matching is case sensitive.
            {"source_id": "S", "target_id": "N", "lines": [{"sku": "t", "quantity": 1}]},
            {"source_id": "S", "target_id": "N", "lines": [{"sku": "X", "quantity": 1}]},
            # Merged quantity exceeds the ordered quantity.
            {"source_id": "S", "target_id": "N", "lines": [{"sku": "T", "quantity": 5}]},
            {"source_id": "S", "target_id": "N",
             "lines": [{"sku": "T", "quantity": 3}, {"sku": "T", "quantity": 2}]},
            # Moving the whole order away is rejected.
            {"source_id": "S", "target_id": "N",
             "lines": [{"sku": "T", "quantity": 4}, {"sku": "C", "quantity": 2}]},
        ]
        for payload in bad_payloads:
            with self.assertRaises(ValueError, msg=payload):
                self.app.split_order(**payload)
        # An occupied target id is rejected, even of a cancelled order.
        self.app.place("G", [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.split_order("S", "G", [{"sku": "T", "quantity": 1}])
        self.app.cancel("G")
        with self.assertRaises(ValueError):
            self.app.split_order("S", "G", [{"sku": "T", "quantity": 1}])
        # A non-placed source cannot be split.
        self.app.cancel("S")
        with self.assertRaises(ValueError):
            self.app.split_order("S", "N", [{"sku": "T", "quantity": 1}])

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
                self.app.split_order("S", "N", [{"sku": "T", "quantity": 1}])
        # Inconsistent deal prices inside the source's own rows reject too.
        self._write_source([
            {"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100},
            {"sku": "T", "quantity": 2, "unit_price_cents": 150, "subtotal_cents": 300},
        ])
        raw = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.split_order("S", "N", [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.path.read_bytes(), raw)

    def test_paused_repriced_catalog_missing_and_short_stock_do_not_block(self):
        self.app.place("S", [{"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 1}])
        self.app.set_product_enabled("T", False)
        self.app.reprice_products([{"sku": "C", "expected_price_cents": 200, "price_cents": 250}])
        result = self.app.split_order("S", "N", [{"sku": "T", "quantity": 1},
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
        moved = self.app.split_order("S", "M", [{"sku": "T", "quantity": 1}])
        self.assertEqual(moved["target"]["status"], "placed")
        self.assertEqual(moved["target"]["lines"][0]["unit_price_cents"], 100)
        # Insufficient available stock never blocks a split.
        self.app.restock("C", 1)
        self.app.place("J", [{"sku": "C", "quantity": 1}, {"sku": "U", "quantity": 1}])
        low = self.app.split_order("J", "K", [{"sku": "C", "quantity": 1}])
        self.assertEqual(low["source"]["lines"], [
            {"sku": "U", "quantity": 1, "unit_price_cents": 50, "subtotal_cents": 50},
        ])
        self.assertEqual(low["target"]["lines"][0]["quantity"], 1)

    def test_reservations_split_without_touching_stock(self):
        self._managed_source()
        self.app.place("H", [{"sku": "T", "quantity": 1}])
        before_t = self.app.stock("T")
        before_c = self.app.stock("C")
        self.app.split_order("S", "N", [{"sku": "T", "quantity": 3},
                                        {"sku": "C", "quantity": 1}])
        # Totals, availability and other orders are untouched.
        self.assertEqual(self.app.stock("T"), before_t)
        self.assertEqual(self.app.stock("C"), before_c)
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        # The source keeps min(old reservation, remaining ordered quantity);
        # the difference moves to the new order.
        self.assertEqual(data["reservations"]["S"], {"T": 1, "C": 1})
        self.assertEqual(data["reservations"]["N"], {"T": 3, "C": 1})
        self.assertEqual(data["reservations"]["H"], {"T": 1})
        # Ownership changed only: no stock history is recorded.
        self.assertEqual([e["action"] for e in self.app.stock_history("T")["events"]],
                         ["restock", "place", "place"])
        self.assertEqual([e["action"] for e in self.app.stock_history("C")["events"]],
                         ["restock", "place"])

    def test_reservation_shortfall_stays_with_source(self):
        # A partially reserved source keeps its whole actual reservation when
        # the remaining quantity still covers it; the target gets nothing.
        self.app.restock("T", 10)
        self.app.place("S", [{"sku": "T", "quantity": 4}])
        self.app.release_reservation("S", [{"sku": "T", "quantity": 3}])
        result = self.app.split_order("S", "N", [{"sku": "T", "quantity": 2}])
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(data["reservations"].get("S"), {"T": 1})
        self.assertNotIn("N", data["reservations"])
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 10, "reserved": 1, "available": 9})
        self.assertEqual(result["target"]["lines"][0]["quantity"], 2)

    def test_zero_reservation_attribution_dropped(self):
        self.app.restock("T", 10)
        self.app.place("S", [{"sku": "T", "quantity": 2}, {"sku": "U", "quantity": 1}])
        self.app.split_order("S", "N", [{"sku": "T", "quantity": 2}])
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        # The source's whole T reservation moved and it holds nothing else
        # managed; its attribution key is gone.
        self.assertNotIn("S", data["reservations"])
        self.assertEqual(data["reservations"]["N"], {"T": 2})
        self.assertEqual(self.app.get("S")["lines"], [
            {"sku": "U", "quantity": 1, "unit_price_cents": 50, "subtotal_cents": 50},
        ])

    def test_unmanaged_products_not_auto_managed(self):
        self.app.place("S", [{"sku": "U", "quantity": 3}])
        self.app.split_order("S", "N", [{"sku": "U", "quantity": 1}])
        self.assertEqual(self.app.stock("U"),
                         {"sku": "U", "on_hand": None, "reserved": 0, "available": None})
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("inventory", data)
        self.assertNotIn("reservations", data)

    def test_missing_reservation_record_reads_as_none(self):
        self._write_source(
            [{"sku": "T", "quantity": 3, "unit_price_cents": 100, "subtotal_cents": 300}],
            extra={"inventory": {"T": {"on_hand": 7, "reserved": 2}}},
        )
        self.app.split_order("S", "N", [{"sku": "T", "quantity": 1}])
        saved = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("reservations", saved)
        self.assertEqual(saved["inventory"]["T"], {"on_hand": 7, "reserved": 2})

    def test_target_id_may_match_cart_id(self):
        self.app.place("S", [{"sku": "T", "quantity": 2}])
        self.app.save_cart("K", [{"sku": "T", "quantity": 1}])
        result = self.app.split_order("S", "K", [{"sku": "T", "quantity": 1}])
        self.assertEqual(result["target"]["order_id"], "K")
        # The cart itself is untouched.
        self.assertEqual(self.app.get_cart("K"),
                         {"cart_id": "K", "lines": [{"sku": "T", "quantity": 1}]})

    def test_rejected_split_writes_nothing(self):
        self._managed_source()
        raw = self.app.path.read_bytes()
        events_before = len(self.app.history("S")["events"])
        for payload in (
            {"source_id": "S", "target_id": "S", "lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": "nope", "target_id": "N", "lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": "S", "target_id": "N", "lines": [{"sku": "X", "quantity": 1}]},
            {"source_id": "S", "target_id": "N", "lines": [{"sku": "T", "quantity": 9}]},
            {"source_id": "S", "target_id": "N",
             "lines": [{"sku": "T", "quantity": 4}, {"sku": "C", "quantity": 2}]},
        ):
            with self.assertRaises(ValueError, msg=payload):
                self.app.split_order(**payload)
        self.assertEqual(self.app.path.read_bytes(), raw)
        self.assertEqual(len(self.app.history("S")["events"]), events_before)

    def test_failure_creates_no_file_or_directory(self):
        empty = self.root / "empty"
        app = OrderDesk(empty)
        with self.assertRaises(ValueError):
            app.split_order("ghost-1", "ghost-2", [{"sku": "T", "quantity": 1}])
        self.assertFalse(empty.exists())

    def test_both_orders_get_events_with_full_snapshot(self):
        self._managed_source()
        result = self.app.split_order("S", "N", [{"sku": "T", "quantity": 1}])
        source_history = self.app.history("S")
        self.assertTrue(source_history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in source_history["events"]],
                         [(1, "place"), (2, "split-order")])
        self.assertEqual(set(source_history["events"][1]), {"sequence", "action", "result"})
        self.assertEqual(source_history["events"][1]["result"], result)
        target_history = self.app.history("N")
        self.assertTrue(target_history["complete"])
        self.assertEqual(target_history["events"],
                         [{"sequence": 1, "action": "split-order", "result": result}])
        self.assertEqual(self.app.history("S")["status"], "placed")
        self.assertEqual(self.app.history("N")["status"], "placed")

    def test_legacy_source_starts_history_at_one_incomplete(self):
        self._write_source(
            [{"sku": "T", "quantity": 3, "unit_price_cents": 100, "subtotal_cents": 300}],
            extra={"inventory": {"T": {"on_hand": 7, "reserved": 3}},
                   "reservations": {"S": {"T": 3}}},
        )
        result = self.app.split_order("S", "N", [{"sku": "T", "quantity": 1}])
        source_history = self.app.history("S")
        self.assertFalse(source_history["complete"])
        self.assertEqual(source_history["events"],
                         [{"sequence": 1, "action": "split-order", "result": result}])
        target_history = self.app.history("N")
        self.assertTrue(target_history["complete"])
        self.assertEqual(target_history["events"],
                         [{"sequence": 1, "action": "split-order", "result": result}])
        saved = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["reservations"]["S"], {"T": 2})
        self.assertEqual(saved["reservations"]["N"], {"T": 1})
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 7, "reserved": 3, "available": 4})

    def test_repeat_target_rejected(self):
        self._managed_source()
        self.app.split_order("S", "N", [{"sku": "T", "quantity": 1}])
        # The target id is occupied now, so the identical request is rejected.
        with self.assertRaises(ValueError):
            self.app.split_order("S", "N", [{"sku": "T", "quantity": 1}])

    def test_queries_reflect_split_content(self):
        self._managed_source()
        self.app.split_order("S", "N", [{"sku": "T", "quantity": 3},
                                        {"sku": "C", "quantity": 1}])
        pick = self.app.pick_list(["S", "N"])
        self.assertEqual(pick["lines"], [
            {"sku": "C", "quantity": 2, "reserved": 2, "available": 8, "shortfall": 0,
             "orders": [{"order_id": "N", "quantity": 1, "reserved": 1},
                        {"order_id": "S", "quantity": 1, "reserved": 1}]},
            {"sku": "T", "quantity": 4, "reserved": 4, "available": 6, "shortfall": 0,
             "orders": [{"order_id": "N", "quantity": 3, "reserved": 3},
                        {"order_id": "S", "quantity": 1, "reserved": 1}]},
        ])
        audit = self.app.reservation_audit("T")
        self.assertEqual(audit["allocated"], 4)
        self.assertEqual(audit["difference"], 0)
        progress = self.app.order_progress("N")
        self.assertEqual({line["sku"]: line["reserved"] for line in progress["lines"]},
                         {"C": 1, "T": 3})

    def test_later_operations_follow_existing_rules(self):
        self._managed_source()
        self.app.split_order("S", "N", [{"sku": "T", "quantity": 3},
                                        {"sku": "C", "quantity": 1}])
        # The new order ships only what it actually holds.
        self.app.ship("N", "DHL", "1")
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 7, "reserved": 1, "available": 6})
        self.assertEqual(self.app.stock("C"),
                         {"sku": "C", "on_hand": 9, "reserved": 1, "available": 8})
        # The source can still be amended, reduced, topped up and cancelled.
        self.app.reduce_order("S", [{"sku": "C", "quantity": 1}])
        self.assertEqual(self.app.get("S")["lines"], [
            {"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100},
        ])
        self.app.cancel("S")
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 7, "reserved": 0, "available": 7})
        # The shipped target can be delivered and accept returns.
        self.app.confirm_delivery("N", "R", "2026-10-04")
        record = self.app.record_return("N", "RT1", [{"sku": "T", "quantity": 1}])
        self.assertEqual(record["lines"], [{"sku": "T", "quantity": 1}])

    def test_carts_and_other_orders_untouched(self):
        self._managed_source()
        self.app.save_cart("K", [{"sku": "T", "quantity": 1}])
        self.app.place("H", [{"sku": "C", "quantity": 1}])
        before_cart = self.app.get_cart("K")
        before_h = self.app.get("H")
        self.app.split_order("S", "N", [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.get_cart("K"), before_cart)
        self.assertEqual(self.app.get("H"), before_h)

    def test_persists_across_reopen(self):
        self._managed_source()
        result = self.app.split_order("S", "N", [{"sku": "T", "quantity": 1}])
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.get("S"), result["source"])
        self.assertEqual(reopened.get("N"), result["target"])
        for order_id in ("S", "N"):
            events = reopened.history(order_id)["events"]
            self.assertEqual(events[-1]["action"], "split-order")
            self.assertEqual(events[-1]["result"], result)
        self.assertEqual(reopened.stock("T")["reserved"], 4)
        self.assertEqual(reopened.stock("C")["reserved"], 2)

    def test_cli_success_failure_and_array_independence(self):
        self.app.restock("T", 10)
        self.app.place("X1", [{"sku": "T", "quantity": 4}])
        payload = self.root / "split.json"
        payload.write_text(json.dumps({"source_id": " X1 ", "target_id": "X2",
                                       "lines": [{"sku": "T", "quantity": 1}]}),
                           encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                             "split-order", str(payload)],
                            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        value = json.loads(ok.stdout)
        self.assertEqual(set(value), {"source", "target"})
        self.assertEqual(value["source"]["order_id"], "X1")
        self.assertEqual(value["source"]["total_cents"], 300)
        self.assertEqual(value["target"]["order_id"], "X2")
        self.assertEqual(value["target"]["total_cents"], 100)
        payload.write_text(json.dumps([
            {"source_id": "X1", "target_id": "X3", "lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": "X1", "target_id": "X3", "lines": [{"sku": "T", "quantity": 1}]},
        ]), encoding="utf-8")
        run = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                              "split-order", str(payload)],
                             text=True, capture_output=True)
        self.assertEqual(run.returncode, 2, run.stdout)
        reopened = OrderDesk(self.root)
        # The first array item succeeded; the failed second item (target id
        # now occupied) did not roll it back or consume another sequence.
        self.assertEqual([e["action"] for e in reopened.history("X1")["events"]],
                         ["place", "split-order", "split-order"])
        self.assertEqual([e["action"] for e in reopened.history("X3")["events"]],
                         ["split-order"])
        self.assertEqual(reopened.get("X1")["total_cents"], 200)
        self.assertEqual(reopened.stock("T")["reserved"], 4)

if __name__ == "__main__":
    unittest.main()
