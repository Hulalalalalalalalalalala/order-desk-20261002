import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk

class ReleaseReservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)
        self.app.add_product("U", "Unmanaged", 50)

    def _stocked_orders(self):
        # O holds 5 T and 3 C; P holds another 2 T.
        self.app.restock("T", 10)
        self.app.restock("C", 8)
        self.app.place("O", [{"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 3}])
        self.app.place("P", [{"sku": "T", "quantity": 2}])

    def test_releases_partial_quantity_and_returns_sorted_snapshot(self):
        self._stocked_orders()
        result = self.app.release_reservation(" O ", [
            {"sku": "C", "quantity": 1, "note": "ignored"},
            {"sku": "T", "quantity": 1},
            {"sku": "T", "quantity": 1},
        ])
        self.assertEqual(result, {
            "order_id": "O",
            "lines": [
                {"sku": "C", "quantity": 1, "reserved": 2,
                 "before": {"sku": "C", "on_hand": 8, "reserved": 3, "available": 5},
                 "after": {"sku": "C", "on_hand": 8, "reserved": 2, "available": 6}},
                {"sku": "T", "quantity": 2, "reserved": 3,
                 "before": {"sku": "T", "on_hand": 10, "reserved": 7, "available": 3},
                 "after": {"sku": "T", "on_hand": 10, "reserved": 5, "available": 5}},
            ],
        })
        for line in result["lines"]:
            self.assertEqual(set(line), {"sku", "quantity", "reserved", "before", "after"})
            self.assertEqual(set(line["before"]), {"sku", "on_hand", "reserved", "available"})
            self.assertEqual(set(line["after"]), {"sku", "on_hand", "reserved", "available"})

    def test_totals_availability_and_other_orders_are_untouched(self):
        self._stocked_orders()
        self.app.release_reservation("O", [{"sku": "T", "quantity": 2}])
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 10, "reserved": 5, "available": 5})
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(data["reservations"]["O"], {"T": 3, "C": 3})
        self.assertEqual(data["reservations"]["P"], {"T": 2})
        # Deal lines, prices, amount and status are preserved.
        order = self.app.get("O")
        self.assertEqual(order["status"], "placed")
        self.assertEqual(order["total_cents"], 1100)
        self.assertEqual(order["lines"][0],
                         {"sku": "T", "quantity": 5, "unit_price_cents": 100,
                          "subtotal_cents": 500})

    def test_full_release_keeps_the_order_and_drops_its_record(self):
        self._stocked_orders()
        result = self.app.release_reservation("O", [
            {"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 3},
        ])
        self.assertEqual([line["reserved"] for line in result["lines"]], [0, 0])
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("O", data["reservations"])
        self.assertEqual(self.app.get("O")["status"], "placed")
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 10, "reserved": 2, "available": 8})
        self.assertEqual(self.app.stock("C"),
                         {"sku": "C", "on_hand": 8, "reserved": 0, "available": 8})

    def test_repeat_submission_judged_on_current_balances(self):
        self._stocked_orders()
        self.app.release_reservation("O", [{"sku": "T", "quantity": 3}])
        # Only 2 remain: the identical 5-piece request and a 3-piece one fail.
        with self.assertRaises(ValueError):
            self.app.release_reservation("O", [{"sku": "T", "quantity": 5}])
        with self.assertRaises(ValueError):
            self.app.release_reservation("O", [{"sku": "T", "quantity": 3}])
        # Releasing the rest succeeds; a third submission then fails.
        again = self.app.release_reservation("O", [{"sku": "T", "quantity": 2}])
        self.assertEqual(again["lines"][0]["reserved"], 0)
        with self.assertRaises(ValueError):
            self.app.release_reservation("O", [{"sku": "T", "quantity": 1}])

    def test_merged_duplicate_skus_are_checked_together(self):
        self._stocked_orders()
        # Each line alone fits, but the merged 6 exceeds the held 5: the whole
        # request is rejected.
        with self.assertRaises(ValueError):
            self.app.release_reservation("O", [
                {"sku": "T", "quantity": 3}, {"sku": "T", "quantity": 3},
            ])
        self.assertEqual(self.app.stock("T")["reserved"], 7)

    def test_paused_sales_do_not_block_release(self):
        self._stocked_orders()
        self.app.set_product_enabled("T", False)
        result = self.app.release_reservation("O", [{"sku": "T", "quantity": 2}])
        self.assertEqual(result["lines"][0]["reserved"], 3)
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 10, "reserved": 5, "available": 5})
        self.assertFalse(self.app.get_product("T")["enabled"])

    def test_product_must_be_known_managed_and_in_the_order(self):
        # U is ordered while unmanaged and stays unmanaged.
        self.app.place("L", [{"sku": "U", "quantity": 4}])
        self._stocked_orders()
        with self.assertRaises(ValueError):
            self.app.release_reservation("L", [{"sku": "U", "quantity": 1}])
        # Managed and held, but not on this order.
        with self.assertRaises(ValueError):
            self.app.release_reservation("P", [{"sku": "C", "quantity": 1}])
        # Unknown catalog sku.
        with self.assertRaises(ValueError):
            self.app.release_reservation("O", [{"sku": "ZZ", "quantity": 1}])
        # Case sensitive: o is a different, unknown order.
        with self.assertRaises(ValueError):
            self.app.release_reservation("o", [{"sku": "T", "quantity": 1}])

    def test_legacy_order_without_reservation_record_treated_as_zero(self):
        order = {"status": "placed",
                 "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100,
                            "subtotal_cents": 200}],
                 "total_cents": 200}
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "inventory": {"T": {"on_hand": 7, "reserved": 2}},
            "orders": {"OLD": dict(order, order_id="OLD")},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        with self.assertRaises(ValueError):
            app.release_reservation("OLD", [{"sku": "T", "quantity": 1}])
        # Nothing moved: stock still shows the legacy reserved total.
        self.assertEqual(app.stock("T"),
                         {"sku": "T", "on_hand": 7, "reserved": 2, "available": 5})

    def test_input_validation(self):
        self._stocked_orders()
        bad_payloads = [
            {"order_id": "  ", "lines": [{"sku": "T", "quantity": 1}]},
            {"order_id": 1, "lines": [{"sku": "T", "quantity": 1}]},
            {"order_id": None, "lines": [{"sku": "T", "quantity": 1}]},
            {"order_id": "ghost", "lines": [{"sku": "T", "quantity": 1}]},
            {"order_id": "O", "lines": []},
            {"order_id": "O", "lines": "x"},
            {"order_id": "O", "lines": None},
            {"order_id": "O", "lines": {}},
            {"order_id": "O", "lines": [{"sku": "T"}]},
            {"order_id": "O", "lines": [{"quantity": 1}]},
            {"order_id": "O", "lines": ["x"]},
            {"order_id": "O", "lines": [{"sku": "T", "quantity": True}]},
            {"order_id": "O", "lines": [{"sku": "T", "quantity": False}]},
            {"order_id": "O", "lines": [{"sku": "T", "quantity": 0}]},
            {"order_id": "O", "lines": [{"sku": "T", "quantity": -2}]},
            {"order_id": "O", "lines": [{"sku": "T", "quantity": 1.5}]},
            {"order_id": "O", "lines": [{"sku": "T", "quantity": "1"}]},
            {"order_id": "O", "lines": [{"sku": "", "quantity": 1}]},
            {"order_id": "O", "lines": [{"sku": 3, "quantity": 1}]},
        ]
        for payload in bad_payloads:
            with self.assertRaises(ValueError, msg=payload):
                self.app.release_reservation(**payload)
        self.app.cancel("P")
        self.app.ship("O", "DHL", "1")
        with self.assertRaises(ValueError):
            self.app.release_reservation("P", [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.release_reservation("O", [{"sku": "T", "quantity": 1}])

    def test_rejected_release_writes_nothing_even_on_last_line(self):
        self._stocked_orders()
        raw = self.app.path.read_bytes()
        events_before = len(self.app.history("O")["events"])
        with self.assertRaises(ValueError):
            self.app.release_reservation("O", [
                {"sku": "T", "quantity": 1},
                {"sku": "C", "quantity": 9},
            ])
        self.assertEqual(self.app.path.read_bytes(), raw)
        self.assertEqual(len(self.app.history("O")["events"]), events_before)
        self.assertEqual([e["action"] for e in self.app.stock_history("T")["events"]],
                         ["restock", "place", "place"])
        self.assertEqual(self.app.stock("T")["reserved"], 7)
        self.assertEqual(self.app.stock("C")["reserved"], 3)

    def test_failure_creates_no_file_or_directory(self):
        empty = self.root / "empty"
        app = OrderDesk(empty)
        with self.assertRaises(ValueError):
            app.release_reservation("ghost", [{"sku": "T", "quantity": 1}])
        self.assertFalse(empty.exists())

    def test_order_and_stock_events_are_recorded_together(self):
        self._stocked_orders()
        result = self.app.release_reservation("O", [
            {"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1},
        ])
        history = self.app.history("O")
        self.assertTrue(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                         [(1, "place"), (2, "release-reservation")])
        self.assertEqual(set(history["events"][1]), {"sequence", "action", "result"})
        self.assertEqual(history["events"][1]["result"], result)
        events_t = self.app.stock_history("T")["events"]
        self.assertEqual([e["action"] for e in events_t],
                         ["restock", "place", "place", "release-reservation"])
        event = events_t[-1]
        self.assertEqual(set(event), {"sequence", "action", "reference_id", "before", "after"})
        self.assertEqual(event["sequence"], 4)
        self.assertEqual(event["reference_id"], "O")
        self.assertEqual(event["before"],
                         {"sku": "T", "on_hand": 10, "reserved": 7, "available": 3})
        self.assertEqual(event["after"],
                         {"sku": "T", "on_hand": 10, "reserved": 5, "available": 5})
        events_c = self.app.stock_history("C")["events"]
        self.assertEqual([e["action"] for e in events_c],
                         ["restock", "place", "release-reservation"])
        self.assertEqual(events_c[-1]["reference_id"], "O")
        # P holds T but is untouched: no event lands on its history.
        self.assertEqual([e["action"] for e in self.app.history("P")["events"]], ["place"])

    def test_legacy_order_starts_history_at_one_incomplete(self):
        order = {"status": "placed",
                 "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100,
                            "subtotal_cents": 200}],
                 "total_cents": 200}
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "inventory": {"T": {"on_hand": 7, "reserved": 2}},
            "orders": {"OLD": dict(order, order_id="OLD")},
            "reservations": {"OLD": {"T": 2}},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        result = app.release_reservation("OLD", [{"sku": "T", "quantity": 2}])
        history = app.history("OLD")
        self.assertFalse(history["complete"])
        self.assertEqual(history["events"], [
            {"sequence": 1, "action": "release-reservation", "result": result},
        ])
        stock_events = app.stock_history("T")["events"]
        self.assertFalse(app.stock_history("T")["complete"])
        self.assertEqual(stock_events[0]["sequence"], 1)
        self.assertEqual(stock_events[0]["action"], "release-reservation")
        self.assertEqual(app.stock("T"),
                         {"sku": "T", "on_hand": 7, "reserved": 0, "available": 7})

    def test_persists_across_reopen(self):
        self._stocked_orders()
        result = self.app.release_reservation("O", [{"sku": "T", "quantity": 2}])
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.get("O")["status"], "placed")
        events = reopened.history("O")["events"]
        self.assertEqual(events[-1]["action"], "release-reservation")
        self.assertEqual(events[-1]["result"], result)
        self.assertEqual(reopened.stock("T"),
                         {"sku": "T", "on_hand": 10, "reserved": 5, "available": 5})

    def test_pick_list_and_plan_reflect_new_balances(self):
        self._stocked_orders()
        self.app.release_reservation("O", [{"sku": "T", "quantity": 4}])
        pick = self.app.pick_list(["O", "P"])
        line = next(line for line in pick["lines"] if line["sku"] == "T")
        self.assertEqual(line["quantity"], 7)
        self.assertEqual(line["reserved"], 3)
        self.assertEqual(line["available"], 7)
        self.assertEqual(line["shortfall"], 0)
        self.assertEqual(line["orders"], [
            {"order_id": "O", "quantity": 5, "reserved": 1},
            {"order_id": "P", "quantity": 2, "reserved": 2},
        ])
        plan = {row["order_id"]: row for row in self.app.reservation_plan(["O", "P"])}
        self.assertTrue(plan["O"]["can_reserve"])
        t_line = next(line for line in plan["O"]["lines"] if line["sku"] == "T")
        self.assertEqual(t_line["reserved"], 1)
        self.assertEqual(t_line["shortfall"], 0)

    def test_reserve_amend_cancel_and_ship_follow_actual_reservations(self):
        self._stocked_orders()
        self.app.release_reservation("O", [
            {"sku": "T", "quantity": 4}, {"sku": "C", "quantity": 3},
        ])
        # Top-up adds back the released T using current availability.
        topped = self.app.reserve_order("O")
        t_line = next(line for line in topped["lines"] if line["sku"] == "T")
        self.assertEqual(t_line["added"], 4)
        self.assertEqual(t_line["reserved"], 5)
        # Release again, then amend down to 2 T: only the held amount stays
        # reserved and the 3 C are released because the line disappears.
        self.app.release_reservation("O", [{"sku": "T", "quantity": 3}])
        self.app.amend("O", [{"sku": "T", "quantity": 2}])
        self.assertEqual(self.app.stock("T")["reserved"], 4)
        self.assertEqual(self.app.stock("C")["reserved"], 0)
        # Cancel frees only what O still holds; P's 2 stay reserved.
        self.app.cancel("O")
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 10, "reserved": 2, "available": 8})
        # P ships only its own 2.
        self.app.ship("P", "DHL", "9")
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 8, "reserved": 0, "available": 8})

    def test_cli_success_failure_and_array_independence(self):
        self._stocked_orders()
        payload = self.root / "rl.json"
        payload.write_text(json.dumps(
            {"order_id": " O ", "lines": [{"sku": "T", "quantity": 2, "extra": 1}]}),
            encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                             "release-reservation", str(payload)],
                            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout), {
            "order_id": "O",
            "lines": [{
                "sku": "T", "quantity": 2, "reserved": 3,
                "before": {"sku": "T", "on_hand": 10, "reserved": 7, "available": 3},
                "after": {"sku": "T", "on_hand": 10, "reserved": 5, "available": 5},
            }],
        })
        # Bad request exits 2.
        bad = self.root / "bad.json"
        bad.write_text(json.dumps(
            {"order_id": "O", "lines": [{"sku": "T", "quantity": 9}]}),
            encoding="utf-8")
        run = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                              "release-reservation", str(bad)],
                             text=True, capture_output=True)
        self.assertEqual(run.returncode, 2, run.stdout)
        self.assertTrue(json.loads(run.stderr)["error"])
        # Outer array: the first item succeeds; the failed second item does not
        # roll it back or consume another sequence.
        payload.write_text(json.dumps([
            {"order_id": "O", "lines": [{"sku": "T", "quantity": 1}]},
            {"order_id": "O", "lines": [{"sku": "T", "quantity": 9}]},
        ]), encoding="utf-8")
        run = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                              "release-reservation", str(payload)],
                             text=True, capture_output=True)
        self.assertEqual(run.returncode, 2, run.stdout)
        reopened = OrderDesk(self.root)
        self.assertEqual([e["action"] for e in reopened.history("O")["events"]],
                         ["place", "release-reservation", "release-reservation"])
        self.assertEqual(reopened.stock("T")["reserved"], 4)

if __name__ == "__main__":
    unittest.main()
