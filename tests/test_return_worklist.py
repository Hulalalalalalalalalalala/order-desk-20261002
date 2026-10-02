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
        self.app.add_product("P", "Paused", 50)
        self.app.add_product("U", "Unmanaged", 0)
        self.app.set_product_enabled("P", False)

    def _shipped(self, order_id, lines, stock=None):
        for sku, quantity in (stock or {"T": 10, "C": 10, "P": 10}).items():
            self.app.restock(sku, quantity)
        self.app.place(order_id, lines)
        self.app.ship(order_id, "DHL", "TRK-" + order_id)

    def _entry(self, result, return_id):
        matches = [item for item in result if item["return_id"] == return_id]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def test_empty_root_and_unknown_order_inputs(self):
        # A root without data.json is an empty worklist and stays absent.
        empty_root = self.root / "empty"
        fresh = OrderDesk(empty_root)
        self.assertEqual(fresh.return_worklist(), [])
        self.assertEqual(fresh.return_worklist("all"), [])
        self.assertFalse(empty_root.exists())
        for bad_stage in (None, 123, True, ["all"], {"x": 1}, "", "   ", "done", "pending\nx", " PENDING "):
            with self.assertRaises(ValueError):
                self.app.return_worklist(bad_stage)
        for bad_order in (123, True, ["O1"], {"x": 1}, "   ", "\t\n"):
            with self.assertRaises(ValueError):
                self.app.return_worklist("all", bad_order)

    def test_pending_entry_shape_and_default_stage(self):
        self._shipped("O1", [{"sku": "T", "quantity": 4}, {"sku": "C", "quantity": 2}])
        self.app.record_return("O1", "R1", [
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 1},
            {"sku": "T", "quantity": 1},
        ])
        result = self.app.return_worklist()
        self.assertEqual(result, [{
            "order_id": "O1",
            "return_id": "R1",
            "stage": "pending",
            "lines": [{"sku": "C", "quantity": 1}, {"sku": "T", "quantity": 2}],
            "can_receive": True,
            "blockers": [],
        }])
        self.assertEqual(set(result[0]), {"order_id", "return_id", "stage", "lines", "can_receive", "blockers"})
        self.assertEqual(set(result[0]["lines"][0]), {"sku", "quantity"})

    def test_stage_filter_whitespace_and_case_sensitive_order(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R3", [{"sku": "T", "quantity": 1}])
        self.app.receive_return("R2")
        self.app.cancel_return("R3")
        self.assertEqual([i["return_id"] for i in self.app.return_worklist("  pending  ")], ["R1"])
        self.assertEqual([i["return_id"] for i in self.app.return_worklist("received")], ["R2"])
        self.assertEqual([i["return_id"] for i in self.app.return_worklist("cancelled", " O1 ")], ["R3"])
        self.assertEqual([i["return_id"] for i in self.app.return_worklist(" all ")], ["R1", "R2", "R3"])
        # Unknown order is a ValueError even though data exists; ids are case sensitive.
        with self.assertRaises(ValueError):
            self.app.return_worklist("all", "o1")
        with self.assertRaises(ValueError):
            self.app.return_worklist("all", "nope")
        # Known orders without registrations, including non-shipped ones, return [].
        self.app.place("O2", [{"sku": "C", "quantity": 1}])
        self.assertEqual(self.app.return_worklist("all", "O2"), [])

    def test_sorted_by_return_id_across_orders(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self._shipped("O2", [{"sku": "C", "quantity": 5}])
        self.app.record_return("O2", "R9", [{"sku": "C", "quantity": 1}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O2", "R1", [{"sku": "C", "quantity": 1}])
        result = self.app.return_worklist("all")
        self.assertEqual([(i["return_id"], i["order_id"]) for i in result],
                         [("R1", "O2"), ("R2", "O1"), ("R9", "O2")])
        only_o1 = self.app.return_worklist("all", "O1")
        self.assertEqual([i["return_id"] for i in only_o1], ["R2"])

    def test_blockers_unknown_unmanaged_paused_and_can_receive(self):
        self.app.set_product_enabled("P", True)
        self._shipped("O1", [{"sku": "T", "quantity": 2}, {"sku": "P", "quantity": 2}])
        self.app.set_product_enabled("P", False)
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}, {"sku": "P", "quantity": 1}])
        entry = self._entry(self.app.return_worklist(), "R1")
        # Paused sales do not block receiving.
        self.assertEqual(entry["blockers"], [])
        self.assertTrue(entry["can_receive"])
        # Unmanaged product blocks.
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        raw["returns"]["O1"].append({
            "order_id": "O1", "return_id": "R3",
            "lines": [{"sku": "U", "quantity": 1}, {"sku": "X", "quantity": 1},
                      {"sku": "T", "quantity": 1}],
        })
        self.app.path.write_text(json.dumps(raw), encoding="utf-8")
        entry = self._entry(self.app.return_worklist(), "R2")
        self.assertTrue(entry["can_receive"])
        entry = self._entry(self.app.return_worklist(), "R3")
        self.assertFalse(entry["can_receive"])
        self.assertEqual(entry["blockers"], [
            {"sku": "U", "reason": "unmanaged"},
            {"sku": "X", "reason": "unknown-product"},
        ])
        self.assertEqual(set(entry["blockers"][0]), {"sku", "reason"})
        # Managing the product clears the blocker without any write from the query.
        self.app.restock("U", 3)
        entry = self._entry(self.app.return_worklist(), "R3")
        self.assertEqual(entry["blockers"], [{"sku": "X", "reason": "unknown-product"}])
        self.assertFalse(entry["can_receive"])

    def test_received_and_cancelled_stages_have_no_blockers(self):
        self._shipped("O1", [{"sku": "T", "quantity": 4}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        self.app.receive_return("R1")
        self.app.cancel_return("R2")
        all_entries = {i["return_id"]: i for i in self.app.return_worklist("all")}
        for return_id, stage in (("R1", "received"), ("R2", "cancelled")):
            entry = all_entries[return_id]
            self.assertEqual(entry["stage"], stage)
            self.assertFalse(entry["can_receive"])
            self.assertEqual(entry["blockers"], [])
            self.assertEqual(entry["lines"], [{"sku": "T", "quantity": 1}])
        # Cancelled stays visible through its filter; pending excludes it.
        self.assertEqual([i["return_id"] for i in self.app.return_worklist("pending")], [])
        self.assertEqual([i["return_id"] for i in self.app.return_worklist("cancelled")], ["R2"])

    def test_stage_reflects_register_receive_cancel_and_restock(self):
        self._shipped("O1", [{"sku": "T", "quantity": 3}])
        self.assertEqual(self.app.return_worklist("all"), [])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.assertEqual(self._entry(self.app.return_worklist("all"), "R1")["stage"], "pending")
        self.app.receive_return("R1")
        entry = self._entry(self.app.return_worklist("all"), "R1")
        self.assertEqual(entry["stage"], "received")
        self.assertFalse(entry["can_receive"])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        self.app.cancel_return("R2")
        self.assertEqual(self._entry(self.app.return_worklist("all"), "R2")["stage"], "cancelled")
        # Restock changes blockers only for pending entries.
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        raw["returns"]["O1"].append({
            "order_id": "O1", "return_id": "R3",
            "lines": [{"sku": "U", "quantity": 1}],
        })
        self.app.path.write_text(json.dumps(raw), encoding="utf-8")
        self.assertFalse(self._entry(self.app.return_worklist(), "R3")["can_receive"])
        self.app.restock("U", 2)
        self.assertTrue(self._entry(self.app.return_worklist(), "R3")["can_receive"])

    def test_non_shipped_or_missing_owner_rejects_whole_query(self):
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "inventory": {"T": {"on_hand": 3, "reserved": 0}},
            "orders": {
                "S1": {
                    "order_id": "S1", "status": "shipped",
                    "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200}],
                    "total_cents": 200, "shipment": {"carrier": "DHL", "tracking_no": "Z"},
                },
                "P1": {
                    "order_id": "P1", "status": "placed",
                    "lines": [{"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100}],
                    "total_cents": 100,
                },
            },
            "returns": {
                "S1": [{"order_id": "S1", "return_id": "ROK",
                        "lines": [{"sku": "T", "quantity": 1}]}],
                "P1": [{"order_id": "P1", "return_id": "RPLACED",
                        "lines": [{"sku": "T", "quantity": 1}]}],
                "GONE": [{"order_id": "GONE", "return_id": "RGONE",
                          "lines": [{"sku": "T", "quantity": 1}]}],
            },
            "cancelled_returns": {
                "P1": [{"order_id": "P1", "return_id": "RCPLACED",
                        "lines": [{"sku": "T", "quantity": 1}]}],
            },
        }
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        # Even a stage filter that excludes the bad registrations must reject:
        # validation runs over every registration in scope.
        for stage in ("pending", "received", "cancelled", "all"):
            with self.assertRaises(ValueError):
                app.return_worklist(stage)
        with self.assertRaises(ValueError):
            app.return_worklist("all", "P1")
        with self.assertRaises(ValueError):
            app.return_worklist("all", "GONE")
        # The healthy order is still queryable on its own.
        result = app.return_worklist("all", "S1")
        self.assertEqual([i["return_id"] for i in result], ["ROK"])

    def test_legacy_missing_buckets_and_orphan_receipts(self):
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "inventory": {"T": {"on_hand": 3, "reserved": 0}},
            "orders": {"OLD": {
                "order_id": "OLD", "status": "shipped",
                "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200}],
                "total_cents": 200, "shipment": {"carrier": "DHL", "tracking_no": "Z"},
            }},
            # An orphan receipt with no registration must not fabricate an entry.
            "return_receipts": {"GHOST": {"order_id": "OLD", "return_id": "GHOST", "lines": []}},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        for stage in ("pending", "received", "cancelled", "all"):
            self.assertEqual(app.return_worklist(stage), [])
        self.assertEqual(app.return_worklist("all", "OLD"), [])

    def test_legacy_registration_without_receipt_is_pending(self):
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "inventory": {"T": {"on_hand": 3, "reserved": 0}},
            "orders": {"OLD": {
                "order_id": "OLD", "status": "shipped",
                "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200}],
                "total_cents": 200, "shipment": {"carrier": "DHL", "tracking_no": "Z"},
            }},
            "returns": {"OLD": [
                {"order_id": "OLD", "return_id": "L1",
                 "lines": [{"sku": "T", "quantity": 1}, {"sku": "T", "quantity": 1}]},
            ]},
            "cancelled_returns": {"OLD": [
                {"order_id": "OLD", "return_id": "L2", "lines": [{"sku": "T", "quantity": 1}]},
            ]},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        pending = app.return_worklist("pending")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["return_id"], "L1")
        self.assertEqual(pending[0]["lines"], [{"sku": "T", "quantity": 2}])
        self.assertTrue(pending[0]["can_receive"])
        cancelled = app.return_worklist("cancelled")
        self.assertEqual(cancelled[0]["return_id"], "L2")
        self.assertFalse(cancelled[0]["can_receive"])

    def test_query_is_read_only_and_stable_across_reopen(self):
        self._shipped("O1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        before = self.app.path.read_bytes()
        for stage in ("pending", "received", "cancelled", "all"):
            self.app.return_worklist(stage)
            self.app.return_worklist(stage, "O1")
        self.assertEqual(self.app.path.read_bytes(), before)
        first = OrderDesk(self.root).return_worklist("all")
        second = OrderDesk(self.root).return_worklist("all")
        self.assertEqual(first, second)
        # Failure must not create anything either.
        missing_root = self.root / "missing"
        fresh = OrderDesk(missing_root)
        with self.assertRaises(ValueError):
            fresh.return_worklist("bogus")
        self.assertFalse(missing_root.exists())

    def test_cli_success_json_and_failure_error_json(self):
        self._shipped("O1", [{"sku": "T", "quantity": 3}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        payload = self.root / "q.json"
        payload.write_text(json.dumps({"stage": " all ", "order_id": " O1 "}), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "return-worklist", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual([i["return_id"] for i in result], ["R1"])
        # No input file: defaults still work.
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "return-worklist"],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout)[0]["return_id"], "R1")
        bad = self.root / "bad.json"
        bad.write_text(json.dumps({"stage": "nope"}), encoding="utf-8")
        failed = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "return-worklist", str(bad)],
            text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2, failed.stdout)
        self.assertIn("invalid stage", json.loads(failed.stderr)["error"])
        bad.write_text(json.dumps({"order_id": "ghost"}), encoding="utf-8")
        failed = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "return-worklist", str(bad)],
            text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2, failed.stdout)
        self.assertIn("unknown order", json.loads(failed.stderr)["error"])


if __name__ == "__main__":
    unittest.main()
