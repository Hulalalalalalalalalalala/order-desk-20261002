import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk

class TransferReservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)
        self.app.add_product("U", "Unmanaged", 50)

    def _unmanaged_pair(self):
        # B is ordered while T and C are unmanaged, so it starts with no
        # reservations; A later takes the only reservations in stock.
        self.app.place("B", [{"sku": "T", "quantity": 7}, {"sku": "C", "quantity": 4}])
        self.app.place("G", [{"sku": "C", "quantity": 1}])
        self.app.restock("T", 5)
        self.app.restock("C", 5)
        self.app.place("A", [{"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 2}])

    def test_moves_reservation_and_returns_sorted_snapshot(self):
        self._unmanaged_pair()
        result = self.app.transfer_reservation(" A ", "B", [
            {"sku": "T", "quantity": 2, "note": "ignored"},
            {"sku": "T", "quantity": 1},
            {"sku": "C", "quantity": 2},
        ])
        self.assertEqual(result, {
            "source_id": "A",
            "target_id": "B",
            "lines": [
                {"sku": "C", "quantity": 2, "source_reserved": 0, "target_reserved": 2},
                {"sku": "T", "quantity": 3, "source_reserved": 0, "target_reserved": 3},
            ],
        })
        for line in result["lines"]:
            self.assertEqual(set(line), {"sku", "quantity", "source_reserved", "target_reserved"})

    def test_totals_availability_and_other_orders_are_untouched(self):
        self._unmanaged_pair()
        before_t = self.app.stock("T")
        before_c = self.app.stock("C")
        self.app.transfer_reservation("A", "B", [
            {"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 2},
        ])
        self.assertEqual(self.app.stock("T"), before_t)
        self.assertEqual(self.app.stock("C"), before_c)
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("A", data["reservations"])
        self.assertEqual(data["reservations"]["B"], {"T": 3, "C": 2})
        # A's deal lines, prices, amount and both statuses are preserved.
        order_a = self.app.get("A")
        self.assertEqual(order_a["status"], "placed")
        self.assertEqual(order_a["total_cents"], 700)
        self.assertEqual(self.app.get("B")["lines"][0],
                         {"sku": "T", "quantity": 7, "unit_price_cents": 100, "subtotal_cents": 700})

    def test_repeat_submission_judged_on_current_balances(self):
        self._unmanaged_pair()
        self.app.transfer_reservation("A", "B", [{"sku": "T", "quantity": 3}])
        # A now holds nothing: the identical request is rejected.
        with self.assertRaises(ValueError):
            self.app.transfer_reservation("A", "B", [{"sku": "T", "quantity": 3}])
        # Moving one back succeeds and reports both new balances.
        back = self.app.transfer_reservation("B", "A", [{"sku": "T", "quantity": 1}])
        self.assertEqual(back["lines"], [
            {"sku": "T", "quantity": 1, "source_reserved": 2, "target_reserved": 1},
        ])
        # B cannot give more than it holds.
        with self.assertRaises(ValueError):
            self.app.transfer_reservation("B", "A", [{"sku": "T", "quantity": 3}])

    def test_target_cannot_exceed_its_merged_ordered_quantity(self):
        self.app.restock("T", 5)
        self.app.place("S", [{"sku": "T", "quantity": 3}])
        self.app.place("G", [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.transfer_reservation("S", "G", [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.transfer_reservation("G", "S", [{"sku": "T", "quantity": 2}])

    def test_paused_sales_do_not_block_transfer(self):
        self._unmanaged_pair()
        self.app.transfer_reservation("A", "B", [{"sku": "C", "quantity": 2}])
        self.app.set_product_enabled("C", False)
        before = self.app.stock("C")
        result = self.app.transfer_reservation("B", "G", [{"sku": "C", "quantity": 1}])
        self.assertEqual(result["lines"][0],
                         {"sku": "C", "quantity": 1, "source_reserved": 1, "target_reserved": 1})
        self.assertEqual(self.app.stock("C"), before)

    def test_product_must_be_managed_and_in_both_orders(self):
        self._unmanaged_pair()
        # G holds a real C reservation but has no T line.
        with self.assertRaises(ValueError):
            self.app.transfer_reservation("A", "G", [{"sku": "T", "quantity": 1}])
        # U exists but is unmanaged and only B-side orders carry it.
        self.app.place("U1", [{"sku": "U", "quantity": 2}])
        self.app.place("U2", [{"sku": "U", "quantity": 1}, {"sku": "C", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.transfer_reservation("U2", "U1", [{"sku": "U", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.transfer_reservation("A", "U2", [{"sku": "ZZ", "quantity": 1}])

    def test_input_validation(self):
        self._unmanaged_pair()
        bad_payloads = [
            {"source_id": "  ", "target_id": "B", "lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": 1, "target_id": "B", "lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": None, "target_id": "B", "lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": "B", "target_id": "B", "lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": "nope", "target_id": "B", "lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": "B", "target_id": "nope", "lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": "B", "target_id": "A", "lines": []},
            {"source_id": "B", "target_id": "A", "lines": "x"},
            {"source_id": "B", "target_id": "A", "lines": None},
            {"source_id": "B", "target_id": "A", "lines": [{"sku": "T"}]},
            {"source_id": "B", "target_id": "A", "lines": [{"sku": "T", "quantity": True}]},
            {"source_id": "B", "target_id": "A", "lines": [{"sku": "T", "quantity": 0}]},
            {"source_id": "B", "target_id": "A", "lines": [{"sku": "T", "quantity": -2}]},
            {"source_id": "B", "target_id": "A", "lines": [{"sku": "T", "quantity": 1.5}]},
            {"source_id": "B", "target_id": "A", "lines": [{"sku": "T", "quantity": "1"}]},
            {"source_id": "B", "target_id": "A", "lines": [{"sku": "", "quantity": 1}]},
            {"source_id": "B", "target_id": "A", "lines": [{"quantity": 1}]},
            {"source_id": "B", "target_id": "A", "lines": ["x"]},
        ]
        for payload in bad_payloads:
            with self.assertRaises(ValueError, msg=payload):
                self.app.transfer_reservation(**payload)
        self.app.cancel("G")
        self.app.ship("A", "DHL", "1")
        with self.assertRaises(ValueError):
            self.app.transfer_reservation("G", "B", [{"sku": "C", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.transfer_reservation("A", "B", [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.transfer_reservation("B", "A", [{"sku": "T", "quantity": 1}])
        # Ids are trimmed and case sensitive.
        with self.assertRaises(ValueError):
            self.app.transfer_reservation("b", "G", [{"sku": "C", "quantity": 1}])

    def test_rejected_transfer_writes_nothing(self):
        self._unmanaged_pair()
        self.app.transfer_reservation("A", "B", [{"sku": "C", "quantity": 2}])
        raw = self.app.path.read_bytes()
        events_before = len(self.app.history("B")["events"])
        with self.assertRaises(ValueError):
            self.app.transfer_reservation("B", "A", [{"sku": "C", "quantity": 9}])
        self.assertEqual(self.app.path.read_bytes(), raw)
        self.assertEqual(len(self.app.history("B")["events"]), events_before)
        self.assertEqual([e["action"] for e in self.app.stock_history("C")["events"]],
                         ["restock", "place"])

    def test_failure_creates_no_file_or_directory(self):
        empty = self.root / "empty"
        app = OrderDesk(empty)
        with self.assertRaises(ValueError):
            app.transfer_reservation("ghost-1", "ghost-2", [{"sku": "T", "quantity": 1}])
        self.assertFalse(empty.exists())

    def test_both_orders_get_events_with_full_snapshot(self):
        self._unmanaged_pair()
        result = self.app.transfer_reservation("A", "B", [
            {"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 2},
        ])
        for order_id in ("A", "B"):
            history = self.app.history(order_id)
            self.assertTrue(history["complete"])
            self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                             [(1, "place"), (2, "transfer-reservation")])
            self.assertEqual(set(history["events"][1]), {"sequence", "action", "result"})
            self.assertEqual(history["events"][1]["result"], result)
        # Transfer changes ownership only: no stock history is recorded.
        self.assertEqual([e["action"] for e in self.app.stock_history("T")["events"]],
                         ["restock", "place"])
        self.assertEqual([e["action"] for e in self.app.stock_history("C")["events"]],
                         ["restock", "place"])

    def test_legacy_orders_start_history_at_one_incomplete(self):
        order = {"status": "placed",
                 "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100,
                            "subtotal_cents": 200}],
                 "total_cents": 200}
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "inventory": {"T": {"on_hand": 7, "reserved": 2}},
            "orders": {"OLD": dict(order, order_id="OLD"),
                       "NEW": dict(order, order_id="NEW")},
            "reservations": {"NEW": {"T": 2}},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        result = app.transfer_reservation("NEW", "OLD", [{"sku": "T", "quantity": 2}])
        for order_id in ("OLD", "NEW"):
            history = app.history(order_id)
            self.assertFalse(history["complete"])
            self.assertEqual(history["events"], [
                {"sequence": 1, "action": "transfer-reservation", "result": result},
            ])
        self.assertEqual(app.stock("T"),
                         {"sku": "T", "on_hand": 7, "reserved": 2, "available": 5})

    def test_persists_across_reopen(self):
        self._unmanaged_pair()
        result = self.app.transfer_reservation("A", "B", [
            {"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 2},
        ])
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.get("A")["status"], "placed")
        for order_id in ("A", "B"):
            events = reopened.history(order_id)["events"]
            self.assertEqual(events[-1]["action"], "transfer-reservation")
            self.assertEqual(events[-1]["result"], result)
        self.assertEqual(reopened.stock("T")["reserved"], 3)
        self.assertEqual(reopened.stock("C")["reserved"], 2)

    def test_pick_list_and_plan_reflect_new_ownership(self):
        self.app.place("B", [{"sku": "T", "quantity": 7}])
        self.app.restock("T", 5)
        self.app.place("A", [{"sku": "T", "quantity": 3}])
        self.app.transfer_reservation("A", "B", [{"sku": "T", "quantity": 2}])
        pick = self.app.pick_list(["A", "B"])
        line = pick["lines"][0]
        self.assertEqual(line["quantity"], 10)
        self.assertEqual(line["reserved"], 3)
        self.assertEqual(line["orders"], [
            {"order_id": "A", "quantity": 3, "reserved": 1},
            {"order_id": "B", "quantity": 7, "reserved": 2},
        ])
        plan = {row["order_id"]: row for row in self.app.reservation_plan(["B", "A"])}
        self.assertFalse(plan["B"]["can_reserve"])
        self.assertTrue(plan["A"]["can_reserve"])
        self.assertEqual([l["reserved"] for l in plan["B"]["lines"]], [2])

    def test_later_cancel_amend_and_ship_follow_actual_reservations(self):
        self._unmanaged_pair()
        self.app.transfer_reservation("A", "B", [
            {"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 2},
        ])
        # A holds nothing now: cancelling it releases no stock.
        before = self.app.stock("T")
        self.app.cancel("A")
        self.assertEqual(self.app.stock("T"), before)
        # B ships only what it actually holds.
        self.app.ship("B", "DHL", "1")
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 2, "reserved": 0, "available": 2})
        self.assertEqual(self.app.stock("C"),
                         {"sku": "C", "on_hand": 3, "reserved": 0, "available": 3})

    def test_cli_success_failure_and_array_independence(self):
        self.app.place("X2", [{"sku": "T", "quantity": 5}])
        self.app.restock("T", 5)
        self.app.place("X1", [{"sku": "T", "quantity": 2}])
        payload = self.root / "tr.json"
        payload.write_text(json.dumps(
            {"source_id": " X1 ", "target_id": "X2",
             "lines": [{"sku": "T", "quantity": 1}]}), encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                             "transfer-reservation", str(payload)],
                            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout), {
            "source_id": "X1", "target_id": "X2",
            "lines": [{"sku": "T", "quantity": 1,
                       "source_reserved": 1, "target_reserved": 1}],
        })
        payload.write_text(json.dumps([
            {"source_id": "X1", "target_id": "X2",
             "lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": "X1", "target_id": "X2",
             "lines": [{"sku": "T", "quantity": 9}]},
        ]), encoding="utf-8")
        run = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                              "transfer-reservation", str(payload)],
                             text=True, capture_output=True)
        self.assertEqual(run.returncode, 2, run.stdout)
        reopened = OrderDesk(self.root)
        # The first array item succeeded; the failed second item did not roll
        # it back or consume another sequence.
        self.assertEqual([e["action"] for e in reopened.history("X1")["events"]],
                         ["place", "transfer-reservation", "transfer-reservation"])
        self.assertEqual(reopened.stock("T")["reserved"], 2)

if __name__ == "__main__":
    unittest.main()
