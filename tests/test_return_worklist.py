import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk


class ReturnWorklistTests(unittest.TestCase):
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

    def test_default_is_pending_and_cross_order_sorted_by_return_id(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        self._shipped("O2", [{"sku": "C", "quantity": 2}])
        self.app.record_return("O2", "R2", [{"sku": "C", "quantity": 1}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        result = self.app.return_worklist()
        self.assertEqual([(r["order_id"], r["return_id"], r["stage"]) for r in result],
                         [("O1", "R1", "pending"), ("O2", "R2", "pending")])
        self.assertEqual(set(result[0]),
                         {"order_id", "return_id", "stage", "lines", "can_receive", "blockers"})

    def test_entry_shape_lines_merged_sorted(self):
        self._shipped("O1", [{"sku": "T", "quantity": 4}, {"sku": "C", "quantity": 3}])
        self.app.record_return("O1", "R1", [
            {"sku": "T", "quantity": 1},
            {"sku": "C", "quantity": 2},
            {"sku": "T", "quantity": 1},
        ])
        entry = self.app.return_worklist()[0]
        self.assertEqual(entry["lines"], [
            {"sku": "C", "quantity": 2},
            {"sku": "T", "quantity": 2},
        ])
        for line in entry["lines"]:
            self.assertEqual(set(line), {"sku", "quantity"})
        self.assertTrue(entry["can_receive"])
        self.assertEqual(entry["blockers"], [])

    def test_stage_filters(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "RP", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "RR", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "RC", [{"sku": "T", "quantity": 1}])
        self.app.receive_return("RR")
        self.app.cancel_return("RC")
        self.assertEqual([r["return_id"] for r in self.app.return_worklist()], ["RP"])
        self.assertEqual([r["return_id"] for r in self.app.return_worklist(stage="pending")], ["RP"])
        received = self.app.return_worklist(stage="received")
        self.assertEqual([r["return_id"] for r in received], ["RR"])
        self.assertEqual(received[0]["stage"], "received")
        cancelled = self.app.return_worklist(stage="cancelled")
        self.assertEqual([r["return_id"] for r in cancelled], ["RC"])
        self.assertEqual(cancelled[0]["stage"], "cancelled")
        self.assertEqual([r["return_id"] for r in self.app.return_worklist(stage="all")],
                         ["RC", "RP", "RR"])

    def test_stage_whitespace_trimmed(self):
        self.assertEqual(self.app.return_worklist(stage="  all  "), [])
        self.assertEqual(self.app.return_worklist(stage="\tpending\n"), [])

    def test_invalid_stage(self):
        for bad in (None, 123, 1.5, b"pending", ["pending"], {"stage": 1}, True,
                    "", "   ", "done", "PENDING", " pendingx"):
            with self.assertRaises(ValueError):
                self.app.return_worklist(stage=bad)

    def test_order_filter_known_empty_unknown_invalid(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        # Known order without matching registrations is an empty list, not an error.
        self.assertEqual(self.app.return_worklist(order_id="O1"), [])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.assertEqual([r["return_id"] for r in self.app.return_worklist(order_id=" O1 ")], ["R1"])
        with self.assertRaises(ValueError):
            self.app.return_worklist(order_id="NOPE")
        for bad in (123, b"O1", ["O1"], {"x": 1}, True, "   ", "\t\n"):
            with self.assertRaises(ValueError):
                self.app.return_worklist(order_id=bad)

    def test_order_filter_scopes_both_buckets(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self._shipped("O2", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O2", "R2", [{"sku": "T", "quantity": 1}])
        self.app.cancel_return("R2")
        self.assertEqual(
            [r["return_id"] for r in self.app.return_worklist(stage="all", order_id="O2")],
            ["R2"],
        )
        self.assertEqual(self.app.return_worklist(stage="pending", order_id="O2"), [])

    def test_unknown_product_and_unmanaged_blockers(self):
        # T sold and was returned, then its catalog entry was removed; U still
        # exists but has never been restocked.
        data = {
            "products": {"U": {"sku": "U", "name": "Unmanaged", "price_cents": 0}},
            "orders": {"OLD": {
                "order_id": "OLD", "status": "shipped",
                "lines": [
                    {"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100},
                    {"sku": "U", "quantity": 2, "unit_price_cents": 0, "subtotal_cents": 0},
                ],
                "total_cents": 100,
                "shipment": {"carrier": "DHL", "tracking_no": "Z"},
            }},
            "returns": {"OLD": [
                {"order_id": "OLD", "return_id": "L1", "lines": [
                    {"sku": "T", "quantity": 1},
                    {"sku": "U", "quantity": 2},
                ]},
            ]},
        }
        self._load(data)
        entry = self.app.return_worklist()[0]
        self.assertFalse(entry["can_receive"])
        self.assertEqual(entry["blockers"], [
            {"sku": "T", "reason": "unknown-product"},
            {"sku": "U", "reason": "unmanaged"},
        ])
        for blocker in entry["blockers"]:
            self.assertEqual(set(blocker), {"sku", "reason"})

    def test_legacy_unknown_and_unmanaged_blockers_sorted(self):
        data = {
            "products": {"X": {"sku": "X", "name": "Kept", "price_cents": 50}},
            "orders": {"OLD": {
                "order_id": "OLD", "status": "shipped",
                "lines": [
                    {"sku": "X", "quantity": 1, "unit_price_cents": 50, "subtotal_cents": 50},
                    {"sku": "Y", "quantity": 1, "unit_price_cents": 50, "subtotal_cents": 50},
                ],
                "total_cents": 100,
                "shipment": {"carrier": "DHL", "tracking_no": "Z"},
            }},
            "returns": {"OLD": [
                {"order_id": "OLD", "return_id": "L1", "lines": [
                    {"sku": "Y", "quantity": 1},
                    {"sku": "X", "quantity": 1},
                ]},
            ]},
        }
        self._load(data)
        entry = self.app.return_worklist()[0]
        self.assertEqual(entry["blockers"], [
            {"sku": "X", "reason": "unmanaged"},
            {"sku": "Y", "reason": "unknown-product"},
        ])
        self.assertFalse(entry["can_receive"])

    def test_paused_sales_do_not_block(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.set_product_enabled("T", False)
        entry = self.app.return_worklist()[0]
        self.assertEqual(entry["blockers"], [])
        self.assertTrue(entry["can_receive"])
        # And receiving actually still works.
        self.app.receive_return("R1")

    def test_restock_clears_unmanaged_blocker(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}, {"sku": "U", "quantity": 2}])
        self.app.ship("O1", "DHL", "X")
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}, {"sku": "U", "quantity": 2}])
        self.assertFalse(self.app.return_worklist()[0]["can_receive"])
        self.app.restock("U", 3)
        entry = self.app.return_worklist()[0]
        self.assertTrue(entry["can_receive"])
        self.assertEqual(entry["blockers"], [])

    def test_received_and_cancelled_have_no_blockers_and_cannot_receive(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}, {"sku": "U", "quantity": 2}])
        self.app.ship("O1", "DHL", "X")
        self.app.record_return("O1", "RR", [{"sku": "U", "quantity": 2}])
        self.app.record_return("O1", "RD", [{"sku": "T", "quantity": 1}])
        self.app.receive_return("RD")
        self.app.cancel_return("RR")
        entries = {e["return_id"]: e for e in self.app.return_worklist(stage="all")}
        self.assertFalse(entries["RR"]["can_receive"])
        self.assertEqual(entries["RR"]["blockers"], [])
        self.assertFalse(entries["RD"]["can_receive"])
        self.assertEqual(entries["RD"]["blockers"], [])

    def test_stage_transitions_reflected_after_operations(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.return_worklist()[0]["stage"], "pending")
        self.app.receive_return("R1")
        self.assertEqual(self.app.return_worklist(), [])
        self.assertEqual(self.app.return_worklist(stage="all")[0]["stage"], "received")
        self._shipped("O2", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O2", "R2", [{"sku": "T", "quantity": 1}])
        self.app.cancel_return("R2")
        stages = {e["return_id"]: e["stage"] for e in self.app.return_worklist(stage="all")}
        self.assertEqual(stages, {"R1": "received", "R2": "cancelled"})

    def test_legacy_missing_receipt_is_pending(self):
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
                {"order_id": "OLD", "return_id": "L1", "lines": [{"sku": "T", "quantity": 2}]},
            ]},
        }
        self._load(data)
        entry = self.app.return_worklist()[0]
        self.assertEqual(entry["stage"], "pending")
        self.assertTrue(entry["can_receive"])

    def test_legacy_missing_returns_buckets_are_empty(self):
        self.assertEqual(self.app.return_worklist(stage="all"), [])
        empty_root = Path(self.temp.name) / "empty"
        self.assertEqual(OrderDesk(empty_root).return_worklist(stage="all"), [])
        self.assertFalse(empty_root.exists())

    def test_order_missing_or_not_shipped_rejects_whole_query(self):
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "inventory": {"T": {"on_hand": 3, "reserved": 0}},
            "orders": {
                "OK": {
                    "order_id": "OK", "status": "shipped",
                    "lines": [{"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100}],
                    "total_cents": 100,
                    "shipment": {"carrier": "DHL", "tracking_no": "Z"},
                },
                "PLACED": {
                    "order_id": "PLACED", "status": "placed",
                    "lines": [{"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100}],
                    "total_cents": 100,
                },
            },
            "returns": {
                "OK": [{"order_id": "OK", "return_id": "ROK", "lines": [{"sku": "T", "quantity": 1}]}],
                "PLACED": [{"order_id": "PLACED", "return_id": "RP", "lines": [{"sku": "T", "quantity": 1}]}],
                "GONE": [{"order_id": "GONE", "return_id": "RG", "lines": [{"sku": "T", "quantity": 1}]}],
            },
            "cancelled_returns": {
                "GONE": [{"order_id": "GONE", "return_id": "RC", "lines": [{"sku": "T", "quantity": 1}]}],
            },
        }
        self._load(data)
        before = self.app.path.read_bytes()
        # A healthy record does not mask a bad legacy one: no partial results.
        with self.assertRaises(ValueError):
            self.app.return_worklist()
        with self.assertRaises(ValueError):
            self.app.return_worklist(stage="all")
        # Cancelled record under a vanished order is rejected too.
        with self.assertRaises(ValueError):
            self.app.return_worklist(stage="cancelled")
        # A known placed order queried directly with its legacy record is rejected.
        with self.assertRaises(ValueError):
            self.app.return_worklist(order_id="PLACED")
        # Filtering to the one healthy order still succeeds.
        self.assertEqual([r["return_id"] for r in self.app.return_worklist(order_id="OK")], ["ROK"])
        # Stages with no in-scope bad records succeed even if other stages rot.
        self.assertEqual([r["return_id"] for r in self.app.return_worklist(stage="received")], [])
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_known_unshipped_order_without_records_returns_empty(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.return_worklist(order_id="O1"), [])
        self.assertEqual(self.app.return_worklist(), [])

    def test_query_creates_no_files_and_changes_nothing(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        before = self.app.path.read_bytes()
        for stage in ("pending", "received", "cancelled", "all"):
            self.app.return_worklist(stage=stage)
            self.app.return_worklist(stage=stage, order_id="O1")
        self.assertEqual(self.app.path.read_bytes(), before)
        empty_root = Path(self.temp.name) / "empty"
        fresh = OrderDesk(empty_root)
        with self.assertRaises(ValueError):
            fresh.return_worklist(stage="bogus")
        with self.assertRaises(ValueError):
            fresh.return_worklist(order_id="ghost")
        self.assertFalse(empty_root.exists())

    def test_reopen_consistency_and_no_history_change(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        first = self.app.return_worklist(stage="all")
        self.assertEqual(OrderDesk(self.root).return_worklist(stage="all"), first)
        events = self.app.history("O1")["events"]
        self.app.return_worklist(stage="all")
        self.assertEqual(self.app.history("O1")["events"], events)
        self.assertEqual([(e["sequence"], e["action"]) for e in events],
                         [(1, "place"), (2, "ship"), (3, "record-return")])

    def test_cli_success_and_failure(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        payload = self.root / "q.json"
        payload.write_text(json.dumps({"stage": " all ", "order_id": " O1 "}), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "return-worklist", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual([r["return_id"] for r in result], ["R1"])
        # No input file defaults to pending across the whole root.
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "return-worklist"],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual([r["return_id"] for r in json.loads(ok.stdout)], ["R1"])
        # Array input is dispatched row by row.
        payload.write_text(json.dumps([{"stage": "pending"}, {"stage": "cancelled"}]), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "return-worklist", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout), [
            [{"order_id": "O1", "return_id": "R1", "stage": "pending",
              "lines": [{"sku": "T", "quantity": 1}], "can_receive": True, "blockers": []}],
            [],
        ])
        payload.write_text(json.dumps({"stage": "bogus"}), encoding="utf-8")
        failed = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "return-worklist", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2, failed.stdout)
        self.assertIn("stage", json.loads(failed.stderr)["error"])

    def _load(self, data):
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        self.app = OrderDesk(self.root)


if __name__ == "__main__":
    unittest.main()
