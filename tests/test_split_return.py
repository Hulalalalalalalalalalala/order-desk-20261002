import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk


class SplitReturnTests(unittest.TestCase):
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

    def test_split_moves_quantities_and_returns_before_source_target(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 3}])
        self.app.record_return("O1", "R1", [
            {"sku": "T", "quantity": 3},
            {"sku": "C", "quantity": 2},
        ])
        result = self.app.split_return(
            "R1", "R2",
            [{"sku": "C", "quantity": 2}, {"sku": "T", "quantity": 3}],
            [{"sku": "T", "quantity": 1}, {"sku": "T", "quantity": 1, "ignored": True}],
        )
        self.assertEqual(result, {
            "before": {
                "order_id": "O1",
                "return_id": "R1",
                "lines": [{"sku": "C", "quantity": 2}, {"sku": "T", "quantity": 3}],
            },
            "source": {
                "order_id": "O1",
                "return_id": "R1",
                "lines": [{"sku": "C", "quantity": 2}, {"sku": "T", "quantity": 1}],
            },
            "target": {
                "order_id": "O1",
                "return_id": "R2",
                "lines": [{"sku": "T", "quantity": 2}],
            },
        })
        self.assertEqual(set(result), {"before", "source", "target"})
        for view in result.values():
            self.assertEqual(set(view), {"order_id", "return_id", "lines"})
            self.assertTrue(all(set(line) == {"sku", "quantity"} for line in view["lines"]))
        # The stored records carry the split lines under the same order.
        self.assertEqual(self.app.get_returns("O1")["records"], [
            {"order_id": "O1", "return_id": "R1",
             "lines": [{"sku": "C", "quantity": 2}, {"sku": "T", "quantity": 1}]},
            {"order_id": "O1", "return_id": "R2",
             "lines": [{"sku": "T", "quantity": 2}]},
        ])

    def test_ids_trimmed_and_case_sensitive(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        result = self.app.split_return("  R1 ", " R2 ", [{"sku": "T", "quantity": 3}],
                                       [{"sku": "T", "quantity": 1}])
        self.assertEqual(result["target"]["return_id"], "R2")
        # Case-sensitive ids: r1 is unknown, and R1 stays occupied afterwards.
        with self.assertRaises(ValueError):
            self.app.split_return("r1", "R3", [{"sku": "T", "quantity": 2}],
                                  [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.split_return("R1", "R2", [{"sku": "T", "quantity": 2}],
                                  [{"sku": "T", "quantity": 1}])

    def test_target_id_may_match_order_or_cart(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.save_cart("CART1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        result = self.app.split_return("R1", "O1", [{"sku": "T", "quantity": 3}],
                                       [{"sku": "T", "quantity": 1}])
        self.assertEqual(result["target"]["return_id"], "O1")
        result = self.app.split_return("R1", "CART1", [{"sku": "T", "quantity": 2}],
                                       [{"sku": "T", "quantity": 1}])
        self.assertEqual(result["target"]["return_id"], "CART1")

    def test_target_id_occupied_by_active_received_or_cancelled_return(self):
        self._shipped("O1", [{"sku": "T", "quantity": 9}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 4}])
        self.app.record_return("O1", "RA", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "RR", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "RC", [{"sku": "T", "quantity": 1}])
        self.app.receive_return("RR")
        self.app.cancel_return("RC")
        before = self.app.path.read_bytes()
        for target_id in ("R1", "RA", "RR", "RC"):
            with self.assertRaises(ValueError):
                self.app.split_return("R1", target_id, [{"sku": "T", "quantity": 4}],
                                      [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_unknown_cancelled_and_received_source_rejected(self):
        self._shipped("O1", [{"sku": "T", "quantity": 9}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R3", [{"sku": "T", "quantity": 2}])
        self.app.cancel_return("R2")
        self.app.receive_return("R3")
        before = self.app.path.read_bytes()
        for source_id in ("R9", "R2", "R3"):
            with self.assertRaises(ValueError):
                self.app.split_return(source_id, "RN", [{"sku": "T", "quantity": 2}],
                                      [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_order_missing_or_not_shipped_rejected(self):
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "orders": {"OLD": {
                "order_id": "OLD", "status": "placed",
                "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100, "subtotal_cents": 200}],
                "total_cents": 200,
            }},
            "returns": {
                "OLD": [{"order_id": "OLD", "return_id": "RP", "lines": [{"sku": "T", "quantity": 1}]}],
                "GONE": [{"order_id": "GONE", "return_id": "RG", "lines": [{"sku": "T", "quantity": 1}]}],
            },
        }
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(self.root)
        before = app.path.read_bytes()
        for return_id in ("RP", "RG"):
            with self.assertRaises(ValueError):
                app.split_return(return_id, "RN", [{"sku": "T", "quantity": 1}],
                                 [{"sku": "T", "quantity": 1}])
        self.assertEqual(app.path.read_bytes(), before)

    def test_delivered_order_can_be_split(self):
        self._shipped("O1", [{"sku": "T", "quantity": 3}])
        self.app.confirm_delivery("O1", "Sam", "2026-10-01")
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        result = self.app.split_return("R1", "R2", [{"sku": "T", "quantity": 2}],
                                       [{"sku": "T", "quantity": 1}])
        self.assertEqual(result["source"]["lines"], [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.get("O1")["status"], "delivered")

    def test_expected_mismatch_rejected(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 3}])
        self.app.record_return("O1", "R1", [
            {"sku": "T", "quantity": 1},
            {"sku": "C", "quantity": 1},
            {"sku": "T", "quantity": 1},
        ])
        before = self.app.path.read_bytes()
        for expected in (
            [{"sku": "T", "quantity": 2}],
            [{"sku": "T", "quantity": 1}, {"sku": "C", "quantity": 1}],
            [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 2}],
            [{"sku": "t", "quantity": 2}, {"sku": "C", "quantity": 1}],
        ):
            with self.assertRaises(ValueError):
                self.app.split_return("R1", "R2", expected, [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.path.read_bytes(), before)
        # Row order does not matter for the original-content check.
        result = self.app.split_return("R1", "R2",
                                       [{"sku": "C", "quantity": 1}, {"sku": "T", "quantity": 2}],
                                       [{"sku": "C", "quantity": 1}])
        self.assertEqual(result["source"]["lines"], [{"sku": "T", "quantity": 2}])

    def test_moved_sku_must_be_in_source_and_within_quantity(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 3}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        before = self.app.path.read_bytes()
        # C is in the order but not in this registration.
        with self.assertRaises(ValueError):
            self.app.split_return("R1", "R2", [{"sku": "T", "quantity": 2}],
                                  [{"sku": "C", "quantity": 1}])
        # Merged moved quantity exceeds the registered quantity.
        with self.assertRaises(ValueError):
            self.app.split_return("R1", "R2", [{"sku": "T", "quantity": 2}],
                                  [{"sku": "T", "quantity": 1}, {"sku": "T", "quantity": 2}])
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_cannot_split_the_whole_return(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 3}])
        self.app.record_return("O1", "R1", [
            {"sku": "T", "quantity": 2},
            {"sku": "C", "quantity": 1},
        ])
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.split_return("R1", "R2",
                                  [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}],
                                  [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])
        self.assertEqual(self.app.path.read_bytes(), before)

    def test_paused_unmanaged_and_uncatalogued_products_do_not_block(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}, {"sku": "U", "quantity": 2}],
                      stock={"T": 10})
        self.app.record_return("O1", "R1", [
            {"sku": "T", "quantity": 2},
            {"sku": "U", "quantity": 2},
        ])
        self.app.set_product_enabled("T", False)
        result = self.app.split_return("R1", "R2",
                                       [{"sku": "T", "quantity": 2}, {"sku": "U", "quantity": 2}],
                                       [{"sku": "U", "quantity": 1}])
        self.assertEqual(result["target"]["lines"], [{"sku": "U", "quantity": 1}])
        # A product dropped from the catalog after ordering can still be split.
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        del data["products"]["U"]
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        result = self.app.split_return("R1", "R3",
                                       [{"sku": "T", "quantity": 2}, {"sku": "U", "quantity": 1}],
                                       [{"sku": "U", "quantity": 1}])
        self.assertEqual(result["source"]["lines"], [{"sku": "T", "quantity": 2}])

    def test_invalid_identifiers_and_line_shapes(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        good = [{"sku": "T", "quantity": 2}]
        move = [{"sku": "T", "quantity": 1}]
        for bad_id in (None, 123, 1.5, b"R1", ["R1"], {"x": 1}, "   ", "\t\n", True):
            with self.assertRaises(ValueError):
                self.app.split_return(bad_id, "R2", good, move)
            with self.assertRaises(ValueError):
                self.app.split_return("R1", bad_id, good, move)
        for bad_lines in (None, "x", 5, {}, [], [{"sku": "T"}], [{"quantity": 1}],
                          [["sku"]], ["x"], [{"sku": " ", "quantity": 1}],
                          [{"sku": "T", "quantity": 0}], [{"sku": "T", "quantity": -1}],
                          [{"sku": "T", "quantity": 1.5}], [{"sku": "T", "quantity": "1"}],
                          [{"sku": "T", "quantity": True}]):
            with self.assertRaises(ValueError):
                self.app.split_return("R1", "R2", bad_lines, move)
            with self.assertRaises(ValueError):
                self.app.split_return("R1", "R2", good, bad_lines)
        # Same id after trimming is rejected.
        with self.assertRaises(ValueError):
            self.app.split_return("R1", " R1 ", good, move)

    def test_failures_create_no_file_and_consume_no_sequence(self):
        empty_root = Path(self.temp.name) / "empty"
        fresh = OrderDesk(empty_root)
        with self.assertRaises(ValueError):
            fresh.split_return("R1", "R2", [{"sku": "T", "quantity": 1}],
                               [{"sku": "T", "quantity": 1}])
        self.assertFalse(empty_root.exists())
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        with self.assertRaises(ValueError):
            self.app.split_return("R1", "R2", [{"sku": "T", "quantity": 1}],
                                  [{"sku": "T", "quantity": 1}])
        self.app.split_return("R1", "R2", [{"sku": "T", "quantity": 2}],
                              [{"sku": "T", "quantity": 1}])
        events = self.app.history("O1")["events"]
        self.assertEqual([(e["sequence"], e["action"]) for e in events],
                         [(1, "place"), (2, "ship"), (3, "record-return"), (4, "split-return")])

    def test_history_event_snapshot_and_legacy_order(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        result = self.app.split_return("R1", "R2", [{"sku": "T", "quantity": 2}],
                                       [{"sku": "T", "quantity": 1}])
        history = self.app.history("O1")
        self.assertTrue(history["complete"])
        event = history["events"][-1]
        self.assertEqual(set(event), {"sequence", "action", "result"})
        self.assertEqual(event["action"], "split-return")
        self.assertEqual(event["result"], result)
        # Old snapshots are untouched.
        self.assertEqual(history["events"][2]["result"]["lines"], [{"sku": "T", "quantity": 2}])
        # A legacy order without history starts at 1 with complete=False, and a
        # legacy registration without a receipt reads as pending.
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
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
        legacy_root = Path(self.temp.name) / "legacy"
        legacy_root.mkdir()
        OrderDesk(legacy_root).path.write_text(json.dumps(data), encoding="utf-8")
        app = OrderDesk(legacy_root)
        result = app.split_return("L1", "L2", [{"sku": "T", "quantity": 2}],
                                  [{"sku": "T", "quantity": 1}])
        history = app.history("OLD")
        self.assertFalse(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]],
                         [(1, "split-return")])
        self.assertEqual(history["events"][0]["result"], result)

    def test_stock_reservations_order_and_allowance_untouched(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.app.place("O2", [{"sku": "T", "quantity": 2}])
        order_before = self.app.get("O1")
        stock_t = self.app.stock("T")
        remaining_before = self.app.get_returns("O1")["remaining"]
        self.app.split_return("R1", "R2", [{"sku": "T", "quantity": 3}],
                              [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.get("O1"), order_before)
        self.assertEqual(self.app.get("O1")["shipment"], {"carrier": "DHL", "tracking_no": "TRK-O1"})
        self.assertEqual(self.app.stock("T"), stock_t)
        # Cumulative returned and returnable quantities are unchanged.
        self.assertEqual(self.app.get_returns("O1")["remaining"], remaining_before)
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["reservations"]["O2"], {"T": 2})
        # No receipt or stock event is produced by the split.
        self.assertNotIn("return_receipts", raw)
        self.assertEqual([e["action"] for e in raw["stock_history"]["T"]["events"]],
                         ["restock", "place", "ship", "place"])

    def test_persistence_and_follow_up_receive_and_cancel(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}, {"sku": "C", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.app.split_return("R1", "R2", [{"sku": "T", "quantity": 3}],
                              [{"sku": "T", "quantity": 1}])
        reopened = OrderDesk(self.root)
        # The worklist shows two pending registrations.
        entries = reopened.return_worklist()
        self.assertEqual([(e["return_id"], e["stage"], e["lines"]) for e in entries], [
            ("R1", "pending", [{"sku": "T", "quantity": 2}]),
            ("R2", "pending", [{"sku": "T", "quantity": 1}]),
        ])
        # Receiving only the new registration leaves the source pending.
        receipt = reopened.receive_return("R2")
        self.assertEqual([(line["sku"], line["quantity"]) for line in receipt["lines"]],
                         [("T", 1)])
        entries = reopened.return_worklist("all")
        self.assertEqual([(e["return_id"], e["stage"]) for e in entries],
                         [("R1", "pending"), ("R2", "received")])
        self.assertEqual(reopened.stock("T")["on_hand"], 6)
        # The source can still be cancelled with its remaining lines.
        result = reopened.cancel_return("R1")
        self.assertEqual(result["lines"], [{"sku": "T", "quantity": 2}])
        self.assertEqual(reopened.get_returns("O1")["remaining"], [
            {"sku": "C", "quantity": 2},
            {"sku": "T", "quantity": 4},
        ])

    def test_amount_and_quantity_queries_use_both_latest_records(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 3}])
        self.app.split_return("R1", "R2", [{"sku": "T", "quantity": 3}],
                              [{"sku": "T", "quantity": 1}])
        self.app.receive_return("R2")
        progress = self.app.order_progress("O1")
        self.assertEqual(progress["lines"], [{
            "sku": "T", "ordered": 5, "reserved": 0, "needed": 0,
            "shipped": 5, "pending": 2, "received": 1, "remaining": 2, "net": 4,
        }])
        summary = self.app.fulfillment_summary()
        self.assertEqual(summary["totals"], {
            "shipped": 5, "pending": 2, "received": 1, "net": 4,
            "shipped_cents": 500, "pending_cents": 200,
            "received_cents": 100, "net_cents": 400,
        })

    def test_cli_split_return_and_array_partial_failure(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "RA", [{"sku": "T", "quantity": 2}])
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([
            {"source_id": "R1", "target_id": "R2",
             "expected_lines": [{"sku": "T", "quantity": 2}],
             "lines": [{"sku": "T", "quantity": 1}]},
            {"source_id": "RA", "target_id": "RB",
             "expected_lines": [{"sku": "T", "quantity": 9}],
             "lines": [{"sku": "T", "quantity": 1}]},
        ]), encoding="utf-8")
        # Each array entry runs independently: the first succeeds, the second
        # fails and the command reports the failure.
        run = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "split-return", str(batch)],
            text=True, capture_output=True)
        self.assertEqual(run.returncode, 2, run.stdout)
        self.assertIn("expected lines", json.loads(run.stderr)["error"])
        self.assertEqual(self.app.get_returns("O1")["records"], [
            {"order_id": "O1", "return_id": "R1", "lines": [{"sku": "T", "quantity": 1}]},
            {"order_id": "O1", "return_id": "R2", "lines": [{"sku": "T", "quantity": 1}]},
            {"order_id": "O1", "return_id": "RA", "lines": [{"sku": "T", "quantity": 2}]},
        ])
        # A fully successful batch prints the results in order.
        batch.write_text(json.dumps([
            {"source_id": "RA", "target_id": "RB",
             "expected_lines": [{"sku": "T", "quantity": 2}],
             "lines": [{"sku": "T", "quantity": 1}]},
        ]), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "split-return", str(batch)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        payload = json.loads(ok.stdout)
        self.assertEqual(payload[0]["target"], {
            "order_id": "O1",
            "return_id": "RB",
            "lines": [{"sku": "T", "quantity": 1}],
        })


if __name__ == "__main__":
    unittest.main()
