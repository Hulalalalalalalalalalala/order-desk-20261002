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

    def _two_orders(self):
        # O2 is placed while the products are unmanaged, so it holds no
        # reservations; O1 is placed after restocking and holds 4 T and 2 C.
        self.app.place("O2", [{"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 2}])
        self.app.restock("T", 10)
        self.app.restock("C", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 4}, {"sku": "C", "quantity": 2}])

    def test_basic_transfer_result_and_balances(self):
        self._two_orders()
        result = self.app.transfer_reservation("O1", "O2", [
            {"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1},
        ])
        self.assertEqual(set(result), {"source_id", "target_id", "lines"})
        self.assertEqual(result["source_id"], "O1")
        self.assertEqual(result["target_id"], "O2")
        self.assertEqual(result["lines"], [
            {"sku": "C", "quantity": 1, "source_reserved": 1, "target_reserved": 1},
            {"sku": "T", "quantity": 2, "source_reserved": 2, "target_reserved": 2},
        ])
        # Totals on the inventory side never move.
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 10, "reserved": 4, "available": 6})
        self.assertEqual(self.app.stock("C"),
                         {"sku": "C", "on_hand": 5, "reserved": 2, "available": 3})

    def test_duplicate_skus_merge_and_extra_fields_ignored(self):
        self._two_orders()
        result = self.app.transfer_reservation(" O1 ", " O2\n", [
            {"sku": "T", "quantity": 1, "note": "x"},
            {"sku": " T ", "quantity": 2, "extra": [1]},
        ])
        self.assertEqual(result["lines"], [
            {"sku": "T", "quantity": 3, "source_reserved": 1, "target_reserved": 3},
        ])

    def test_paused_product_does_not_block_transfer(self):
        self._two_orders()
        self.app.set_product_enabled("T", False)
        result = self.app.transfer_reservation("O1", "O2", [{"sku": "T", "quantity": 1}])
        self.assertEqual(result["lines"][0]["target_reserved"], 1)

    def test_order_content_status_and_other_orders_untouched(self):
        self._two_orders()
        self.app.place("O3", [{"sku": "T", "quantity": 1}])
        before_o1 = self.app.get("O1")
        before_o2 = self.app.get("O2")
        before_o3 = self.app.get("O3")
        self.app.transfer_reservation("O1", "O2", [{"sku": "T", "quantity": 2}])
        self.assertEqual(self.app.get("O1"), before_o1)
        self.assertEqual(self.app.get("O2"), before_o2)
        self.assertEqual(self.app.get("O3"), before_o3)
        self.assertEqual(self.app.get("O1")["status"], "placed")
        self.assertEqual(self.app.get("O1")["total_cents"], 800)

    def test_no_stock_history_events_and_sequences_untouched(self):
        self._two_orders()
        t_events = len(self.app.stock_history("T")["events"])
        c_events = len(self.app.stock_history("C")["events"])
        self.app.transfer_reservation("O1", "O2", [{"sku": "T", "quantity": 1},
                                                   {"sku": "C", "quantity": 1}])
        self.assertEqual(len(self.app.stock_history("T")["events"]), t_events)
        self.assertEqual(len(self.app.stock_history("C")["events"]), c_events)

    def test_history_events_on_both_orders(self):
        self._two_orders()
        result = self.app.transfer_reservation("O1", "O2", [{"sku": "T", "quantity": 2}])
        for order_id in ("O1", "O2"):
            history = self.app.history(order_id)
            self.assertTrue(history["complete"])
            self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                             [(1, "place"), (2, "transfer-reservation")])
            self.assertEqual(set(history["events"][1]), {"sequence", "action", "result"})
            self.assertEqual(history["events"][1]["result"], result)
        # A second transfer continues each order's own sequence.
        self.app.transfer_reservation("O1", "O2", [{"sku": "T", "quantity": 1}])
        self.assertEqual([e["sequence"] for e in self.app.history("O1")["events"]], [1, 2, 3])
        self.assertEqual([e["sequence"] for e in self.app.history("O2")["events"]], [1, 2, 3])

    def test_legacy_orders_start_history_at_one_incomplete(self):
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "inventory": {"T": {"on_hand": 9, "reserved": 3}},
            "orders": {
                "A": {"order_id": "A", "status": "placed",
                      "lines": [{"sku": "T", "quantity": 3, "unit_price_cents": 100,
                                 "subtotal_cents": 300}], "total_cents": 300},
                "B": {"order_id": "B", "status": "placed",
                      "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100,
                                 "subtotal_cents": 200}], "total_cents": 200},
            },
            "reservations": {"A": {"T": 3}},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        result = app.transfer_reservation("A", "B", [{"sku": "T", "quantity": 2}])
        self.assertEqual(result["lines"], [
            {"sku": "T", "quantity": 2, "source_reserved": 1, "target_reserved": 2},
        ])
        for order_id in ("A", "B"):
            history = app.history(order_id)
            self.assertFalse(history["complete"])
            self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                             [(1, "transfer-reservation")])
            self.assertEqual(history["events"][0]["result"], result)

    def test_persists_across_reopen(self):
        self._two_orders()
        result = self.app.transfer_reservation("O1", "O2", [{"sku": "T", "quantity": 2}])
        reopened = OrderDesk(self.root)
        pick = reopened.pick_list(["O1", "O2"])
        tea = next(line for line in pick["lines"] if line["sku"] == "T")
        row = {r["order_id"]: r for r in tea["orders"]}
        self.assertEqual(row["O1"]["reserved"], 2)
        self.assertEqual(row["O2"]["reserved"], 2)
        self.assertEqual(reopened.history("O1")["events"][1]["result"], result)
        self.assertEqual(reopened.history("O2")["events"][1]["result"], result)

    def test_transfer_down_to_zero_drops_source_record(self):
        # O2 ordered enough to absorb everything O1 holds.
        self.app.place("O2", [{"sku": "T", "quantity": 4}, {"sku": "C", "quantity": 2}])
        self.app.restock("T", 10)
        self.app.restock("C", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 4}, {"sku": "C", "quantity": 2}])
        self.app.transfer_reservation("O1", "O2", [
            {"sku": "T", "quantity": 4}, {"sku": "C", "quantity": 2},
        ])
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertNotIn("O1", data["reservations"])
        self.assertEqual(data["reservations"]["O2"], {"C": 2, "T": 4})
        # Source now has nothing left to give.
        with self.assertRaises(ValueError):
            self.app.transfer_reservation("O1", "O2", [{"sku": "T", "quantity": 1}])

    def test_repeat_submission_judged_by_current_balances(self):
        self._two_orders()
        self.app.transfer_reservation("O1", "O2", [{"sku": "T", "quantity": 2}])
        # The same request again would exceed the target's ordered quantity.
        with self.assertRaises(ValueError):
            self.app.transfer_reservation("O1", "O2", [{"sku": "T", "quantity": 2}])
        # But one more unit still fits the target's remaining allowance.
        result = self.app.transfer_reservation("O1", "O2", [{"sku": "T", "quantity": 1}])
        self.assertEqual(result["lines"][0]["source_reserved"], 1)
        self.assertEqual(result["lines"][0]["target_reserved"], 3)

    def test_transferred_reservations_drive_pick_plan_and_ship(self):
        self._two_orders()
        self.app.transfer_reservation("O1", "O2", [{"sku": "T", "quantity": 3}])
        pick = self.app.pick_list(["O2"])
        tea = next(line for line in pick["lines"] if line["sku"] == "T")
        self.assertEqual(tea["orders"],
                         [{"order_id": "O2", "quantity": 3, "reserved": 3}])
        plan = self.app.reservation_plan(["O2"])
        tea = next(line for line in plan[0]["lines"] if line["sku"] == "T")
        self.assertEqual(tea["reserved"], 3)
        # Shipping the target deducts exactly the transferred reservation.
        self.app.ship("O2", "DHL", "1")
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 7, "reserved": 1, "available": 6})
        # The source keeps its remaining reservation for its own ship.
        self.app.ship("O1", "UPS", "2")
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 6, "reserved": 0, "available": 6})

    def test_amend_and_cancel_use_transferred_reservations(self):
        self._two_orders()
        self.app.transfer_reservation("O1", "O2", [{"sku": "T", "quantity": 2}])
        # The target's own allowance now includes the transferred amount.
        self.app.amend("O2", [{"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 2}])
        self.assertEqual(self.app.stock("T")["reserved"], 5)
        self.app.cancel("O2")
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 10, "reserved": 2, "available": 8})

    def test_input_validation(self):
        self._two_orders()
        lines = [{"sku": "T", "quantity": 1}]
        for bad in (None, 123, 1.5, b"O1", ["O1"], {"x": 1}, "   ", ""):
            with self.assertRaises(ValueError):
                self.app.transfer_reservation(bad, "O2", lines)
            with self.assertRaises(ValueError):
                self.app.transfer_reservation("O1", bad, lines)
        # Same order on both sides.
        with self.assertRaises(ValueError):
            self.app.transfer_reservation("O1", " O1 ", lines)
        # Lines must be a nonempty list of objects.
        for bad in (None, "T", 123, [], {}, [None], ["T"], [1], [[]]):
            with self.assertRaises(ValueError):
                self.app.transfer_reservation("O1", "O2", bad)
        # Missing fields and bad sku/quantity.
        for bad_line in ({}, {"sku": "T"}, {"quantity": 1},
                         {"sku": "  ", "quantity": 1}, {"sku": None, "quantity": 1},
                         {"sku": "T", "quantity": 0}, {"sku": "T", "quantity": -1},
                         {"sku": "T", "quantity": 1.5}, {"sku": "T", "quantity": True},
                         {"sku": "T", "quantity": "1"}, {"sku": "T", "quantity": None}):
            with self.assertRaises(ValueError):
                self.app.transfer_reservation("O1", "O2", [bad_line])
        # Ids and skus are trimmed but case sensitive.
        with self.assertRaises(ValueError):
            self.app.transfer_reservation("o1", "O2", lines)
        with self.assertRaises(ValueError):
            self.app.transfer_reservation("O1", "O2", [{"sku": "t", "quantity": 1}])

    def test_unknown_order_and_status_rules(self):
        self._two_orders()
        lines = [{"sku": "T", "quantity": 1}]
        with self.assertRaises(ValueError):
            self.app.transfer_reservation("ghost", "O2", lines)
        with self.assertRaises(ValueError):
            self.app.transfer_reservation("O1", "ghost", lines)
        self.app.place("O3", [{"sku": "T", "quantity": 1}])
        self.app.cancel("O3")
        with self.assertRaises(ValueError):
            self.app.transfer_reservation("O3", "O2", lines)
        with self.assertRaises(ValueError):
            self.app.transfer_reservation("O1", "O3", lines)
        self.app.place("O4", [{"sku": "T", "quantity": 1}])
        self.app.ship("O4", "DHL", "1")
        with self.assertRaises(ValueError):
            self.app.transfer_reservation("O4", "O2", lines)
        with self.assertRaises(ValueError):
            self.app.transfer_reservation("O1", "O4", lines)

    def test_product_must_be_known_managed_and_in_both_orders(self):
        self._two_orders()
        self.app.add_product("U", "Unmanaged", 50)
        raw = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.transfer_reservation("O1", "O2", [{"sku": "GONE", "quantity": 1}])
        self.assertEqual(self.app.path.read_bytes(), raw)
        # Unmanaged product, even if present in both orders.
        self.app.amend("O1", [{"sku": "T", "quantity": 4}, {"sku": "C", "quantity": 2},
                              {"sku": "U", "quantity": 1}])
        self.app.amend("O2", [{"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 2},
                              {"sku": "U", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.transfer_reservation("O1", "O2", [{"sku": "U", "quantity": 1}])
        # Product only in the source order.
        self.app.amend("O2", [{"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 2}])
        with self.assertRaises(ValueError):
            self.app.transfer_reservation("O1", "O2", [{"sku": "U", "quantity": 1}])
        # Product only in the target order.
        self.app.amend("O1", [{"sku": "T", "quantity": 4}, {"sku": "C", "quantity": 2}])
        self.app.amend("O2", [{"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 2},
                              {"sku": "U", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.transfer_reservation("O1", "O2", [{"sku": "U", "quantity": 1}])

    def test_source_shortage_and_target_overflow_reject_without_writing(self):
        self._two_orders()
        raw = self.app.path.read_bytes()
        # Source holds only 4 T and 2 C.
        with self.assertRaises(ValueError):
            self.app.transfer_reservation("O1", "O2", [{"sku": "T", "quantity": 5}])
        # Target ordered only 3 T; it already holds zero, so 4 would overflow.
        with self.assertRaises(ValueError):
            self.app.transfer_reservation("O1", "O2", [{"sku": "T", "quantity": 4}])
        # A valid line does not rescue an invalid one: the whole request fails.
        with self.assertRaises(ValueError):
            self.app.transfer_reservation("O1", "O2", [
                {"sku": "T", "quantity": 1}, {"sku": "C", "quantity": 3},
            ])
        self.assertEqual(self.app.path.read_bytes(), raw)
        self.assertEqual([e["action"] for e in self.app.history("O1")["events"]], ["place"])
        self.assertEqual([e["action"] for e in self.app.history("O2")["events"]], ["place"])
        self.assertEqual(self.app.stock("T")["reserved"], 4)

    def test_target_existing_reservation_counts_toward_cap(self):
        # O2 ordered 5 T while unmanaged; O1 holds 5 T after restocking.
        self.app.place("O2", [{"sku": "T", "quantity": 5}])
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 5}])
        self.app.transfer_reservation("O1", "O2", [{"sku": "T", "quantity": 2}])
        # O2 already holds 2 of its 5, so 4 more would exceed the ordered total.
        with self.assertRaises(ValueError):
            self.app.transfer_reservation("O1", "O2", [{"sku": "T", "quantity": 4}])
        result = self.app.transfer_reservation("O1", "O2", [{"sku": "T", "quantity": 3}])
        self.assertEqual(result["lines"][0]["source_reserved"], 0)
        self.assertEqual(result["lines"][0]["target_reserved"], 5)

    def test_failure_creates_no_file_or_directory(self):
        empty = self.root / "empty"
        app = OrderDesk(empty)
        with self.assertRaises(ValueError):
            app.transfer_reservation("A", "B", [{"sku": "T", "quantity": 1}])
        self.assertFalse(empty.exists())

    def test_cli_transfer_reservation_success_and_failure(self):
        self._two_orders()
        payload = self.root / "t.json"
        payload.write_text(json.dumps({
            "source_id": " O1 ", "target_id": "O2",
            "lines": [{"sku": "T", "quantity": 2}],
        }), encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                             "transfer-reservation", str(payload)],
                            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout), {
            "source_id": "O1", "target_id": "O2",
            "lines": [{"sku": "T", "quantity": 2, "source_reserved": 2, "target_reserved": 2}],
        })
        payload.write_text(json.dumps({
            "source_id": "O1", "target_id": "O2",
            "lines": [{"sku": "T", "quantity": 9}],
        }), encoding="utf-8")
        failed = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                                 "transfer-reservation", str(payload)],
                                text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2, failed.stdout)
        self.assertIn("error", json.loads(failed.stderr))

    def test_cli_array_runs_item_by_item(self):
        self._two_orders()
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([
            {"source_id": "O1", "target_id": "O2", "lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": "O1", "target_id": "ghost", "lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": "O1", "target_id": "O2", "lines": [{"sku": "C", "quantity": 1}]},
        ]), encoding="utf-8")
        run = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                              "transfer-reservation", str(batch)],
                             text=True, capture_output=True)
        self.assertEqual(run.returncode, 2, run.stdout)
        reopened = OrderDesk(self.root)
        # The first item succeeded and was not rolled back; the third never ran.
        self.assertEqual([e["action"] for e in reopened.history("O1")["events"]],
                         ["place", "transfer-reservation"])
        self.assertEqual([e["action"] for e in reopened.history("O2")["events"]],
                         ["place", "transfer-reservation"])
        pick = reopened.pick_list(["O1", "O2"])
        rows = {line["sku"]: {r["order_id"]: r["reserved"] for r in line["orders"]}
                for line in pick["lines"]}
        self.assertEqual(rows["T"], {"O1": 3, "O2": 1})
        self.assertEqual(rows["C"], {"O1": 2, "O2": 0})

if __name__ == "__main__":
    unittest.main()
