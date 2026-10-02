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

    def _unmanaged_pair(self):
        # B and G are ordered while T and C are unmanaged, so they start with
        # no reservations; restocks happen later, then A is the first order
        # that actually holds stock: T reserved 3 (A), C reserved 2 (A).
        self.app.place("B", [{"sku": "T", "quantity": 7}, {"sku": "C", "quantity": 4}])
        self.app.place("G", [{"sku": "C", "quantity": 1}])
        self.app.restock("T", 5)
        self.app.restock("C", 5)
        self.app.place("A", [{"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 2}])

    def test_releases_and_returns_sorted_snapshot(self):
        self._unmanaged_pair()
        result = self.app.release_reservation(" A ", [
            {"sku": "T", "quantity": 2, "note": "ignored"},
            {"sku": "T", "quantity": 1},
            {"sku": "C", "quantity": 2},
        ])
        self.assertEqual(result["order_id"], "A")
        self.assertEqual(result["lines"], [
            {"sku": "C", "quantity": 2, "reserved": 0,
             "before": {"sku": "C", "on_hand": 5, "reserved": 2, "available": 3},
             "after": {"sku": "C", "on_hand": 5, "reserved": 0, "available": 5}},
            {"sku": "T", "quantity": 3, "reserved": 0,
             "before": {"sku": "T", "on_hand": 5, "reserved": 3, "available": 2},
             "after": {"sku": "T", "on_hand": 5, "reserved": 0, "available": 5}},
        ])
        self.assertEqual(set(result), {"order_id", "lines"})
        for line in result["lines"]:
            self.assertEqual(set(line), {"sku", "quantity", "reserved", "before", "after"})

    def test_reserved_and_available_move_together_on_hand_and_others_untouched(self):
        self._unmanaged_pair()
        self.app.release_reservation("A", [{"sku": "T", "quantity": 2}])
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 5, "reserved": 1, "available": 4})
        # C is untouched and on_hand never moves.
        self.assertEqual(self.app.stock("C"),
                         {"sku": "C", "on_hand": 5, "reserved": 2, "available": 3})
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(data["reservations"]["A"], {"T": 1, "C": 2})
        # Fully released sku keys and empty records disappear.
        self.app.release_reservation("A", [{"sku": "T", "quantity": 1},
                                           {"sku": "C", "quantity": 2}])
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("A", data["reservations"])
        self.assertEqual(data["reservations"], {})
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 5, "reserved": 0, "available": 5})
        self.assertEqual(self.app.stock("C"),
                         {"sku": "C", "on_hand": 5, "reserved": 0, "available": 5})
        # Deal lines, prices, amount and status are preserved.
        order = self.app.get("A")
        self.assertEqual(order["status"], "placed")
        self.assertEqual(order["total_cents"], 700)
        self.assertEqual(order["lines"], [
            {"sku": "T", "quantity": 3, "unit_price_cents": 100, "subtotal_cents": 300},
            {"sku": "C", "quantity": 2, "unit_price_cents": 200, "subtotal_cents": 400},
        ])

    def test_full_release_keeps_the_order(self):
        self._unmanaged_pair()
        self.app.release_reservation("A", [
            {"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 2},
        ])
        self.assertEqual(self.app.get("A")["status"], "placed")
        self.assertIn("A", [order["order_id"] for order in self.app.list_orders()])
        # Releasing again has no balance left to draw from.
        with self.assertRaises(ValueError):
            self.app.release_reservation("A", [{"sku": "T", "quantity": 1}])

    def test_repeat_submission_judged_on_current_balances(self):
        self._unmanaged_pair()
        first = self.app.release_reservation("A", [{"sku": "T", "quantity": 2}])
        self.assertEqual(first["lines"][0]["reserved"], 1)
        # The identical merged request now exceeds the remaining balance.
        with self.assertRaises(ValueError):
            self.app.release_reservation("A", [
                {"sku": "T", "quantity": 1}, {"sku": "T", "quantity": 1},
            ])
        # A request fitting the current balance still succeeds.
        second = self.app.release_reservation("A", [{"sku": "T", "quantity": 1}])
        self.assertEqual(second["lines"][0]["reserved"], 0)

    def test_paused_sales_do_not_block_release(self):
        self._unmanaged_pair()
        self.app.set_product_enabled("C", False)
        result = self.app.release_reservation("A", [{"sku": "C", "quantity": 2}])
        self.assertEqual(result["lines"][0]["reserved"], 0)
        self.assertEqual(self.app.stock("C"),
                         {"sku": "C", "on_hand": 5, "reserved": 0, "available": 5})

    def test_product_must_be_known_managed_and_in_order(self):
        self._unmanaged_pair()
        with self.assertRaises(ValueError):
            self.app.release_reservation("A", [{"sku": "ZZ", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.release_reservation("A", [{"sku": "U", "quantity": 1}])
        # G only carries C: T is known and managed but not on its lines.
        with self.assertRaises(ValueError):
            self.app.release_reservation("G", [{"sku": "T", "quantity": 1}])
        # B is placed and carries T, but holds no reservation (ordered before
        # the product was managed).
        with self.assertRaises(ValueError):
            self.app.release_reservation("B", [{"sku": "T", "quantity": 1}])
        # G also holds nothing on the C it did order.
        with self.assertRaises(ValueError):
            self.app.release_reservation("G", [{"sku": "C", "quantity": 1}])
        # Merged quantity is what is compared to the balance.
        with self.assertRaises(ValueError):
            self.app.release_reservation("A", [
                {"sku": "T", "quantity": 2}, {"sku": "T", "quantity": 2},
            ])

    def test_input_validation(self):
        self._unmanaged_pair()
        bad_payloads = [
            {"order_id": "  ", "lines": [{"sku": "T", "quantity": 1}]},
            {"order_id": 1, "lines": [{"sku": "T", "quantity": 1}]},
            {"order_id": None, "lines": [{"sku": "T", "quantity": 1}]},
            {"order_id": "nope", "lines": [{"sku": "T", "quantity": 1}]},
            {"order_id": "A", "lines": []},
            {"order_id": "A", "lines": "x"},
            {"order_id": "A", "lines": None},
            {"order_id": "A", "lines": [{"sku": "T"}]},
            {"order_id": "A", "lines": [{"quantity": 1}]},
            {"order_id": "A", "lines": ["x"]},
            {"order_id": "A", "lines": [{"sku": "T", "quantity": True}]},
            {"order_id": "A", "lines": [{"sku": "T", "quantity": 0}]},
            {"order_id": "A", "lines": [{"sku": "T", "quantity": -2}]},
            {"order_id": "A", "lines": [{"sku": "T", "quantity": 1.5}]},
            {"order_id": "A", "lines": [{"sku": "T", "quantity": "1"}]},
            {"order_id": "A", "lines": [{"sku": "", "quantity": 1}]},
        ]
        for payload in bad_payloads:
            with self.assertRaises(ValueError, msg=payload):
                self.app.release_reservation(**payload)
        self.app.cancel("G")
        self.app.ship("A", "DHL", "1")
        with self.assertRaises(ValueError):
            self.app.release_reservation("G", [{"sku": "C", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.release_reservation("A", [{"sku": "T", "quantity": 1}])
        # Ids are trimmed and case sensitive.
        with self.assertRaises(ValueError):
            self.app.release_reservation("b", [{"sku": "T", "quantity": 1}])

    def test_rejected_release_writes_nothing(self):
        self._unmanaged_pair()
        self.app.release_reservation("A", [{"sku": "C", "quantity": 2}])
        raw = self.app.path.read_bytes()
        events_before = len(self.app.history("A")["events"])
        stock_events_before = [e["action"] for e in self.app.stock_history("T")["events"]]
        with self.assertRaises(ValueError):
            # The over-large C line rejects the whole request before the valid
            # T line is applied: no partial release survives a failed item.
            self.app.release_reservation("A", [
                {"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 9},
            ])
        self.assertEqual(self.app.path.read_bytes(), raw)
        self.assertEqual(len(self.app.history("A")["events"]), events_before)
        self.assertEqual([e["action"] for e in self.app.stock_history("T")["events"]],
                         stock_events_before)
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 5, "reserved": 3, "available": 2})
        self.assertEqual(self.app.stock("C"),
                         {"sku": "C", "on_hand": 5, "reserved": 0, "available": 5})

    def test_failure_creates_no_file_or_directory(self):
        empty = self.root / "empty"
        app = OrderDesk(empty)
        with self.assertRaises(ValueError):
            app.release_reservation("ghost", [{"sku": "T", "quantity": 1}])
        self.assertFalse(empty.exists())

    def test_order_and_stock_events_with_full_snapshot(self):
        self._unmanaged_pair()
        result = self.app.release_reservation("A", [
            {"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 1},
        ])
        history = self.app.history("A")
        self.assertTrue(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                         [(1, "place"), (2, "release-reservation")])
        self.assertEqual(set(history["events"][1]), {"sequence", "action", "result"})
        self.assertEqual(history["events"][1]["result"], result)
        t_events = self.app.stock_history("T")["events"]
        self.assertEqual([e["action"] for e in t_events],
                         ["restock", "place", "release-reservation"])
        self.assertEqual(t_events[-1]["reference_id"], "A")
        self.assertEqual(t_events[-1]["before"],
                         {"sku": "T", "on_hand": 5, "reserved": 3, "available": 2})
        self.assertEqual(t_events[-1]["after"],
                         {"sku": "T", "on_hand": 5, "reserved": 0, "available": 5})
        c_events = self.app.stock_history("C")["events"]
        self.assertEqual([e["action"] for e in c_events],
                         ["restock", "place", "release-reservation"])
        self.assertEqual(c_events[-1]["reference_id"], "A")
        self.assertEqual(c_events[-1]["before"],
                         {"sku": "C", "on_hand": 5, "reserved": 2, "available": 3})
        self.assertEqual(c_events[-1]["after"],
                         {"sku": "C", "on_hand": 5, "reserved": 1, "available": 4})
        # Only A receives an order event; the bystander orders keep theirs.
        self.assertEqual([e["action"] for e in self.app.history("G")["events"]], ["place"])
        self.assertEqual([e["action"] for e in self.app.history("B")["events"]], ["place"])

    def test_legacy_orders_start_history_at_one_incomplete(self):
        order = {"status": "placed",
                 "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100,
                            "subtotal_cents": 200}],
                 "total_cents": 200}
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "inventory": {"T": {"on_hand": 7, "reserved": 2}},
            "orders": {"OLD": order},
            "reservations": {"OLD": {"T": 2}},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        result = app.release_reservation("OLD", [{"sku": "T", "quantity": 2}])
        self.assertEqual(result["lines"], [{
            "sku": "T", "quantity": 2, "reserved": 0,
            "before": {"sku": "T", "on_hand": 7, "reserved": 2, "available": 5},
            "after": {"sku": "T", "on_hand": 7, "reserved": 0, "available": 7},
        }])
        history = app.history("OLD")
        self.assertFalse(history["complete"])
        self.assertEqual(history["events"], [
            {"sequence": 1, "action": "release-reservation", "result": result},
        ])
        stock_history = app.stock_history("T")
        self.assertFalse(stock_history["complete"])
        self.assertEqual([e["sequence"] for e in stock_history["events"]], [1])
        self.assertEqual(app.stock("T"),
                         {"sku": "T", "on_hand": 7, "reserved": 0, "available": 7})

    def test_persists_across_reopen(self):
        self._unmanaged_pair()
        result = self.app.release_reservation("A", [
            {"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 2},
        ])
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.get("A")["status"], "placed")
        events = reopened.history("A")["events"]
        self.assertEqual(events[-1]["action"], "release-reservation")
        self.assertEqual(events[-1]["result"], result)
        self.assertEqual(reopened.stock("T"),
                         {"sku": "T", "on_hand": 5, "reserved": 1, "available": 4})
        self.assertEqual(reopened.stock("C"),
                         {"sku": "C", "on_hand": 5, "reserved": 0, "available": 5})
        self.assertEqual(json.loads(reopened.path.read_text(encoding="utf-8"))["reservations"],
                         {"A": {"T": 1}})

    def test_pick_list_and_plan_reflect_new_balance(self):
        self.app.place("B", [{"sku": "T", "quantity": 7}])
        self.app.restock("T", 5)
        self.app.place("A", [{"sku": "T", "quantity": 3}])
        self.app.release_reservation("A", [{"sku": "T", "quantity": 2}])
        pick = self.app.pick_list(["A", "B"])
        line = pick["lines"][0]
        self.assertEqual(line["quantity"], 10)
        self.assertEqual(line["reserved"], 1)
        self.assertEqual(line["available"], 4)
        self.assertEqual(line["shortfall"], 5)
        self.assertEqual(line["orders"], [
            {"order_id": "A", "quantity": 3, "reserved": 1},
            {"order_id": "B", "quantity": 7, "reserved": 0},
        ])
        plan = {row["order_id"]: row for row in self.app.reservation_plan(["B", "A"])}
        # B needs 7 against only 4 freed units, so it cannot complete and
        # consumes nothing; A's remaining demand of 2 then fits.
        self.assertFalse(plan["B"]["can_reserve"])
        self.assertTrue(plan["A"]["can_reserve"])
        self.assertEqual([l["reserved"] for l in plan["B"]["lines"]], [0])
        self.assertEqual([l["shortfall"] for l in plan["B"]["lines"]], [3])

    def test_later_topup_amend_cancel_and_ship_follow_actual_reservations(self):
        self.app.place("B", [{"sku": "T", "quantity": 4}, {"sku": "C", "quantity": 2}])
        self.app.place("G", [{"sku": "C", "quantity": 1}])
        self.app.restock("T", 5)
        self.app.restock("C", 5)
        self.app.place("A", [{"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 2}])
        # A gives two T units back; nothing else moves yet.
        self.app.release_reservation("A", [{"sku": "T", "quantity": 2}])
        # Freed availability is real stock: B, ordered before the products
        # were managed, can now top up to exactly its demand.
        topped = self.app.reserve_order("B")
        self.assertEqual([(l["sku"], l["added"], l["reserved"]) for l in topped["lines"]],
                         [("C", 2, 2), ("T", 4, 4)])
        # Amend keeps working off actual reservations: shrinking B's T line by
        # one releases exactly that unit.
        self.app.amend("B", [{"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 2}])
        self.assertEqual(self.app.get("B")["total_cents"], 700)
        # A ships only the T 1 and C 2 it still holds.
        self.app.ship("A", "DHL", "1")
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 4, "reserved": 3, "available": 1})
        self.assertEqual(self.app.stock("C"),
                         {"sku": "C", "on_hand": 3, "reserved": 2, "available": 1})
        # An order that takes its one unit, releases it fully and is then
        # cancelled changes no stock on the cancel.
        self.app.place("X", [{"sku": "T", "quantity": 1}])
        self.app.release_reservation("X", [{"sku": "T", "quantity": 1}])
        before = self.app.stock("T")
        self.app.cancel("X")
        self.assertEqual(self.app.stock("T"), before)

    def test_batch_ship_follows_released_balances(self):
        self._unmanaged_pair()
        # Give G a real unit, then release everything A still holds.
        self.app.transfer_reservation("A", "G", [{"sku": "C", "quantity": 1}])
        self.app.release_reservation("A", [
            {"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 1},
        ])
        orders = self.app.ship_batch([
            {"order_id": "A", "carrier": "DHL", "tracking_no": "1"},
            {"order_id": "G", "carrier": "UPS", "tracking_no": "2"},
        ])
        self.assertEqual([order["order_id"] for order in orders], ["A", "G"])
        # A held nothing when shipped: T is completely untouched; G's single C
        # unit is the only stock deducted.
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 5, "reserved": 0, "available": 5})
        self.assertEqual(self.app.stock("C"),
                         {"sku": "C", "on_hand": 4, "reserved": 0, "available": 4})

    def test_cli_success_failure_and_array_independence(self):
        self.app.place("X2", [{"sku": "T", "quantity": 5}])
        self.app.restock("T", 5)
        self.app.place("X1", [{"sku": "T", "quantity": 2}])
        payload = self.root / "rr.json"
        payload.write_text(json.dumps(
            {"order_id": " X1 ", "lines": [{"sku": "T", "quantity": 1}]}),
            encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                             "release-reservation", str(payload)],
                            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout), {
            "order_id": "X1",
            "lines": [{"sku": "T", "quantity": 1, "reserved": 1,
                       "before": {"sku": "T", "on_hand": 5, "reserved": 2, "available": 3},
                       "after": {"sku": "T", "on_hand": 5, "reserved": 1, "available": 4}}],
        })
        payload.write_text(json.dumps([
            {"order_id": "X1", "lines": [{"sku": "T", "quantity": 1}]},
            {"order_id": "X1", "lines": [{"sku": "T", "quantity": 9}]},
        ]), encoding="utf-8")
        run = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                              "release-reservation", str(payload)],
                             text=True, capture_output=True)
        self.assertEqual(run.returncode, 2, run.stdout)
        reopened = OrderDesk(self.root)
        # The first array item succeeded; the failed second item did not roll
        # it back or consume another sequence.
        self.assertEqual([e["action"] for e in reopened.history("X1")["events"]],
                         ["place", "release-reservation", "release-reservation"])
        self.assertEqual(reopened.stock("T")["reserved"], 0)
        self.assertEqual(reopened.stock("T")["available"], 5)

if __name__ == "__main__":
    unittest.main()
