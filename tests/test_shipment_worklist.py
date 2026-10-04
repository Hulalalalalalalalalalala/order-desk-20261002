import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk


class ShipmentWorklistTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)

    def _ship_pair(self):
        # Two orders shipped together under DHL/TRK-1.
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        self.app.ship("O1", "DHL", "TRK-1")
        self.app.ship("O2", "DHL", "TRK-1")

    def test_empty_root_returns_empty_and_creates_nothing(self):
        fresh = Path(self.temp.name) / "fresh"
        app = OrderDesk(fresh)
        self.assertEqual(app.shipment_worklist(), [])
        self.assertEqual(app.shipment_worklist(stage="all"), [])
        self.assertEqual(app.shipment_worklist(stage="conflict"), [])
        self.assertFalse((fresh / "data.json").exists())

    def test_legacy_data_without_orders_returns_empty(self):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "data.json").write_text(json.dumps({"products": {}}), encoding="utf-8")
        self.assertEqual(self.app.shipment_worklist(), [])
        self.assertEqual(self.app.shipment_worklist(stage="all"), [])

    def test_grouping_and_sorting_uses_current_shipment(self):
        self._ship_pair()
        self.app.place("O3", [{"sku": "T", "quantity": 1}])
        self.app.ship("O3", "UPS", "TRK-9")
        self.app.place("O4", [{"sku": "T", "quantity": 1}])
        self.app.ship("O4", "DHL", "TRK-2")
        result = self.app.shipment_worklist(stage="all")
        self.assertEqual([(g["carrier"], g["tracking_no"]) for g in result],
                         [("DHL", "TRK-1"), ("DHL", "TRK-2"), ("UPS", "TRK-9")])
        first = result[0]
        self.assertEqual(set(first), {"carrier", "tracking_no", "pending_ids",
                                      "deliveries", "invalid_ids", "conflict"})
        self.assertEqual(first["pending_ids"], ["O1", "O2"])
        self.assertEqual(first["deliveries"], [])
        self.assertEqual(first["invalid_ids"], [])
        self.assertFalse(first["conflict"])

    def test_shipment_fields_are_trimmed_and_case_sensitive(self):
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        # Stored values keep surrounding whitespace, grouping normalizes them.
        self.app.ship("O1", "  DHL ", " TRK-1 ")
        self.app.ship("O2", "DHL", "TRK-1")
        result = self.app.shipment_worklist(stage="all")
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0]["carrier"], result[0]["tracking_no"]), ("DHL", "TRK-1"))
        self.assertEqual(result[0]["pending_ids"], ["O1", "O2"])
        # Case-sensitive: dhl and DHL are different carriers.
        self.app.place("O3", [{"sku": "T", "quantity": 1}])
        self.app.ship("O3", "dhl", "TRK-1")
        result = self.app.shipment_worklist(stage="all")
        self.assertEqual([g["carrier"] for g in result], ["DHL", "dhl"])

    def test_invalid_shipment_info_is_skipped(self):
        self._ship_pair()
        self.app.place("O3", [{"sku": "T", "quantity": 1}])
        self.app.ship("O3", "UPS", "TRK-9")
        data = json.loads((self.root / "data.json").read_text(encoding="utf-8"))
        # Mutate the third order's shipment into every shape shipment-orders
        # skips; none of them form a group or fail the query.
        data["orders"]["O3"]["shipment"] = {"carrier": "  ", "tracking_no": "TRK-9"}
        (self.root / "data.json").write_text(json.dumps(data), encoding="utf-8")
        result = self.app.shipment_worklist(stage="all")
        self.assertEqual([(g["carrier"], g["tracking_no"]) for g in result], [("DHL", "TRK-1")])
        data = json.loads((self.root / "data.json").read_text(encoding="utf-8"))
        data["orders"]["O3"]["shipment"] = "UPS TRK-9"
        (self.root / "data.json").write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual([g["tracking_no"] for g in self.app.shipment_worklist(stage="all")], ["TRK-1"])

    def test_same_shipment_same_combo_one_pending_single_delivery(self):
        self._ship_pair()
        self.app.confirm_delivery("O1", "Ann", "2026-10-01")
        result = self.app.shipment_worklist()
        self.assertEqual(len(result), 1)
        group = result[0]
        self.assertEqual(group["pending_ids"], ["O2"])
        self.assertEqual(len(group["deliveries"]), 1)
        delivery = group["deliveries"][0]
        self.assertEqual(set(delivery), {"recipient", "delivered_on", "order_ids"})
        self.assertEqual(delivery, {"recipient": "Ann", "delivered_on": "2026-10-01",
                                    "order_ids": ["O1"]})
        self.assertEqual(group["invalid_ids"], [])
        self.assertFalse(group["conflict"])

    def test_extra_delivery_fields_ignored_and_recipient_trimmed(self):
        self._ship_pair()
        self.app.confirm_delivery("O1", " Ann ", "2026-10-01")
        data = json.loads((self.root / "data.json").read_text(encoding="utf-8"))
        data["orders"]["O1"]["delivery"]["note"] = "left at door"
        (self.root / "data.json").write_text(json.dumps(data), encoding="utf-8")
        group = self.app.shipment_worklist(stage="all")[0]
        self.assertEqual(group["deliveries"][0]["recipient"], "Ann")
        self.assertEqual(set(group["deliveries"][0]), {"recipient", "delivered_on", "order_ids"})

    def test_consistent_signoff_exits_open_but_stays_in_all(self):
        self._ship_pair()
        self.app.confirm_shipment_delivery("DHL", "TRK-1", "Ann", "2026-10-01")
        self.assertEqual(self.app.shipment_worklist(), [])
        result = self.app.shipment_worklist(stage="all")
        self.assertEqual(len(result), 1)
        group = result[0]
        self.assertEqual(group["pending_ids"], [])
        self.assertEqual(len(group["deliveries"]), 1)
        self.assertEqual(group["deliveries"][0]["order_ids"], ["O1", "O2"])
        self.assertFalse(group["conflict"])

    def test_different_dates_mark_conflict(self):
        self._ship_pair()
        self.app.confirm_delivery("O1", "Ann", "2026-10-01")
        self.app.confirm_delivery("O2", "Ann", "2026-10-02")
        for stage in ("open", "all", "conflict"):
            result = self.app.shipment_worklist(stage=stage)
            self.assertEqual(len(result), 1, stage)
            group = result[0]
            self.assertTrue(group["conflict"])
            self.assertEqual([d["delivered_on"] for d in group["deliveries"]],
                             ["2026-10-01", "2026-10-02"])
            self.assertEqual(group["deliveries"][0]["order_ids"], ["O1"])
            self.assertEqual(group["deliveries"][1]["order_ids"], ["O2"])

    def test_deliveries_sorted_by_recipient_then_date(self):
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        self.app.place("O3", [{"sku": "T", "quantity": 1}])
        self.app.ship_batch([
            {"order_id": "O1", "carrier": "DHL", "tracking_no": "TRK-1"},
            {"order_id": "O2", "carrier": "DHL", "tracking_no": "TRK-1"},
            {"order_id": "O3", "carrier": "DHL", "tracking_no": "TRK-1"},
        ])
        self.app.confirm_delivery("O1", "Bob", "2026-10-03")
        self.app.confirm_delivery("O2", "Ann", "2026-10-02")
        self.app.confirm_delivery("O3", "Ann", "2026-10-01")
        group = self.app.shipment_worklist(stage="conflict")[0]
        self.assertEqual([(d["recipient"], d["delivered_on"]) for d in group["deliveries"]],
                         [("Ann", "2026-10-01"), ("Ann", "2026-10-02"), ("Bob", "2026-10-03")])
        self.assertEqual(group["deliveries"][0]["order_ids"], ["O3"])

    def test_invalid_delivered_records_go_to_invalid_ids(self):
        self._ship_pair()
        self.app.confirm_delivery("O1", "Ann", "2026-10-01")
        data = json.loads((self.root / "data.json").read_text(encoding="utf-8"))
        # O2 stays delivered with a malformed delivery in several shapes.
        data["orders"]["O2"]["status"] = "delivered"
        data["orders"]["O2"]["delivery"] = None
        (self.root / "data.json").write_text(json.dumps(data), encoding="utf-8")
        group = self.app.shipment_worklist(stage="all")[0]
        self.assertEqual(group["invalid_ids"], ["O2"])
        self.assertTrue(group["conflict"])
        self.assertEqual([d["order_ids"] for d in group["deliveries"]], [["O1"]])
        # The malformed group still shows up in open and conflict, and the
        # query succeeds rather than raising.
        self.assertEqual(self.app.shipment_worklist(stage="conflict")[0]["invalid_ids"], ["O2"])
        self.assertEqual(self.app.shipment_worklist()[0]["invalid_ids"], ["O2"])
        for bad in (
            "not-an-object",
            {"recipient": "Ann"},
            {"delivered_on": "2026-10-01"},
            {"recipient": "   ", "delivered_on": "2026-10-01"},
            {"recipient": 5, "delivered_on": "2026-10-01"},
            {"recipient": "Ann", "delivered_on": "2026-02-29"},
            {"recipient": "Ann", "delivered_on": "2026-10-1"},
            {"recipient": "Ann", "delivered_on": None},
        ):
            data["orders"]["O2"]["delivery"] = bad
            (self.root / "data.json").write_text(json.dumps(data), encoding="utf-8")
            group = self.app.shipment_worklist(stage="all")[0]
            self.assertEqual(group["invalid_ids"], ["O2"], bad)
            self.assertEqual(group["pending_ids"], [])
            self.assertTrue(group["conflict"], bad)

    def test_leftover_delivery_on_shipped_order_is_ignored(self):
        self._ship_pair()
        data = json.loads((self.root / "data.json").read_text(encoding="utf-8"))
        # A shipped order carrying a stray delivery never contributes one.
        data["orders"]["O2"]["delivery"] = {"recipient": "Zoe", "delivered_on": "2026-09-01"}
        (self.root / "data.json").write_text(json.dumps(data), encoding="utf-8")
        group = self.app.shipment_worklist()[0]
        self.assertEqual(group["pending_ids"], ["O1", "O2"])
        self.assertEqual(group["deliveries"], [])
        self.assertFalse(group["conflict"])

    def test_conflict_filters_out_consistent_groups(self):
        self._ship_pair()
        self.app.confirm_shipment_delivery("DHL", "TRK-1", "Ann", "2026-10-01")
        self.app.place("O3", [{"sku": "T", "quantity": 1}])
        self.app.place("O4", [{"sku": "T", "quantity": 1}])
        self.app.ship("O3", "UPS", "TRK-9")
        self.app.ship("O4", "UPS", "TRK-9")
        self.app.confirm_delivery("O3", "Bob", "2026-10-01")
        self.app.confirm_delivery("O4", "Bob", "2026-10-02")
        conflict = self.app.shipment_worklist(stage="conflict")
        self.assertEqual([g["tracking_no"] for g in conflict], ["TRK-9"])
        opened = self.app.shipment_worklist()
        self.assertEqual([g["tracking_no"] for g in opened], ["TRK-9"])

    def test_correcting_shipment_moves_order_to_new_group(self):
        self._ship_pair()
        self.app.confirm_delivery("O1", "Ann", "2026-10-01")
        self.app.correct_shipment(
            "O2", {"carrier": "DHL", "tracking_no": "TRK-1"},
            {"carrier": "UPS", "tracking_no": "TRK-9"},
        )
        result = self.app.shipment_worklist(stage="all")
        self.assertEqual([(g["carrier"], g["tracking_no"]) for g in result],
                         [("DHL", "TRK-1"), ("UPS", "TRK-9")])
        old, new = result
        self.assertEqual(old["pending_ids"], [])
        self.assertEqual([d["order_ids"] for d in old["deliveries"]], [["O1"]])
        self.assertEqual(new["pending_ids"], ["O2"])

    def test_history_snapshots_never_participate(self):
        self._ship_pair()
        # Correct both orders away from DHL/TRK-1; the historical ship
        # snapshots must not fabricate the old group.
        self.app.correct_shipment(
            "O1", {"carrier": "DHL", "tracking_no": "TRK-1"},
            {"carrier": "UPS", "tracking_no": "TRK-9"},
        )
        self.app.correct_shipment(
            "O2", {"carrier": "DHL", "tracking_no": "TRK-1"},
            {"carrier": "UPS", "tracking_no": "TRK-9"},
        )
        result = self.app.shipment_worklist(stage="all")
        self.assertEqual([(g["carrier"], g["tracking_no"]) for g in result], [("UPS", "TRK-9")])

    def test_paused_missing_catalog_and_returns_do_not_block(self):
        self._ship_pair()
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.set_product_enabled("T", False)
        data = json.loads((self.root / "data.json").read_text(encoding="utf-8"))
        del data["products"]["T"]
        (self.root / "data.json").write_text(json.dumps(data), encoding="utf-8")
        result = self.app.shipment_worklist()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["pending_ids"], ["O1", "O2"])
        # A delivered order keeps grouping even with an open return.
        data = json.loads((self.root / "data.json").read_text(encoding="utf-8"))
        data["products"]["T"] = {"sku": "T", "name": "Tea", "price_cents": 100}
        (self.root / "data.json").write_text(json.dumps(data), encoding="utf-8")
        self.app.confirm_delivery("O1", "Ann", "2026-10-01")
        group = self.app.shipment_worklist(stage="all")[0]
        self.assertEqual(group["pending_ids"], ["O2"])
        self.assertEqual([d["order_ids"] for d in group["deliveries"]], [["O1"]])

    def test_other_statuses_never_group(self):
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        self.app.cancel("O2")
        self.assertEqual(self.app.shipment_worklist(stage="all"), [])

    def test_stage_whitespace_trimmed(self):
        self.assertEqual(self.app.shipment_worklist(stage=" open "),
                         self.app.shipment_worklist())
        self.assertEqual(self.app.shipment_worklist(stage="\tall\n"),
                         self.app.shipment_worklist(stage="all"))
        self.assertEqual(self.app.shipment_worklist(stage=" conflict "),
                         self.app.shipment_worklist(stage="conflict"))

    def test_invalid_stage(self):
        for bad in (None, 123, 1.5, b"open", ["open"], {"stage": 1}, True,
                    "", "   ", "pending", "OPEN", " openx", "conflicts", "Open"):
            with self.assertRaises(ValueError):
                self.app.shipment_worklist(stage=bad)

    def test_query_is_read_only_and_repeatable(self):
        self._ship_pair()
        self.app.confirm_delivery("O1", "Ann", "2026-10-01")
        before = (self.root / "data.json").read_bytes()
        first = self.app.shipment_worklist(stage="all")
        self.assertEqual((self.root / "data.json").read_bytes(), before)
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.shipment_worklist(stage="all"), first)

    def test_cli_shipment_worklist_success_and_failure(self):
        self._ship_pair()
        payload = self.root / "p.json"
        payload.write_text(json.dumps({"stage": "all"}), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "shipment-worklist", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual([(g["carrier"], g["tracking_no"]) for g in result], [("DHL", "TRK-1")])
        self.assertEqual(result[0]["pending_ids"], ["O1", "O2"])
        # Default stage (no input file) is open.
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "shipment-worklist"],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout)[0]["pending_ids"], ["O1", "O2"])
        payload.write_text(json.dumps({"stage": "bogus"}), encoding="utf-8")
        failed = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "shipment-worklist", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2, failed.stdout)
        self.assertIn("error", json.loads(failed.stderr))

    def test_cli_array_executes_each_item(self):
        self._ship_pair()
        self.app.confirm_delivery("O1", "Ann", "2026-10-01")
        self.app.confirm_delivery("O2", "Ann", "2026-10-02")
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([{"stage": "conflict"}, {"stage": "all"}]),
                        encoding="utf-8")
        run = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "shipment-worklist", str(batch)],
            text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        results = json.loads(run.stdout)
        self.assertEqual(len(results), 2)
        self.assertEqual(len(results[0]), 1)
        self.assertTrue(results[0][0]["conflict"])
        self.assertEqual(len(results[1]), 1)


if __name__ == "__main__":
    unittest.main()
