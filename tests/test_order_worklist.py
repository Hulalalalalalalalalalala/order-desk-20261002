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
        self.app.add_product("U", "Unmanaged", 0)

    def _place(self, order_id, lines, stock=None):
        for sku, quantity in (stock or {"T": 10, "C": 10}).items():
            self.app.restock(sku, quantity)
        return self.app.place(order_id, lines)

    def _shipped(self, order_id, lines, stock=None):
        self._place(order_id, lines, stock=stock)
        self.app.ship(order_id, "DHL", "TRK-" + order_id)

    def _delivered(self, order_id, lines, stock=None):
        self._shipped(order_id, lines, stock=stock)
        self.app.confirm_delivery(order_id, "Ann", "2026-10-01")

    def test_default_open_selects_orders_with_tasks_sorted_by_order_id(self):
        # OR: fully reserved placed -> ship. OS: shipped -> deliver.
        # OD: delivered with no returns -> no task, excluded. OC: cancelled.
        self._place("OR", [{"sku": "T", "quantity": 2}])
        self._shipped("OS", [{"sku": "T", "quantity": 2}])
        self._delivered("OD", [{"sku": "T", "quantity": 2}])
        self._place("OC", [{"sku": "T", "quantity": 2}])
        self.app.cancel("OC")
        result = self.app.order_worklist()
        self.assertEqual([entry["order_id"] for entry in result], ["OR", "OS"])

    def test_entry_shape_and_progress_identical_to_order_progress(self):
        self._place("O1", [{"sku": "T", "quantity": 2}])
        entry = self.app.order_worklist()[0]
        self.assertEqual(set(entry), {"order_id", "tasks", "progress"})
        self.assertEqual(entry["order_id"], "O1")
        self.assertEqual(entry["progress"], self.app.order_progress("O1"))
        self.assertEqual(set(entry["progress"]), {"order", "history", "lines"})

    def test_placed_with_needed_shows_reserve_then_top_up_shows_ship(self):
        # Placed while U is unmanaged: nothing to reserve, so the task is ship.
        self.app.place("OU", [{"sku": "U", "quantity": 4}])
        self.assertEqual(self.app.order_worklist(stage="all")[0]["tasks"], ["ship"])
        # Manage U after the fact: needed appears without netting availability.
        self.app.restock("U", 10)
        entry = self.app.order_worklist(stage="reserve")[0]
        self.assertEqual(entry["order_id"], "OU")
        self.assertEqual(entry["tasks"], ["reserve"])
        self.assertEqual([t for t in self._tasks() if t == "reserve"], ["reserve"])
        self.assertEqual(self.app.order_worklist(stage="ship"), [])
        self.app.reserve_order("OU")
        self.assertEqual(self.app.order_worklist()[0]["tasks"], ["ship"])

    def test_released_reservation_shows_reserve_despite_available_stock(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 5}])
        self.app.release_reservation("O1", [{"sku": "T", "quantity": 2}])
        # Available is 7 again, but needed compares only against this order's
        # actual reservation.
        self.assertEqual(self.app.stock("T")["available"], 7)
        self.assertEqual(self.app.order_worklist()[0]["tasks"], ["reserve"])

    def test_partial_managed_shortfall_shows_reserve_not_ship(self):
        self.app.restock("T", 10)
        # Managed T fully covered, unmanaged U never raises a reserve task.
        self.app.place("OK", [{"sku": "T", "quantity": 2}, {"sku": "U", "quantity": 4}])
        self.assertEqual(self.app.order_worklist()[0]["tasks"], ["ship"])
        # A legacy placed order with a managed sku shortfall must list reserve.
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "inventory": {"T": {"on_hand": 5, "reserved": 0}},
            "orders": {"OLD": {
                "order_id": "OLD", "status": "placed",
                "lines": [{"sku": "T", "quantity": 3, "unit_price_cents": 100,
                           "subtotal_cents": 300}],
                "total_cents": 300,
            }},
            "reservations": {"OLD": {"T": 1}},
        }
        self._load(data)
        self.assertEqual(self.app.order_worklist()[0]["tasks"], ["reserve"])

    def test_shipped_tasks_order_deliver_before_receive_return(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        entry = self.app.order_worklist()[0]
        self.assertEqual(entry["tasks"], ["deliver", "receive-return"])
        # It appears once in each specific filter.
        self.assertEqual([e["order_id"] for e in self.app.order_worklist(stage="deliver")], ["O1"])
        self.assertEqual(
            [e["order_id"] for e in self.app.order_worklist(stage="receive-return")], ["O1"])

    def test_delivered_with_pending_return_has_only_receive_task(self):
        self._delivered("O1", [{"sku": "T", "quantity": 5}])
        self.assertEqual(self.app.order_worklist(), [])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        entries = self.app.order_worklist()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["tasks"], ["receive-return"])
        self.assertEqual(self.app.order_worklist(stage="deliver"), [])

    def test_cancelled_and_finished_orders_have_no_tasks(self):
        self._place("OC", [{"sku": "T", "quantity": 2}])
        self.app.cancel("OC")
        self._delivered("OD", [{"sku": "T", "quantity": 2}])
        self.assertEqual(self.app.order_worklist(), [])
        result = self.app.order_worklist(stage="all")
        self.assertEqual([e["order_id"] for e in result], ["OC", "OD"])
        for entry in result:
            self.assertEqual(entry["tasks"], [])

    def test_stage_filters(self):
        # reserve: placed order with a shortfall
        self.app.place("OR", [{"sku": "U", "quantity": 1}])
        self.app.restock("U", 5)
        # ship: placed order ready to go
        self._place("OS", [{"sku": "T", "quantity": 1}])
        # deliver + receive-return: shipped with a pending return
        self._shipped("OD", [{"sku": "T", "quantity": 2}])
        self.app.record_return("OD", "RD", [{"sku": "T", "quantity": 1}])
        # receive-return only: delivered with a pending return
        self._delivered("ORR", [{"sku": "C", "quantity": 2}])
        self.app.record_return("ORR", "RR", [{"sku": "C", "quantity": 1}])
        # cancelled: never selected by task filters
        self._place("OC", [{"sku": "T", "quantity": 1}])
        self.app.cancel("OC")
        self.assertEqual([e["order_id"] for e in self.app.order_worklist(stage="reserve")], ["OR"])
        self.assertEqual([e["order_id"] for e in self.app.order_worklist(stage="ship")], ["OS"])
        self.assertEqual([e["order_id"] for e in self.app.order_worklist(stage="deliver")], ["OD"])
        self.assertEqual(
            [e["order_id"] for e in self.app.order_worklist(stage="receive-return")],
            ["OD", "ORR"],
        )
        self.assertEqual(
            [e["order_id"] for e in self.app.order_worklist(stage="open")],
            ["OD", "OR", "ORR", "OS"],
        )
        all_orders = self.app.order_worklist(stage="all")
        self.assertEqual([e["order_id"] for e in all_orders], ["OC", "OD", "OR", "ORR", "OS"])
        self.assertEqual([e for e in all_orders if e["order_id"] == "OD"][0]["tasks"],
                         ["deliver", "receive-return"])

    def test_stage_whitespace_trimmed(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        self.assertEqual([e["order_id"] for e in self.app.order_worklist(stage=" open ")], ["O1"])
        self.assertEqual([e["order_id"] for e in self.app.order_worklist(stage="\tall\n")],
                         ["O1"])

    def test_invalid_stage(self):
        for bad in (None, 123, 1.5, b"open", ["open"], {"stage": 1}, True,
                    "", "   ", "done", "OPEN", " openx", "reserved"):
            with self.assertRaises(ValueError):
                self.app.order_worklist(stage=bad)

    def test_received_and_cancelled_returns_clear_receive_task(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "RP", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "RR", [{"sku": "T", "quantity": 1}])
        self.app.record_return("O1", "RC", [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.order_worklist()[0]["tasks"],
                         ["deliver", "receive-return"])
        self.app.receive_return("RR")
        self.app.cancel_return("RC")
        # One pending registration remains.
        self.assertEqual(self.app.order_worklist()[0]["tasks"],
                         ["deliver", "receive-return"])
        self.app.cancel_return("RP")
        self.assertEqual(self.app.order_worklist()[0]["tasks"], ["deliver"])
        self.assertEqual(self.app.order_worklist(stage="receive-return"), [])

    def test_amended_return_judged_at_latest_lines(self):
        self._shipped("O1", [{"sku": "T", "quantity": 5}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 2}])
        self.assertEqual(
            self.app.order_worklist(stage="receive-return")[0]["progress"]["lines"][0]["pending"],
            2,
        )
        self.app.amend_return("R1", [{"sku": "T", "quantity": 2}],
                              [{"sku": "T", "quantity": 1}])
        entry = self.app.order_worklist(stage="receive-return")[0]
        self.assertEqual(entry["progress"]["lines"][0]["pending"], 1)
        self.app.receive_return("R1")
        self.assertEqual(self.app.order_worklist(stage="receive-return"), [])

    def test_receive_task_kept_when_product_paused_missing_or_unmanaged(self):
        data = {
            "products": {"U": {"sku": "U", "name": "Unmanaged", "price_cents": 0}},
            "orders": {
                "OLD": {
                    "order_id": "OLD", "status": "delivered",
                    "lines": [
                        {"sku": "T", "quantity": 1, "unit_price_cents": 100,
                         "subtotal_cents": 100},
                        {"sku": "U", "quantity": 2, "unit_price_cents": 0,
                         "subtotal_cents": 0},
                    ],
                    "total_cents": 100,
                    "delivery": {"recipient": "Ann", "delivered_on": "2026-10-01"},
                },
            },
            "returns": {"OLD": [
                {"order_id": "OLD", "return_id": "L1", "lines": [
                    {"sku": "T", "quantity": 1},
                    {"sku": "U", "quantity": 2},
                ]},
            ]},
        }
        self._load(data)
        entries = self.app.order_worklist()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["tasks"], ["receive-return"])
        # Paused sales do not remove the task either; T was absent from the
        # legacy catalog, so add it back on the same root.
        self.app.add_product("T", "Tea", 100)
        self.app.restock("T", 2)
        self.app.place("O2", [{"sku": "T", "quantity": 2}])
        self.app.ship("O2", "DHL", "2")
        self.app.record_return("O2", "R2", [{"sku": "T", "quantity": 1}])
        self.app.set_product_enabled("T", False)
        ids = [e["order_id"] for e in self.app.order_worklist(stage="receive-return")]
        self.assertEqual(ids, ["O2", "OLD"])

    def test_empty_root_and_missing_orders_collection(self):
        empty = self.root / "empty"
        self.assertEqual(OrderDesk(empty).order_worklist(), [])
        self.assertFalse(empty.exists())
        # Legacy document carrying other collections but no orders.
        legacy = self.root / "legacy"
        legacy.mkdir()
        (legacy / "data.json").write_text(json.dumps({"history": {}, "returns": {}}),
                                          encoding="utf-8")
        self.assertEqual(OrderDesk(legacy).order_worklist(stage="all"), [])

    def test_missing_collections_use_empty_semantics(self):
        # Legacy placed order, no reservations/history: managed line short of
        # its reservation still lists reserve; history is not fabricated.
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "inventory": {"T": {"on_hand": 5, "reserved": 0}},
            "orders": {"OLD": {
                "order_id": "OLD", "status": "placed",
                "lines": [{"sku": "T", "quantity": 2, "unit_price_cents": 100,
                           "subtotal_cents": 200}],
                "total_cents": 200,
            }},
        }
        self._load(data)
        entry = self.app.order_worklist()[0]
        self.assertEqual(entry["tasks"], ["reserve"])
        self.assertEqual(entry["progress"]["history"],
                         {"order_id": "OLD", "status": "placed",
                          "complete": False, "events": []})

    def test_tasks_reflect_later_operations(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.assertEqual(self.app.order_worklist()[0]["tasks"], ["ship"])
        self.app.ship("O1", "DHL", "1")
        self.assertEqual(self.app.order_worklist()[0]["tasks"], ["deliver"])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        self.assertEqual(self.app.order_worklist()[0]["tasks"],
                         ["deliver", "receive-return"])
        self.app.confirm_delivery("O1", "Ann", "2026-10-01")
        self.assertEqual(self.app.order_worklist()[0]["tasks"], ["receive-return"])
        self.app.receive_return("R1")
        self.assertEqual(self.app.order_worklist(), [])

    def test_query_writes_nothing_and_consumes_no_sequences(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        before = self.app.path.read_bytes()
        first = self.app.order_worklist(stage="all")
        for value in ("open", "all", "reserve", "ship", "deliver", "receive-return",
                      " open ", "bogus"):
            try:
                self.app.order_worklist(stage=value)
            except ValueError:
                pass
        self.assertEqual(self.app.path.read_bytes(), before)
        events = self.app.history("O1")["events"]
        self.assertEqual([(e["sequence"], e["action"]) for e in events],
                         [(1, "place"), (2, "ship"), (3, "record-return")])
        # Reopen consistency.
        self.assertEqual(OrderDesk(self.root).order_worklist(stage="all"), first)
        # Invalid stage on a root with no data directory never creates it.
        empty = self.root / "empty"
        with self.assertRaises(ValueError):
            OrderDesk(empty).order_worklist(stage=123)
        self.assertFalse(empty.exists())

    def test_cli_success_and_failure(self):
        self._shipped("O1", [{"sku": "T", "quantity": 2}])
        self.app.record_return("O1", "R1", [{"sku": "T", "quantity": 1}])
        payload = self.root / "q.json"
        payload.write_text(json.dumps({"stage": " all "}), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "order-worklist", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual([e["order_id"] for e in result], ["O1"])
        self.assertEqual(result[0]["tasks"], ["deliver", "receive-return"])
        # No input file defaults to open.
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "order-worklist"],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual([e["order_id"] for e in json.loads(ok.stdout)], ["O1"])
        # Array input is dispatched row by row.
        payload.write_text(json.dumps([{"stage": "deliver"}, {"stage": "ship"}]),
                           encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "order-worklist", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        rows = json.loads(ok.stdout)
        self.assertEqual(len(rows), 2)
        self.assertEqual([e["order_id"] for e in rows[0]], ["O1"])
        self.assertEqual(rows[1], [])
        # Invalid stage exits with code 2 and mentions stage.
        payload.write_text(json.dumps({"stage": "bogus"}), encoding="utf-8")
        failed = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "order-worklist", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2, failed.stdout)
        self.assertIn("stage", json.loads(failed.stderr)["error"])

    def _tasks(self):
        return [task for entry in self.app.order_worklist(stage="all")
                for task in entry["tasks"]]

    def _load(self, data):
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")
        self.app = OrderDesk(self.root)


if __name__ == "__main__":
    unittest.main()
