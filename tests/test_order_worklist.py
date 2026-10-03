import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk


class OrderWorklistTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)
        self.app.add_product("U", "Unmanaged", 50)

    def _tasks(self, stage="open"):
        return {entry["order_id"]: entry["tasks"]
                for entry in self.app.order_worklist(stage=stage)}

    def test_empty_root_returns_empty_and_creates_nothing(self):
        fresh = Path(self.temp.name) / "fresh"
        app = OrderDesk(fresh)
        self.assertEqual(app.order_worklist(), [])
        self.assertEqual(app.order_worklist(stage="all"), [])
        self.assertFalse((fresh / "data.json").exists())

    def test_legacy_data_without_orders_returns_empty(self):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "data.json").write_text(json.dumps({"products": {}}), encoding="utf-8")
        self.assertEqual(self.app.order_worklist(), [])
        self.assertEqual(self.app.order_worklist(stage="all"), [])

    def test_open_stage_picks_orders_with_tasks_sorted(self):
        self.app.restock("T", 10)
        # Fully reserved placed order: ship task.
        self.app.place("O2", [{"sku": "T", "quantity": 2}])
        # Placed while C was unmanaged, then C became managed: reserve task.
        self.app.place("O1", [{"sku": "C", "quantity": 3}])
        self.app.restock("C", 1)
        # Shipped order: deliver task.
        self.app.place("O3", [{"sku": "T", "quantity": 1}])
        self.app.ship("O3", "DHL", "TRK-3")
        # Delivered order without pending returns: no tasks.
        self.app.place("O4", [{"sku": "T", "quantity": 1}])
        self.app.ship("O4", "DHL", "TRK-4")
        self.app.confirm_delivery("O4", "Ann", "2026-10-01")
        # Cancelled order: no tasks.
        self.app.place("O5", [{"sku": "T", "quantity": 1}])
        self.app.cancel("O5")
        result = self.app.order_worklist()
        self.assertEqual([entry["order_id"] for entry in result], ["O1", "O2", "O3"])
        self.assertEqual(result[0]["tasks"], ["reserve"])
        self.assertEqual(result[1]["tasks"], ["ship"])
        self.assertEqual(result[2]["tasks"], ["deliver"])
        for entry in result:
            self.assertEqual(set(entry), {"order_id", "tasks", "progress"})

    def test_all_stage_includes_orders_without_tasks(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        self.app.cancel("O2")
        result = self.app.order_worklist(stage="all")
        self.assertEqual([entry["order_id"] for entry in result], ["O1", "O2"])
        self.assertEqual(result[0]["tasks"], ["ship"])
        self.assertEqual(result[1]["tasks"], [])

    def test_reserve_uses_actual_reservations_not_availability(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        # Releasing the reservation leaves plenty available, but needed follows
        # the actual reservation balance, never the current availability.
        self.app.release_reservation("O1", [{"sku": "T", "quantity": 3}])
        self.assertEqual(self._tasks()["O1"], ["reserve"])
        # Unmanaged products never create a reserve task.
        self.app.place("O2", [{"sku": "U", "quantity": 5}])
        self.assertEqual(self._tasks()["O2"], ["ship"])
        # Topping the reservation back up clears the reserve task.
        self.app.reserve_order("O1")
        self.assertEqual(self._tasks()["O1"], ["ship"])

    def test_shipped_with_pending_return_lists_both_tasks_once(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.ship("O1", "DHL", "TRK-1")
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        entry = self.app.order_worklist()[0]
        self.assertEqual(entry["tasks"], ["deliver", "receive-return"])
        # The order appears once in each matching filter.
        self.assertEqual([e["order_id"] for e in self.app.order_worklist(stage="deliver")], ["O1"])
        self.assertEqual([e["order_id"] for e in self.app.order_worklist(stage="receive-return")], ["O1"])
        # Confirming delivery leaves only the receive-return task.
        self.app.confirm_delivery("O1", "Ann", "2026-10-01")
        self.assertEqual(self._tasks()["O1"], ["receive-return"])
        self.assertEqual(self.app.order_worklist(stage="deliver"), [])
        # Receiving the return clears the last task.
        self.app.receive_return("R1")
        self.assertEqual(self.app.order_worklist(), [])

    def test_return_task_survives_paused_and_unmanaged_products(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 2}, {"sku": "U", "quantity": 1}])
        self.app.ship("O1", "DHL", "TRK-1")
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R2", [{"sku": "U", "quantity": 1}])
        self.app.set_product_enabled("T", False)
        self.assertEqual(self._tasks()["O1"], ["deliver", "receive-return"])

    def test_return_task_survives_missing_catalog_product(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.ship("O1", "DHL", "TRK-1")
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        data = json.loads((self.root / "data.json").read_text(encoding="utf-8"))
        del data["products"]["T"]
        (self.root / "data.json").write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self._tasks()["O1"], ["deliver", "receive-return"])

    def test_cancelled_and_received_returns_create_no_task(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 3}])
        self.app.ship("O1", "DHL", "TRK-1")
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "R2", [{"sku": "T", "quantity": 1}])
        self.app.cancel_return("R1")
        self.app.receive_return("R2")
        self.assertEqual(self._tasks()["O1"], ["deliver"])
        # Amending a pending registration is judged by its latest content.
        self.app.record_return("O1", "R3", [{"sku": "T", "quantity": 1}])
        self.assertEqual(self._tasks()["O1"], ["deliver", "receive-return"])
        self.app.amend_return("R3", [{"sku": "T", "quantity": 1}],
                              [{"sku": "T", "quantity": 1}])
        self.assertEqual(self._tasks()["O1"], ["deliver", "receive-return"])

    def test_progress_matches_order_progress_verbatim(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        entry = self.app.order_worklist()[0]
        self.assertEqual(entry["progress"], self.app.order_progress("O1"))

    def test_stage_whitespace_trimmed(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.assertEqual([e["order_id"] for e in self.app.order_worklist(stage="  ship\t")],
                         ["O1"])
        self.assertEqual(self.app.order_worklist(stage=" open "), self.app.order_worklist())

    def test_invalid_stage(self):
        for bad in (None, 123, 1.5, b"open", ["open"], {"stage": 1}, True,
                    "", "   ", "pending", "OPEN", " openx", "receive_return"):
            with self.assertRaises(ValueError):
                self.app.order_worklist(stage=bad)

    def test_query_is_read_only_and_repeatable(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        before = (self.root / "data.json").read_bytes()
        first = self.app.order_worklist()
        self.assertEqual((self.root / "data.json").read_bytes(), before)
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.order_worklist(), first)

    def test_cli_order_worklist_success_and_failure(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        payload = self.root / "p.json"
        payload.write_text(json.dumps({"stage": "all"}), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "order-worklist", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual([entry["order_id"] for entry in result], ["O1"])
        self.assertEqual(result[0]["tasks"], ["ship"])
        payload.write_text(json.dumps({"stage": "bogus"}), encoding="utf-8")
        failed = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "order-worklist", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2, failed.stdout)
        self.assertIn("error", json.loads(failed.stderr))

    def test_cli_array_executes_each_item(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        batch = self.root / "batch.json"
        batch.write_text(json.dumps([{"stage": "ship"}, {"stage": "deliver"}]),
                        encoding="utf-8")
        run = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "order-worklist", str(batch)],
            text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        results = json.loads(run.stdout)
        self.assertEqual(len(results), 2)
        self.assertEqual([e["order_id"] for e in results[0]], ["O1"])
        self.assertEqual(results[1], [])


if __name__ == "__main__":
    unittest.main()
