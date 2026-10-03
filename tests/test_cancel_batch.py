import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk


class CancelBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)
        self.app.add_product("C", "Coffee", 200)
        self.app.add_product("U", "Unrestocked", 50)

    def payload(self, rows):
        path = self.root / "input.json"
        path.write_text(json.dumps({"order_ids": rows}), encoding="utf-8")
        return path

    def test_batch_cancels_all_orders_sorted_with_lines_preserved(self):
        self.app.restock("T", 10)
        o2 = self.app.place("O2", [{"sku": "T", "quantity": 2}, {"sku": "T", "quantity": 1}])
        o1 = self.app.place("O1", [{"sku": "C", "quantity": 1}])
        result = self.app.cancel_batch([" O2 ", "O1"])
        self.assertEqual([order["order_id"] for order in result], ["O1", "O2"])
        for order in result:
            self.assertEqual(order["status"], "cancelled")
            self.assertEqual(set(order), {"order_id", "status", "lines", "total_cents"})
        by_id = {order["order_id"]: order for order in result}
        # Lines, amounts and duplicate-sku arrangement are preserved.
        self.assertEqual(by_id["O2"]["lines"], o2["lines"])
        self.assertEqual(by_id["O2"]["total_cents"], o2["total_cents"])
        self.assertEqual(by_id["O1"]["lines"], o1["lines"])
        reopened = OrderDesk(self.root)
        self.assertEqual(reopened.list_orders(), result)
        self.assertEqual(reopened.get("O1"), by_id["O1"])

    def test_batch_releases_only_each_orders_reservation(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.place("O2", [{"sku": "T", "quantity": 2}])
        self.app.place("O3", [{"sku": "T", "quantity": 3}])
        result = self.app.cancel_batch(["O2", "O1"])
        self.assertEqual([order["order_id"] for order in result], ["O1", "O2"])
        # 10 on hand; O1 and O2 held 2 each and O3 keeps its 3, so reserved
        # drops 7 -> 3 and available rises 3 -> 7 while on_hand stays 10.
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 10, "reserved": 3, "available": 7})
        self.assertEqual(self.app.get("O3")["status"], "placed")
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["reservations"], {"O3": {"T": 3}})

    def test_unmanaged_later_managed_and_legacy_orders_cancel_without_stock_change(self):
        self.app.place("U1", [{"sku": "U", "quantity": 1000}])
        # Product managed only after the order was placed: no real reservation.
        self.app.place("C1", [{"sku": "C", "quantity": 4}])
        self.app.restock("C", 9)
        # Legacy order: placed record without a reservation record or history.
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        data["orders"]["OLD"] = {"order_id": "OLD", "status": "placed",
                                 "lines": [{"sku": "T", "quantity": 2}], "total_cents": 200}
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        result = self.app.cancel_batch(["U1", "C1", "OLD"])
        self.assertEqual([order["order_id"] for order in result], ["C1", "OLD", "U1"])
        raw = json.loads(self.app.path.read_text(encoding="utf-8"))
        # The unmanaged product is not auto-managed and nothing is released by
        # ordered quantity: C1 never held a real reservation.
        self.assertNotIn("U", raw.get("inventory", {}))
        self.assertNotIn("reservations", raw)
        self.assertEqual(self.app.stock("C"),
                         {"sku": "C", "on_hand": 9, "reserved": 0, "available": 9})
        # No stock change means no stock events beyond the restock.
        stock_history = self.app.stock_history("C")
        self.assertEqual([e["action"] for e in stock_history["events"]], ["restock"])
        # The legacy order gets a fresh history starting at 1, complete false.
        history = self.app.history("OLD")
        self.assertFalse(history["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in history["events"]], [(1, "cancel")])
        self.assertEqual(history["events"][0]["result"], self.app.get("OLD"))

    def test_paused_or_catalog_missing_product_still_cancels(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.place("O2", [{"sku": "C", "quantity": 1}])
        self.app.set_product_enabled("T", False)
        # A product dropped from the current catalog does not block cancel.
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        del data["products"]["C"]
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        result = self.app.cancel_batch(["O1", "O2"])
        self.assertEqual([order["status"] for order in result], ["cancelled", "cancelled"])
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 5, "reserved": 0, "available": 5})

    def test_history_sequences_continue_and_stock_snapshots_chain_in_input_order(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.app.amend("O1", [{"sku": "T", "quantity": 2}])
        self.app.place("O2", [{"sku": "T", "quantity": 3}])
        self.app.cancel_batch(["O2", "O1"])
        h1 = self.app.history("O1")
        self.assertTrue(h1["complete"])
        self.assertEqual([(e["sequence"], e["action"]) for e in h1["events"]],
                         [(1, "place"), (2, "amend"), (3, "cancel")])
        self.assertEqual(h1["events"][2]["result"], self.app.get("O1"))
        h2 = self.app.history("O2")
        self.assertEqual([(e["sequence"], e["action"]) for e in h2["events"]],
                         [(1, "place"), (2, "cancel")])
        # Stock events follow the request input order (O2 before O1), each
        # before snapshot chaining the previous after; the sorted return order
        # does not change event order.
        events = self.app.stock_history("T")["events"]
        self.assertEqual([(e["action"], e["reference_id"]) for e in events],
                         [("restock", None), ("place", "O1"), ("amend", "O1"),
                          ("place", "O2"), ("cancel", "O2"), ("cancel", "O1")])
        self.assertEqual(events[4]["before"]["reserved"], 5)
        self.assertEqual(events[4]["after"]["reserved"], 2)
        self.assertEqual(events[5]["before"]["reserved"], 2)
        self.assertEqual(events[5]["after"]["reserved"], 0)

    def test_invalid_batches_are_rejected_entirely(self):
        self.app.restock("T", 10)
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        self.app.place("O2", [{"sku": "T", "quantity": 1}])
        self.app.place("dup", [{"sku": "T", "quantity": 1}])
        self.app.cancel("dup")
        self.app.ship("O2", "DHL", "2")
        before = self.app.path.read_bytes()
        invalid_lists = [
            [],
            None,
            {},
            "x",
            [3],
            [True],
            [None],
            ["  "],
            [" O1 ", "O1"],
            ["missing"],
            # Already cancelled and already shipped orders are rejected too.
            ["dup"],
            ["O2"],
            # A bad later entry must roll back nothing even though the first is fine.
            ["O1", "missing"],
            ["O1", "dup"],
        ]
        for order_ids in invalid_lists:
            with self.subTest(order_ids=order_ids):
                with self.assertRaises(ValueError):
                    self.app.cancel_batch(order_ids)
        self.assertEqual(self.app.path.read_bytes(), before)
        self.assertEqual(self.app.get("O1")["status"], "placed")
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 9, "reserved": 1, "available": 8})
        raw = json.loads(before)
        self.assertEqual(raw["history"]["O1"]["events"][-1]["action"], "place")
        self.assertEqual(raw["reservations"], {"O1": {"T": 1}})

    def test_case_sensitive_ids_are_not_duplicates(self):
        self.app.restock("T", 5)
        self.app.place("a", [{"sku": "T", "quantity": 1}])
        self.app.place("A", [{"sku": "T", "quantity": 1}])
        result = self.app.cancel_batch(["a", " A "])
        self.assertEqual([order["order_id"] for order in result], ["A", "a"])

    def test_failure_does_not_create_directory(self):
        missing_root = self.root / "does-not-exist"
        app = OrderDesk(missing_root)
        with self.assertRaises(ValueError):
            app.cancel_batch([])
        with self.assertRaises(ValueError):
            app.cancel_batch(["X"])
        self.assertFalse(missing_root.exists())

    def test_batch_cancelled_orders_follow_normal_reopen_rules(self):
        self.app.restock("T", 5)
        self.app.place("O1", [{"sku": "T", "quantity": 2}])
        self.app.cancel_batch(["O1"])
        order = self.app.reopen_order("O1")
        self.assertEqual(order["status"], "placed")
        self.assertEqual(self.app.stock("T"),
                         {"sku": "T", "on_hand": 5, "reserved": 2, "available": 3})

    def test_cli_success_failure_and_outer_array_independence(self):
        self.app.restock("T", 10)
        self.app.place("A", [{"sku": "T", "quantity": 3}])
        self.app.place("B", [{"sku": "T", "quantity": 2}])
        self.app.place("C", [{"sku": "T", "quantity": 1}])
        ok_payload = self.payload(["B", "A"])
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "cancel-batch", str(ok_payload)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        cancelled = json.loads(ok.stdout)
        self.assertEqual([order["order_id"] for order in cancelled], ["A", "B"])
        # A single bad id in the request rejects the whole batch.
        bad_payload = self.payload(["C", "missing"])
        bad = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "cancel-batch", str(bad_payload)],
            text=True, capture_output=True)
        self.assertEqual(bad.returncode, 2, bad.stdout)
        self.assertIn("error", json.loads(bad.stderr))
        self.assertEqual(OrderDesk(self.root).get("C")["status"], "placed")
        # The outer JSON array still runs requests independently: the first
        # request succeeds and is not rolled back when the second fails.
        outer = self.root / "outer.json"
        outer.write_text(json.dumps([
            {"order_ids": ["C"]},
            {"order_ids": ["missing"]},
        ]), encoding="utf-8")
        stopped = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "cancel-batch", str(outer)],
            text=True, capture_output=True)
        self.assertEqual(stopped.returncode, 2, stopped.stdout)
        self.assertIn("error", json.loads(stopped.stderr))
        # A and B (5 units) were cancelled earlier; C's 1 unit now cancels too.
        self.assertEqual(OrderDesk(self.root).get("C")["status"], "cancelled")
        self.assertEqual(OrderDesk(self.root).stock("T"),
                         {"sku": "T", "on_hand": 10, "reserved": 0, "available": 10})

    def test_cli_rejects_non_object_input(self):
        path = self.root / "array.json"
        path.write_text(json.dumps([{"order_id": "A"}]), encoding="utf-8")
        failed = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root),
             "cancel-batch", str(path)],
            text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2)
        self.assertIn("error", json.loads(failed.stderr))


if __name__ == "__main__":
    unittest.main()
