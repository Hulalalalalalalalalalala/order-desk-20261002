import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk


class AmendReturnTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)
        self.app.add_product("U", "Unmanaged", 0)

    def _shipped(self, order_id, lines, stock=None):
        for sku, quantity in (stock or {"T": 10, "C": 10}).items():
            self.app.restock(sku, quantity)
        self.app.place(order_id, lines)
        self.app.ship(order_id, "DHL", "TRK-" + order_id)

    def test_amend_replaces_whole_registration_with_snapshot(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 2}])
        self.app.record_return("O1", "R1", [
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 2},
        ])
        result = self.app.amend_return("R1", [
            {"sku": "T", "quantity": 2},
            {"sku": "C", "quantity": 1},
        ], [
            {"sku": "T", "quantity": 1},
            {"sku": "C", "quantity": 2},
        ])
        self.assertEqual(result, {
            "order_id": "O1",
            "return_id": "R1",
            "before": [{"sku": "C", "quantity": 1}, {"sku": "T", "quantity": 2}],
            "after": [{"sku": "C", "quantity": 2}, {"sku": "T", "quantity": 1}],
        })
        self.assertEqual(set(result), {"order_id", "return_id", "before", "after"})
        for view in (result["before"], result["after"]):
            for line in view:
                self.assertEqual(set(line), {"sku", "quantity"})
        view = self.app.get_returns("O1")
        self.assertEqual(view["records"], [{
            "order_id": "O1", "return_id": "R1",
            "lines": [{"sku": "C", "quantity": 2}, {"sku": "T", "quantity": 1}],
        }])

    def test_duplicate_skus_merge_in_both_lists_and_current(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [
            {"sku": "T", "quantity": 1},
            {"sku": "T", "quantity": 2},
        ])
        # Expected duplicates merge to 3, out of order.
        result = self.app.amend_return("R1", [
            {"sku": "T", "quantity": 2},
            {"sku": "T", "quantity": 1},
        ], [
            {"sku": "T", "quantity": 2},
            {"sku": "T", "quantity": 2, "ignored": True},
        ])
        self.assertEqual(result["before"], [{"sku": "T", "quantity": 3}])
        self.assertEqual(result["after"], [{"sku": "T", "quantity": 4}])

    def test_expected_mismatch_rejects_even_when_target_equals_current(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.amend_return("R1", [{"sku": "T", "quantity": 2}],
                                  [{"sku": "T", "quantity": 3}])
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_unchanged_content_succeeds_but_writes_nothing(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        before = self.app.path.read_bytes()
        result = self.app.amend_return("R1", [{"sku": "T", "quantity": 3}],
                                       [{"sku": "T", "quantity": 1},
                                        {"sku": "T", "quantity": 2}])
        self.assertEqual(result["before"], [{"sku": "T", "quantity": 3}])
        self.assertEqual(result["after"], [{"sku": "T", "quantity": 3}])
        self.assertEqual(self.app.path.read_bytes(), before)
        events = self.app.history("O1")["events"]
        self.assertEqual([e["action"] for e in events],
                         ["place", "ship", "record-return"])

    def test_quota_example_four_allowed_five_rejected(self):
        # Five ordered; another registration holds one; this one had three.
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        result = self.app.amend_return("R1", [{"sku": "T", "quantity": 3}],
                                       [{"sku": "T", "quantity": 4}])
        self.assertEqual(result["after"], [{"sku": "T", "quantity": 4}])
        # Remaining returnable quantity reflects the new registration.
        self.assertEqual(self.app.get_returns("O1")["remaining"],
                         [{"sku": "T", "quantity": 0}])

    def test_quota_example_five_rejected(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.amend_return("R1", [{"sku": "T", "quantity": 3}],
                                  [{"sku": "T", "quantity": 5}])
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(
            [r["return_id"] for r in self.app.get_returns("O1")["records"]],
            ["R1", "R2"])

    def test_old_quantity_frees_quota_for_growth(self):
        # Five ordered, no other returns: 3 can become 5 but not 6.
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.app.amend_return("R1", [{"sku": "T", "quantity": 3}],
                              [{"sku": "T", "quantity": 5}])
        self.assertEqual(self.app.get_returns("O1")["remaining"],
                         [{"sku": "T", "quantity": 0}])

    def test_shrink_then_grow_uses_new_quota(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        # 3 -> 1 frees room; R2 keeps 1.
        self.app.amend_return("R1", [{"sku": "T", "quantity": 3}],
                              [{"sku": "T", "quantity": 1}])
        self.app.amend_return("R1", [{"sku": "T", "quantity": 1}],
                              [{"sku": "T", "quantity": 4}])
        with self.assertRaises(ValueError):
            self.app.amend_return("R1", [{"sku": "T", "quantity": 4}],
                                  [{"sku": "T", "quantity": 5}])

    def test_received_other_return_occupies_quota(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        self.app.receive_return("R2")
        with self.assertRaises(ValueError):
            self.app.amend_return("R1", [{"sku": "T", "quantity": 3}],
                                  [{"sku": "T", "quantity": 5}])
        self.app.amend_return("R1", [{"sku": "T", "quantity": 3}],
                              [{"sku": "T", "quantity": 4}])

    def test_cancelled_other_return_does_not_occupy_quota(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        self.app.cancel_return("R2")
        # R2's one freed: R1 may grow to 5.
        self.app.amend_return("R1", [{"sku": "T", "quantity": 3}],
                              [{"sku": "T", "quantity": 5}])

    def test_add_and_remove_original_order_skus(self):
        self._shipped("O1", [{"sku": "T", "quantity": 3}, {"sku": "C", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        result = self.app.amend_return("R1", [{"sku": "T", "quantity": 1}], [
            {"sku": "C", "quantity": 2},
        ])
        self.assertEqual(result["before"], [{"sku": "T", "quantity": 1}])
        self.assertEqual(result["after"], [{"sku": "C", "quantity": 2}])
        self.assertEqual(self.app.get_returns("O1")["remaining"], [
            {"sku": "C", "quantity": 0},
            {"sku": "T", "quantity": 3},
        ])

    def test_new_sku_not_in_original_order_rejected(self):
        self.app.restock("U", 5)
        self._shipped("O1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.amend_return("R1", [{"sku": "T", "quantity": 1}],
                                  [{"sku": "U", "quantity": 1}])
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_paused_unmanaged_and_missing_catalog_do_not_block(self):
        # T pauses, U is unmanaged, X disappears from the catalog; all three are
        # original order lines and may stay or grow within ordered quantity.
        data = {
            "products": {
                "T": {"sku": "T", "name": "Tea", "price_cents": 100, "enabled": False},
                "U": {"sku": "U", "name": "Unmanaged", "price_cents": 0},
            },
            "inventory": {"T": {"on_hand": 3, "reserved": 0}},
            "orders": {"OLD": {
                "order_id": "OLD", "status": "delivered",
                "lines": [
                    {"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200},
                    {"sku": "U", "quantity": 2, "unit_price_cents": 0, "subtotal_cents": 0},
                    {"sku": "X", "quantity": 1, "unit_price_cents": 50, "subtotal_cents": 50},
                ],
                "total_cents": 250,
                "shipment": {"carrier": "DHL", "tracking_no": "Z"},
                "delivery": {"recipient": "Ann", "delivered_on": "2026-09-01"},
            }},
            "returns": {"OLD": [
                {"order_id": "OLD", "return_id": "L1", "lines": [{"sku": "T", "quantity": 1}]},
            ]},
        }
        self._load(data)
        result = self.app.amend_return("L1", [{"sku": "T", "quantity": 1}], [
            {"sku": "T", "quantity": 2},
            {"sku": "U", "quantity": 2},
            {"sku": "X", "quantity": 1},
        ])
        self.assertEqual([line["sku"] for line in result["after"]], ["T", "U", "X"])

    def test_delivered_order_allowed_placed_order_rejected(self):
        self._shipped("O1", [{"sku": "T", "quantity": 3}])
        self.app.confirm_delivery("O1", "Ann", "2026-09-01")
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        result = self.app.amend_return("R1", [{"sku": "T", "quantity": 1}],
                                       [{"sku": "T", "quantity": 2}])
        self.assertEqual(result["after"], [{"sku": "T", "quantity": 2}])
        # A return under a placed order (legacy data) cannot be amended.
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "orders": {"P": {
                "order_id": "P", "status": "placed",
                "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200}],
                "total_cents": 200,
            }},
            "returns": {"P": [
                {"order_id": "P", "return_id": "RP", "lines": [{"sku": "T", "quantity": 1}]},
            ]},
        }
        root2 = Path(self.temp.name) / "second"
        root2.mkdir(parents=True)
        OrderDesk(root2).path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            OrderDesk(root2).amend_return("RP", [{"sku": "T", "quantity": 1}],
                                          [{"sku": "T", "quantity": 2}])

    def test_unknown_cancelled_and_received_returns_rejected(self):
        self._shipped("O1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R3", [{"sku": "T", "quantity": 1}])
        self.app.cancel_return("R2")
        self.app.receive_return("R3")
        one = [{"sku": "T", "quantity": 1}]
        with self.assertRaises(ValueError):
            self.app.amend_return("nope", one, one)
        with self.assertRaises(ValueError):
            self.app.amend_return("R2", one, one)
        with self.assertRaises(ValueError):
            self.app.amend_return("R3", one, one)

    def test_invalid_identifier_and_lists_and_lines(self):
        self._shipped("O1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        good = [{"sku": "T", "quantity": 1}]
        for bad_id in (None, 123, 1.5, b"R1", ["R1"], {"x": 1}, "   ", "\t\n", True):
            with self.assertRaises(ValueError):
                self.app.amend_return(bad_id, good, good)
        for bad_lines in (None, [], {}, "x", 1, True):
            with self.assertRaises(ValueError):
                self.app.amend_return("R1", bad_lines, good)
            with self.assertRaises(ValueError):
                self.app.amend_return("R1", good, bad_lines)
        for bad_line in (None, 1, "x", [], True):
            with self.assertRaises(ValueError):
                self.app.amend_return("R1", [bad_line], good)
            with self.assertRaises(ValueError):
                self.app.amend_return("R1", good, [bad_line])
        for bad_sku in (None, 1, 1.5, b"T", ["T"], {"x": 1}, "   ", "\t\n", True):
            with self.assertRaises(ValueError):
                self.app.amend_return("R1", good, [{"sku": bad_sku, "quantity": 1}])
            with self.assertRaises(ValueError):
                self.app.amend_return("R1", [{"sku": bad_sku, "quantity": 1}], good)
        for bad_qty in (None, 0, -1, 1.5, "1", b"1", [1], {"x": 1}, True, False):
            with self.assertRaises(ValueError):
                self.app.amend_return("R1", good, [{"sku": "T", "quantity": bad_qty}])
            with self.assertRaises(ValueError):
                self.app.amend_return("R1", [{"sku": "T", "quantity": bad_qty}], good)

    def test_whitespace_trimmed_and_case_sensitive(self):
        self.app.add_product("t", "lower tea", 100)
        self._shipped("O1", [{"sku": "T", "quantity": 3}, {"sku": "t", "quantity": 3}])
        self.app.record_return("O1", "Ra", [{"sku": "T", "quantity": 1}])
        result = self.app.amend_return("  Ra  ", [{"sku": "  T  ", "quantity": 1}],
                                       [{"sku": "T", "quantity": 2}])
        self.assertEqual(result["return_id"], "Ra")
        with self.assertRaises(ValueError):
            self.app.amend_return("ra", [{"sku": "T", "quantity": 2}],
                                  [{"sku": "T", "quantity": 2}])
        # Lowercase t is a different sku and does not match the T registration.
        with self.assertRaises(ValueError):
            self.app.amend_return("Ra", [{"sku": "t", "quantity": 2}],
                                  [{"sku": "t", "quantity": 2}])

    def test_failures_write_nothing_and_consume_no_sequence(self):
        self._shipped("O1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        before = self.app.path.read_bytes()
        good = [{"sku": "T", "quantity": 1}]
        for call in (
            lambda: self.app.amend_return("nope", good, good),
            lambda: self.app.amend_return("R1", [], good),
            lambda: self.app.amend_return("R1", good, []),
            lambda: self.app.amend_return("R1", good, [{"sku": "T", "quantity": 4}]),
            lambda: self.app.amend_return("R1", [{"sku": "T", "quantity": 2}], good),
        ):
            with self.assertRaises(ValueError):
                call()
        self.assertEqual(self.app.path.read_bytes(), before)
        events = self.app.history("O1")["events"]
        self.assertEqual([(e["sequence"], e["action"]) for e in events],
                         [(1, "place"), (2, "ship"), (3, "record-return")])

    def test_failed_amend_creates_no_directory(self):
        empty_root = Path(self.temp.name) / "empty"
        fresh = OrderDesk(empty_root)
        good = [{"sku": "T", "quantity": 1}]
        with self.assertRaises(ValueError):
            fresh.amend_return("R1", good, good)
        self.assertFalse(empty_root.exists())

    def test_history_event_snapshot_and_sequence(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 1}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        result = self.app.amend_return("R1", [{"sku": "T", "quantity": 1}], [
            {"sku": "T", "quantity": 2},
            {"sku": "C", "quantity": 1},
        ])
        history = self.app.history("O1")
        self.assertTrue(history["complete"])
        events = history["events"]
        self.assertEqual([(e["sequence"], e["action"]) for e in events],
                         [(1, "place"), (2, "ship"), (3, "record-return"), (4, "amend-return")])
        self.assertEqual(set(events[3]), {"sequence", "action", "result"})
        self.assertEqual(events[3]["result"], result)
        # Old snapshots stay untouched.
        self.assertEqual(events[2]["result"]["return_id"], "R1")

    def test_legacy_order_without_history_starts_at_one_complete_false(self):
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "inventory": {"T": {"on_hand": 3, "reserved": 0}},
            "orders": {"OLD": {
                "order_id": "OLD", "status": "shipped",
                "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200}],
                "total_cents": 200,
                "shipment": {"carrier": "DHL", "tracking_no": "Z"},
            }},
            "returns": {"OLD": [
                {"order_id": "OLD", "return_id": "L1",
                 "lines": [{"sku": "T", "quantity": 1}, {"sku": "T", "quantity": 1}]},
            ]},
        }
        self._load(data)
        # Legacy missing receipt reads as pending; legacy duplicate skus merge.
        result = self.app.amend_return("L1", [{"sku": "T", "quantity": 2}],
                                       [{"sku": "T", "quantity": 1}])
        self.assertEqual(result["before"], [{"sku": "T", "quantity": 2}])
        history = self.app.history("OLD")
        self.assertFalse(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                         [(1, "amend-return")])
        self.assertEqual(history["events"][0]["result"], result)
        self.assertEqual(self.app.stock("T")["on_hand"], 3)

    def test_legacy_missing_order_rejected_without_write(self):
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "returns": {"GONE": [
                {"order_id": "GONE", "return_id": "RG", "lines": [{"sku": "T", "quantity": 1}]},
            ]},
        }
        self._load(data)
        before = self.app.path.read_bytes()
        good = [{"sku": "T", "quantity": 1}]
        with self.assertRaises(ValueError):
            self.app.amend_return("RG", good, good)
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_stock_reservations_orders_and_other_returns_untouched(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        self.app.place("O2", [{"sku": "T", "quantity": 2}])
        order_before = self.app.get("O1")
        stock_before = self.app.stock("T")
        other_before = self.app.get_returns("O1")["records"]
        self.app.amend_return("R1", [{"sku": "T", "quantity": 3}],
                              [{"sku": "T", "quantity": 4}])
        self.assertEqual(self.app.get("O1"), order_before)
        self.assertEqual(self.app.stock("T"), stock_before)
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["reservations"]["O2"], {"T": 2})
        records = self.app.get_returns("O1")["records"]
        self.assertEqual([r for r in records if r["return_id"] == "R2"], other_before[1:])

    def test_reopen_worklist_cancel_and_receive_use_new_quantities(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        result = self.app.amend_return("R1", [{"sku": "T", "quantity": 3}],
                                       [{"sku": "T", "quantity": 4}])
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.get_returns("O1")["remaining"],
                         [{"sku": "T", "quantity": 1}])
        entry = reopened.return_worklist()[0]
        self.assertEqual(entry["return_id"], "R1")
        self.assertEqual(entry["lines"], [{"sku": "T", "quantity": 4}])
        receipt = reopened.receive_return("R1")
        self.assertEqual(receipt["lines"], [
            {"sku": "T", "quantity": 4,
             "before": {"sku": "T", "on_hand": 5, "reserved": 0, "available": 5},
             "after": {"sku": "T", "on_hand": 9, "reserved": 0, "available": 9}},
        ])

    def test_reopen_then_cancel_uses_new_quantities(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.app.amend_return("R1", [{"sku": "T", "quantity": 3}],
                              [{"sku": "T", "quantity": 4}])
        reopened = OrderDesk(self.root)
        cancelled = reopened.cancel_return("R1")
        self.assertEqual(cancelled["lines"], [{"sku": "T", "quantity": 4}])
        self.assertEqual(reopened.get_returns("O1")["remaining"],
                         [{"sku": "T", "quantity": 5}])

    def test_batch_receive_after_amend_uses_new_quantities(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        self.app.amend_return("R1", [{"sku": "T", "quantity": 2}],
                              [{"sku": "T", "quantity": 3}])
        receipts = self.app.receive_return_batch(["R2", "R1"])
        self.assertEqual([r["return_id"] for r in receipts], ["R1", "R2"])
        self.assertEqual(receipts[0]["lines"][0]["quantity"], 3)
        self.assertEqual(self.app.stock("T")["on_hand"], 9)

    def test_cli_amend_and_array_partial_failure(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        payload = self.root / "in.json"
        payload.write_text(json.dumps([
            {"return_id": "R1",
             "expected_lines": [{"sku": "T", "quantity": 3}],
             "lines": [{"sku": "T", "quantity": 4}]},
            # Second row fails the original-content check; the first persists.
            {"return_id": "R2",
             "expected_lines": [{"sku": "T", "quantity": 9}],
             "lines": [{"sku": "T", "quantity": 1}]},
        ]), encoding="utf-8")
        failed = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "amend-return", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2, failed.stdout)
        self.assertIn("expected lines", json.loads(failed.stderr)["error"])
        records = self.app.get_returns("O1")["records"]
        self.assertEqual([r["lines"] for r in records],
                         [[{"sku": "T", "quantity": 4}], [{"sku": "T", "quantity": 1}]])

    def _load(self, data):
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        self.app = OrderDesk(self.root)


if __name__ == "__main__":
    unittest.main()
