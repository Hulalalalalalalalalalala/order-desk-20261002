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
        self.app.add_product("U", "Unrestocked", 50)

    def _ship(self, order_id, lines, carrier="DHL", tracking_no="X-1"):
        self.app.place(order_id, lines)
        self.app.ship(order_id, carrier, tracking_no)

    def _deliver(self, order_id, recipient="Ann", delivered_on="2026-10-01"):
        self.app.confirm_delivery(order_id, recipient, delivered_on)

    def _by_tracking(self, result):
        return {group["tracking_no"]: group for group in result}

    def test_empty_root_returns_empty_and_creates_nothing(self):
        fresh = self.root / "fresh"
        app = OrderDesk(fresh)
        self.assertEqual(app.shipment_worklist(), [])
        for stage in ("open", "all", "conflict"):
            self.assertEqual(app.shipment_worklist(stage=stage), [])
        self.assertFalse(fresh.exists())

    def test_legacy_data_without_orders_returns_empty(self):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "data.json").write_text(json.dumps({"products": {}}), encoding="utf-8")
        self.assertEqual(self.app.shipment_worklist(), [])
        self.assertEqual(self.app.shipment_worklist(stage="all"), [])

    def test_group_shape_and_sorting(self):
        self.app.restock("T", 10)
        self._ship("O1", [{"sku": "T", "quantity": 1}], "UPS", "Z-9")
        self._ship("O2", [{"sku": "T", "quantity": 1}], "DHL", "X-1")
        result = self.app.shipment_worklist(stage="all")
        self.assertEqual([(g["carrier"], g["tracking_no"]) for g in result],
                         [("DHL", "X-1"), ("UPS", "Z-9")])
        for group in result:
            self.assertEqual(set(group),
                             {"carrier", "tracking_no", "pending_ids", "deliveries",
                              "invalid_ids", "conflict"})
        group = result[0]
        self.assertEqual(group["pending_ids"], ["O2"])
        self.assertEqual(group["deliveries"], [])
        self.assertEqual(group["invalid_ids"], [])
        self.assertFalse(group["conflict"])

    def test_open_keeps_pending_group_and_consistent_group_leaves_open(self):
        self.app.restock("T", 10)
        self._ship("O1", [{"sku": "T", "quantity": 1}], "DHL", "SAME")
        self._ship("O2", [{"sku": "T", "quantity": 1}], "DHL", "SAME")
        self._deliver("O1")
        # Two orders same recipient/date, one unsigned: one valid combination,
        # still open because O2 is pending.
        open_result = self.app.shipment_worklist()
        self.assertEqual(len(open_result), 1)
        group = open_result[0]
        self.assertEqual(group["pending_ids"], ["O2"])
        self.assertEqual(group["deliveries"],
                         [{"recipient": "Ann", "delivered_on": "2026-10-01",
                           "order_ids": ["O1"]}])
        self.assertFalse(group["conflict"])
        # Sign the rest with the same info: group leaves open but stays in all.
        self.app.confirm_shipment_delivery("DHL", "SAME", "Ann", "2026-10-01")
        self.assertEqual(self.app.shipment_worklist(), [])
        all_result = self.app.shipment_worklist(stage="all")
        self.assertEqual(len(all_result), 1)
        finished = all_result[0]
        self.assertEqual(finished["pending_ids"], [])
        self.assertEqual(finished["invalid_ids"], [])
        self.assertEqual(finished["deliveries"],
                         [{"recipient": "Ann", "delivered_on": "2026-10-01",
                           "order_ids": ["O1", "O2"]}])
        self.assertFalse(finished["conflict"])
        self.assertEqual(self.app.shipment_worklist(stage="conflict"), [])

    def test_different_dates_conflict_and_same_recipient_merges(self):
        self.app.restock("T", 10)
        self._ship("O1", [{"sku": "T", "quantity": 1}], "DHL", "SAME")
        self._ship("O2", [{"sku": "T", "quantity": 1}], "DHL", "SAME")
        self._ship("O3", [{"sku": "T", "quantity": 1}], "DHL", "SAME")
        self._deliver("O1", "Ann", "2026-10-01")
        self._deliver("O2", "Ann", "2026-10-02")
        # O3 stays shipped: pending plus two distinct dates.
        result = self.app.shipment_worklist()
        self.assertEqual(len(result), 1)
        group = result[0]
        self.assertTrue(group["conflict"])
        self.assertEqual(group["pending_ids"], ["O3"])
        self.assertEqual(group["deliveries"], [
            {"recipient": "Ann", "delivered_on": "2026-10-01", "order_ids": ["O1"]},
            {"recipient": "Ann", "delivered_on": "2026-10-02", "order_ids": ["O2"]},
        ])
        self.assertEqual(group["invalid_ids"], [])
        # conflict stage returns the same group.
        self.assertEqual(self.app.shipment_worklist(stage="conflict")[0]["tracking_no"], "SAME")

    def test_different_recipients_conflict_sorted_by_recipient_then_date(self):
        self.app.restock("T", 10)
        self._ship("O1", [{"sku": "T", "quantity": 1}], "DHL", "SAME")
        self._ship("O2", [{"sku": "T", "quantity": 1}], "DHL", "SAME")
        self._deliver("O1", "Bob", "2026-10-01")
        self._deliver("O2", "Ann", "2026-10-01")
        group = self.app.shipment_worklist(stage="all")[0]
        self.assertTrue(group["conflict"])
        self.assertEqual([d["recipient"] for d in group["deliveries"]], ["Ann", "Bob"])

    def test_same_recipient_and_date_merges_into_one_delivery(self):
        self.app.restock("T", 10)
        self._ship("B", [{"sku": "T", "quantity": 1}], "DHL", "SAME")
        self._ship("A", [{"sku": "T", "quantity": 1}], "DHL", "SAME")
        self._deliver("B", " Ann ", "2026-10-01")
        self._deliver("A", "Ann", " 2026-10-01 ")
        group = self.app.shipment_worklist(stage="all")[0]
        self.assertEqual(group["deliveries"],
                         [{"recipient": "Ann", "delivered_on": "2026-10-01",
                           "order_ids": ["A", "B"]}])
        self.assertFalse(group["conflict"])

    def test_invalid_deliveries_go_to_invalid_ids_and_conflict(self):
        self.app.restock("T", 20)
        self._ship("GOOD", [{"sku": "T", "quantity": 1}], "DHL", "SAME")
        self._deliver("GOOD", "Ann", "2026-10-01")
        base = {
            "lines": [{"sku": "T", "quantity": 1, "unit_price_cents": 100,
                       "subtotal_cents": 100}],
            "total_cents": 100,
            "shipment": {"carrier": "DHL", "tracking_no": "SAME"},
        }
        MISSING = object()
        variants = [
            MISSING,
            None,
            "signed",
            {},
            {"recipient": "Ann"},
            {"delivered_on": "2026-10-01"},
            {"recipient": "   ", "delivered_on": "2026-10-01"},
            {"recipient": "Ann", "delivered_on": None},
            {"recipient": "Ann", "delivered_on": "2026-02-29"},
            {"recipient": "Ann", "delivered_on": "10/01/2026"},
            {"recipient": 123, "delivered_on": "2026-10-01"},
        ]
        for index, delivery in enumerate(variants):
            order = {"order_id": "BAD%02d" % index, "status": "delivered", **base}
            order = json.loads(json.dumps(order))
            if delivery is not MISSING:
                order["delivery"] = delivery
            data = json.loads(self.app.path.read_text(encoding="utf-8"))
            data["orders"][order["order_id"]] = order
            self._write_raw(data)
        result = OrderDesk(self.root).shipment_worklist(stage="all")
        self.assertEqual(len(result), 1)
        group = result[0]
        self.assertTrue(group["conflict"])
        self.assertEqual(group["invalid_ids"], ["BAD%02d" % i for i in range(len(variants))])
        self.assertEqual(group["deliveries"],
                         [{"recipient": "Ann", "delivered_on": "2026-10-01",
                           "order_ids": ["GOOD"]}])
        # Invalid sign-offs alone make the group appear under open and conflict
        # even when every order is delivered (no pending ids).
        self.assertEqual(self.app.shipment_worklist(stage="conflict"),
                         self.app.shipment_worklist(stage="open"))

    def test_extra_delivery_fields_are_ignored(self):
        self.app.restock("T", 10)
        self._ship("O1", [{"sku": "T", "quantity": 1}], "DHL", "X-1")
        self._deliver("O1", "Ann", "2026-10-01")
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        data["orders"]["O1"]["delivery"]["note"] = "left at door"
        self._write_raw(data)
        group = OrderDesk(self.root).shipment_worklist(stage="all")[0]
        self.assertEqual(group["deliveries"][0],
                         {"recipient": "Ann", "delivered_on": "2026-10-01", "order_ids": ["O1"]})
        self.assertEqual(group["invalid_ids"], [])

    def test_leftover_delivery_on_shipped_order_is_ignored(self):
        self.app.restock("T", 10)
        self._ship("O1", [{"sku": "T", "quantity": 1}], "DHL", "X-1")
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        data["orders"]["O1"]["delivery"] = {"recipient": "Ghost", "delivered_on": "2026-02-30"}
        self._write_raw(data)
        group = OrderDesk(self.root).shipment_worklist(stage="all")[0]
        self.assertEqual(group["pending_ids"], ["O1"])
        self.assertEqual(group["deliveries"], [])
        self.assertEqual(group["invalid_ids"], [])
        self.assertFalse(group["conflict"])

    def test_invalid_shipments_follow_shipment_orders_skip_rule(self):
        self.app.restock("T", 20)
        self._ship("GOOD", [{"sku": "T", "quantity": 1}], "DHL", "X-1")
        base = {"order_id": "BAD", "status": "shipped",
                "lines": [{"sku": "T", "quantity": 1, "unit_price_cents": 100,
                           "subtotal_cents": 100}],
                "total_cents": 100}
        variants = [
            None,
            "DHL X-1",
            {},
            {"carrier": "DHL"},
            {"tracking_no": "X-1"},
            {"carrier": 5, "tracking_no": "X-1"},
            {"carrier": "DHL", "tracking_no": None},
            {"carrier": "   ", "tracking_no": "X-1"},
            {"carrier": "DHL", "tracking_no": "\t"},
        ]
        for index, shipment in enumerate(variants):
            order = dict(base)
            order["order_id"] = "BAD" + str(index)
            if shipment is not None:
                order["shipment"] = shipment
            data = json.loads(self.app.path.read_text(encoding="utf-8"))
            data["orders"][order["order_id"]] = order
            self._write_raw(data)
        result = OrderDesk(self.root).shipment_worklist(stage="all")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["carrier"], "DHL")
        self.assertEqual(result[0]["tracking_no"], "X-1")
        self.assertEqual(result[0]["pending_ids"], ["GOOD"])

    def test_grouping_uses_current_shipment_trimmed_case_sensitive(self):
        self.app.restock("T", 20)
        self._ship("O1", [{"sku": "T", "quantity": 1}], "  DHL  ", " X-1 ")
        self._ship("O2", [{"sku": "T", "quantity": 1}], "DHL", "x-1")
        self._ship("O3", [{"sku": "T", "quantity": 1}], "UPS", "X-1")
        result = self.app.shipment_worklist(stage="all")
        groups = {(g["carrier"], g["tracking_no"]): g for g in result}
        self.assertEqual(set(groups), {("DHL", "X-1"), ("DHL", "x-1"), ("UPS", "X-1")})
        self.assertEqual(groups[("DHL", "X-1")]["pending_ids"], ["O1"])
        self.assertEqual(groups[("DHL", "x-1")]["pending_ids"], ["O2"])

    def test_only_shipped_or_delivered_orders_qualify(self):
        self.app.restock("T", 10)
        self.app.place("P1", [{"sku": "T", "quantity": 1}])
        self.app.place("P2", [{"sku": "T", "quantity": 1}])
        self.app.cancel("P2")
        # Legacy placed/cancelled orders carrying a shipment-shaped field.
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        data["orders"]["P1"]["shipment"] = {"carrier": "DHL", "tracking_no": "X-1"}
        data["orders"]["P2"]["shipment"] = {"carrier": "DHL", "tracking_no": "X-1"}
        self._write_raw(data)
        self.assertEqual(self.app.shipment_worklist(stage="all"), [])

    def test_history_snapshots_never_participate(self):
        self.app.restock("T", 10)
        self._ship("O1", [{"sku": "T", "quantity": 1}], "DHL", "OLD-1")
        self.app.correct_shipment(
            "O1", {"carrier": "DHL", "tracking_no": "OLD-1"},
            {"carrier": "UPS", "tracking_no": "NEW-2"})
        groups = {(g["carrier"], g["tracking_no"]) for g in
                  self.app.shipment_worklist(stage="all")}
        self.assertEqual(groups, {("UPS", "NEW-2")})

    def test_shipment_correction_moves_order_to_new_group(self):
        self.app.restock("T", 10)
        self._ship("O1", [{"sku": "T", "quantity": 1}], "DHL", "OLD")
        self._ship("O2", [{"sku": "T", "quantity": 1}], "DHL", "NEW")
        self.app.correct_shipment(
            "O1", {"carrier": "DHL", "tracking_no": "OLD"},
            {"carrier": "DHL", "tracking_no": "NEW"})
        result = self.app.shipment_worklist(stage="all")
        self.assertEqual([g["tracking_no"] for g in result], ["NEW"])
        self.assertEqual(result[0]["pending_ids"], ["O1", "O2"])

    def test_conflict_stage_excludes_consistent_and_pending_only_groups(self):
        self.app.restock("T", 20)
        self._ship("PEND", [{"sku": "T", "quantity": 1}], "DHL", "P")
        self._ship("OK", [{"sku": "T", "quantity": 1}], "DHL", "OK")
        self._deliver("OK", "Ann", "2026-10-01")
        self._ship("C1", [{"sku": "T", "quantity": 1}], "DHL", "C")
        self._ship("C2", [{"sku": "T", "quantity": 1}], "DHL", "C")
        self._deliver("C1", "Ann", "2026-10-01")
        self._deliver("C2", "Bob", "2026-10-01")
        result = self.app.shipment_worklist(stage="conflict")
        self.assertEqual([g["tracking_no"] for g in result], ["C"])

    def test_open_includes_conflict_group_even_without_pending(self):
        self.app.restock("T", 10)
        self._ship("O1", [{"sku": "T", "quantity": 1}], "DHL", "SAME")
        self._ship("O2", [{"sku": "T", "quantity": 1}], "DHL", "SAME")
        self._deliver("O1", "Ann", "2026-10-01")
        self._deliver("O2", "Bob", "2026-10-01")
        result = self.app.shipment_worklist()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["pending_ids"], [])
        self.assertTrue(result[0]["conflict"])
        # Resolving the conflict (one distinct combination left) leaves open.
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        data["orders"]["O2"]["delivery"] = {"recipient": "Ann", "delivered_on": "2026-10-01"}
        self._write_raw(data)
        self.assertEqual(OrderDesk(self.root).shipment_worklist(), [])

    def test_paused_missing_catalog_and_returns_do_not_block(self):
        self.app.restock("T", 5)
        self._ship("O1", [{"sku": "T", "quantity": 1}, {"sku": "U", "quantity": 2}],
                   "DHL", "SAME")
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.set_product_enabled("T", False)
        result = self.app.shipment_worklist(stage="all")
        self.assertEqual([g["tracking_no"] for g in result], ["SAME"])
        order = {"order_id": "OLD", "status": "delivered",
                 "lines": [{"sku": "GONE", "quantity": 7, "unit_price_cents": 10,
                            "subtotal_cents": 70}],
                 "total_cents": 70,
                 "shipment": {"carrier": "DHL", "tracking_no": "SAME"},
                 "delivery": {"recipient": "Ann", "delivered_on": "2026-10-01"}}
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        data["orders"]["OLD"] = order
        self._write_raw(data)
        result = OrderDesk(self.root).shipment_worklist(stage="all")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["pending_ids"], ["O1"])
        self.assertEqual(result[0]["deliveries"][0]["order_ids"], ["OLD"])

    def test_stage_whitespace_trimmed_and_default(self):
        self.app.restock("T", 10)
        self._ship("O1", [{"sku": "T", "quantity": 1}], "DHL", "X-1")
        self.assertEqual(self.app.shipment_worklist(stage=" open "),
                         self.app.shipment_worklist())
        self.assertEqual(self.app.shipment_worklist(stage="\tall\n"),
                         self.app.shipment_worklist(stage="all"))
        self.assertEqual(self.app.shipment_worklist(stage=" conflict "),
                         self.app.shipment_worklist(stage="conflict"))

    def test_invalid_stage(self):
        for bad in (None, 123, 1.5, b"open", ["open"], {"stage": 1}, True,
                    "", "   ", "pending", "OPEN", " openx", "deliver"):
            with self.assertRaises(ValueError):
                self.app.shipment_worklist(stage=bad)

    def test_query_is_read_only_repeatable_and_uses_no_sequences(self):
        self.app.restock("T", 10)
        self._ship("O1", [{"sku": "T", "quantity": 1}], "DHL", "SAME")
        self._ship("O2", [{"sku": "T", "quantity": 1}], "DHL", "SAME")
        self._deliver("O1", "Ann", "2026-10-01")
        before = self.app.path.read_bytes()
        first = self.app.shipment_worklist(stage="all")
        self.assertEqual(self.app.path.read_bytes(), before)
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.shipment_worklist(stage="all"), first)
        # The read-only query appends no events: place, ship, deliver only.
        self.assertEqual([e["sequence"] for e in self.app.history("O1")["events"]], [1, 2, 3])
        self.assertEqual([e["sequence"] for e in self.app.history("O2")["events"]], [1, 2])

    def test_invalid_query_creates_no_directory(self):
        empty = self.root / "empty"
        app = OrderDesk(empty)
        with self.assertRaises(ValueError):
            app.shipment_worklist(stage=None)
        with self.assertRaises(ValueError):
            app.shipment_worklist(stage="bogus")
        self.assertFalse(empty.exists())

    def test_cli_success_failure_and_array(self):
        self.app.restock("T", 20)
        self._ship("A", [{"sku": "T", "quantity": 1}], "DHL", "SAME")
        self._ship("B", [{"sku": "T", "quantity": 1}], "UPS", "OTHER")
        self._deliver("B", "Ann", "2026-10-01")
        payload = self.root / "p.json"
        payload.write_text(json.dumps({"stage": "all"}), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "shipment-worklist", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        # Sorted by carrier then tracking number: DHL/SAME before UPS/OTHER.
        self.assertEqual([(g["carrier"], g["tracking_no"]) for g in result],
                         [("DHL", "SAME"), ("UPS", "OTHER")])
        # Default stage (no input file) is open: only the pending DHL group.
        default_run = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "shipment-worklist"],
            text=True, capture_output=True)
        self.assertEqual(default_run.returncode, 0, default_run.stderr)
        self.assertEqual([g["tracking_no"] for g in json.loads(default_run.stdout)], ["SAME"])
        payload.write_text(json.dumps({"stage": "bogus"}), encoding="utf-8")
        failed = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "shipment-worklist", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2, failed.stdout)
        self.assertIn("error", json.loads(failed.stderr))
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([{"stage": "open"}, {"stage": "conflict"},
                                    {"stage": "all"}]), encoding="utf-8")
        run = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "shipment-worklist", str(batch)],
            text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        results = json.loads(run.stdout)
        self.assertEqual(len(results), 3)
        self.assertEqual([g["tracking_no"] for g in results[0]], ["SAME"])
        self.assertEqual(results[1], [])
        self.assertEqual([(g["carrier"], g["tracking_no"]) for g in results[2]],
                         [("DHL", "SAME"), ("UPS", "OTHER")])

    def _write_raw(self, data):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "data.json").write_text(json.dumps(data), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
