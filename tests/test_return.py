import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk

class ReturnTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)
        self.app.restock("T", 10)
        self.app.restock("C", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}, {"sku": "T", "quantity": 1}])
        self.app.ship("O1", "DHL", "X-1")

    def _ship(self, order_id, lines):
        self.app.place(order_id, lines)
        self.app.ship(order_id, "DHL", order_id)

    def test_record_return_merges_sorts_and_returns_record_only(self):
        record = self.app.record_return(" O1 ", " R1 ", [
            {"sku": " T ", "quantity": 1}, {"sku": "C", "quantity": 1}, {"sku": "T", "quantity": 1},
        ])
        self.assertEqual(set(record), {"order_id", "return_id", "lines"})
        self.assertEqual(record["order_id"], "O1")
        self.assertEqual(record["return_id"], "R1")
        self.assertEqual([set(line) for line in record["lines"]], [{"sku", "quantity"}, {"sku", "quantity"}])
        self.assertEqual(record["lines"], [{"sku": "C", "quantity": 1}, {"sku": "T", "quantity": 2}])

    def test_multiple_returns_accumulate_and_remaining_keeps_zeros(self):
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 2}, {"sku": "C", "quantity": 1}])
        view = self.app.get_returns("O1")
        self.assertEqual(set(view), {"order_id", "records", "remaining"})
        self.assertEqual(view["order_id"], "O1")
        self.assertEqual([r["return_id"] for r in view["records"]], ["R1", "R2"])
        self.assertEqual(view["records"][0]["lines"], [{"sku": "T", "quantity": 1}])
        self.assertEqual(view["records"][1]["lines"], [{"sku": "C", "quantity": 1}, {"sku": "T", "quantity": 2}])
        self.assertEqual(view["remaining"], [{"sku": "C", "quantity": 0}, {"sku": "T", "quantity": 0}])

    def test_no_returns_yet_gives_empty_records_and_full_remaining(self):
        view = self.app.get_returns("O1")
        self.assertEqual(view["records"], [])
        self.assertEqual(view["remaining"], [{"sku": "C", "quantity": 1}, {"sku": "T", "quantity": 3}])

    def test_records_sorted_by_return_id_and_ids_unique_across_entire_root(self):
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self._ship("O2", [{"sku": "C", "quantity": 2}])
        with self.assertRaises(ValueError):
            self.app.record_return("O2", "R1", [{"sku": "C", "quantity": 1}])
        self.assertEqual([r["return_id"] for r in self.app.get_returns("O1")["records"]], ["R1", "R2"])
        self.assertEqual(self.app.get_returns("O2")["records"], [])

    def test_duplicate_submission_even_identical_is_rejected_without_overwrite(self):
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        before = self.app.path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.path.read_bytes(), before)
        view = self.app.get_returns("O1")
        self.assertEqual(len(view["records"]), 1)
        self.assertEqual(view["records"][0]["lines"], [{"sku": "T", "quantity": 1}])

    def test_only_shipped_orders_can_be_returned(self):
        self.app.place("P1", [{"sku": "T", "quantity": 1}])
        self.app.place("Q1", [{"sku": "T", "quantity": 1}])
        self.app.cancel("Q1")
        for order_id in ("P1", "Q1", "missing"):
            with self.assertRaises(ValueError):
                self.app.record_return(order_id, "RX", [{"sku": "T", "quantity": 1}])
        with self.assertRaises(ValueError):
            self.app.get_returns("missing")
        # Existing non-shipped orders are still queryable.
        self.assertEqual(self.app.get_returns("P1")["remaining"], [{"sku": "T", "quantity": 1}])

    def test_invalid_identifiers_and_line_structure(self):
        bad = [
            {"order_id": "  ", "return_id": "R", "lines": [{"sku": "T", "quantity": 1}]},
            {"order_id": 3, "return_id": "R", "lines": [{"sku": "T", "quantity": 1}]},
            {"order_id": "O1", "return_id": None, "lines": [{"sku": "T", "quantity": 1}]},
            {"order_id": "O1", "return_id": "R", "lines": []},
            {"order_id": "O1", "return_id": "R", "lines": "notalist"},
            {"order_id": "O1", "return_id": "R", "lines": [{"sku": "T"}]},
            {"order_id": "O1", "return_id": "R", "lines": ["junk"]},
            {"order_id": "O1", "return_id": "R", "lines": [{"sku": " T ", "quantity": True}]},
            {"order_id": "O1", "return_id": "R", "lines": [{"sku": " T ", "quantity": 1.0}]},
            {"order_id": "O1", "return_id": "R", "lines": [{"sku": " T ", "quantity": 0}]},
            {"order_id": "O1", "return_id": "R", "lines": [{"sku": " T ", "quantity": "1"}]},
            {"order_id": "O1", "return_id": "R", "lines": [{"sku": "T", "quantity": -1}]},
            {"order_id": "O1", "return_id": "R", "lines": [{"sku": "", "quantity": 1}]},
            {"order_id": "O1", "return_id": "R", "lines": [{"sku": "U", "quantity": 1}]},
        ]
        for kwargs in bad:
            with self.assertRaises(ValueError):
                self.app.record_return(**kwargs)
        self.assertEqual(self.app.get_returns("O1")["records"], [])

    def test_case_sensitive_sku_matching(self):
        with self.assertRaises(ValueError):
            self.app.record_return("O1", "R", [{"sku": "t", "quantity": 1}])
        self.assertEqual(self.app.get_returns("O1")["records"], [])

    def test_cumulative_overflow_rejects_whole_registration_and_does_not_consume_id(self):
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        before = self.app.path.read_bytes()
        for lines in (
            [{"sku": "T", "quantity": 2}],
            [{"sku": "T", "quantity": 1}, {"sku": "T", "quantity": 1}],
            [{"sku": "T", "quantity": 1}, {"sku": "C", "quantity": 2}],
        ):
            with self.assertRaises(ValueError):
                self.app.record_return("O1", "R9", lines)
        self.assertEqual(self.app.path.read_bytes(), before)
        # The failed attempts did not reserve R9 or change cumulative totals.
        self.app.record_return("O1", "R9", [{"sku": "T", "quantity": 1}, {"sku": "C", "quantity": 1}])
        view = self.app.get_returns("O1")
        self.assertEqual([r["return_id"] for r in view["records"]], ["R1", "R9"])
        self.assertEqual(view["remaining"], [{"sku": "C", "quantity": 0}, {"sku": "T", "quantity": 0}])

    def test_return_changes_nothing_else(self):
        order_before = self.app.get("O1")
        self.app.place("O3", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        order_after = self.app.get("O1")
        self.assertEqual(order_after, order_before)
        self.assertEqual(order_after["status"], "shipped")
        self.assertEqual(order_after["total_cents"], 500)
        self.assertEqual(order_after["shipment"], {"carrier": "DHL", "tracking_no": "X-1"})
        # No refund, no stock replenishment; other reservation stays intact.
        self.assertEqual(self.app.stock("T"), {"sku": "T", "on_hand": 7, "reserved": 2, "available": 5})
        self.assertNotIn("returns", json.dumps(self.app.list_orders()))

    def test_persistence_old_files_and_query_does_not_write(self):
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        reopened = OrderDesk(self.root)
        view = reopened.get_returns("O1")
        self.assertEqual([r["return_id"] for r in view["records"]], ["R1"])
        self.assertEqual(view["remaining"], [{"sku": "C", "quantity": 1}, {"sku": "T", "quantity": 1}])
        # Querying changes nothing on disk.
        before = self.app.path.read_bytes()
        reopened.get_returns("O1")
        self.assertEqual(self.app.path.read_bytes(), before)
        # An old file without a returns section reads and queries normally.
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        del raw["returns"]
        self.app.path.write_text(json.dumps(raw), encoding="utf-8")
        old = OrderDesk(self.root)
        self.assertEqual(old.get_returns("O1")["remaining"],
                         [{"sku": "C", "quantity": 1}, {"sku": "T", "quantity": 3}])
        self.assertEqual(self.app.path.read_text(encoding="utf-8"), json.dumps(raw))

    def test_cli_record_return_returns_and_array_partial_failure(self):
        payload = self.root / "ret.json"
        payload.write_text(json.dumps({"order_id": "O1", "return_id": "R1",
                                       "lines": [{"sku": "T", "quantity": 1}, {"sku": "T", "quantity": 1}]}),
                           encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                             "record-return", str(payload)], text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout),
                         {"order_id": "O1", "return_id": "R1", "lines": [{"sku": "T", "quantity": 2}]})
        dup = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                              "record-return", str(payload)], text=True, capture_output=True)
        self.assertEqual(dup.returncode, 2)
        self.assertIn("error", json.loads(dup.stderr))
        (self.root / "q.json").write_text(json.dumps({"order_id": "O1"}), encoding="utf-8")
        query = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                                "returns", str(self.root / "q.json")], text=True, capture_output=True)
        self.assertEqual(query.returncode, 0, query.stderr)
        self.assertEqual(json.loads(query.stdout)["remaining"],
                         [{"sku": "C", "quantity": 1}, {"sku": "T", "quantity": 1}])
        self._ship("O2", [{"sku": "C", "quantity": 2}])
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([
            {"order_id": "O2", "return_id": "B1", "lines": [{"sku": "C", "quantity": 1}]},
            {"order_id": "O2", "return_id": "B2", "lines": [{"sku": "C", "quantity": 99}]},
            {"order_id": "O2", "return_id": "B3", "lines": [{"sku": "C", "quantity": 1}]},
        ]), encoding="utf-8")
        stopped = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                                  "record-return", str(batch)], text=True, capture_output=True)
        self.assertEqual(stopped.returncode, 2, stopped.stdout)
        self.assertIn("error", json.loads(stopped.stderr))
        view = OrderDesk(self.root).get_returns("O2")
        self.assertEqual([r["return_id"] for r in view["records"]], ["B1"])
        # Failed B2 did not consume its id: B2 can be registered afterwards.
        self.app.record_return("O2", "B2", [{"sku": "C", "quantity": 1}])
        self.assertEqual(self.app.get_returns("O2")["remaining"], [{"sku": "C", "quantity": 0}])

if __name__ == "__main__":
    unittest.main()
