import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from order_desk import OrderDesk

class OrderRelationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = OrderDesk(self.root)
        self.app.add_product("T", "Tea", 100)

    def _chain(self):
        # A splits out B, then B merges into C: A -> B -> C.
        self.app.place("A", [{"sku": "T", "quantity": 3}])
        self.app.split_order("A", "B", [{"sku": "T", "quantity": 1}])
        self.app.place("C", [{"sku": "T", "quantity": 2}])
        self.app.merge_orders("B", "C")

    def test_chain_is_returned_from_every_end_and_middle(self):
        self._chain()
        expected_relations = [
            {"action": "split-order", "source_id": "A", "target_id": "B",
             "evidence": [{"order_id": "A", "sequence": 2}, {"order_id": "B", "sequence": 1}]},
            {"action": "merge-orders", "source_id": "B", "target_id": "C",
             "evidence": [{"order_id": "B", "sequence": 2}, {"order_id": "C", "sequence": 2}]},
        ]
        for query in ("A", "B", "C"):
            result = self.app.order_relations(query)
            self.assertEqual(set(result), {"order_id", "orders", "relations", "complete"})
            self.assertEqual(result["order_id"], query)
            self.assertEqual([item["order"]["order_id"] for item in result["orders"]],
                             ["A", "B", "C"])
            for item in result["orders"]:
                self.assertEqual(set(item), {"order", "complete"})
                self.assertEqual(item["order"], self.app.get(item["order"]["order_id"]))
                self.assertEqual(item["complete"],
                                 self.app.history(item["order"]["order_id"])["complete"])
            self.assertEqual(result["relations"], expected_relations)
            self.assertTrue(result["complete"])

    def test_order_without_relations_returns_only_itself(self):
        self.app.place("A", [{"sku": "T", "quantity": 1}])
        result = self.app.order_relations("A")
        self.assertEqual([item["order"]["order_id"] for item in result["orders"]], ["A"])
        self.assertEqual(result["relations"], [])
        self.assertTrue(result["complete"])

    def test_disconnected_groups_do_not_mix(self):
        self._chain()
        self.app.place("X", [{"sku": "T", "quantity": 2}])
        self.app.split_order("X", "Y", [{"sku": "T", "quantity": 1}])
        result = self.app.order_relations("A")
        self.assertEqual([item["order"]["order_id"] for item in result["orders"]],
                         ["A", "B", "C"])
        result = self.app.order_relations("Y")
        self.assertEqual([item["order"]["order_id"] for item in result["orders"]],
                         ["X", "Y"])
        self.assertEqual([relation["action"] for relation in result["relations"]],
                         ["split-order"])

    def test_cycle_terminates_without_duplicate_orders(self):
        self._chain()
        # C is placed after absorbing B; merge it back into A to close a loop.
        self.app.merge_orders("C", "A")
        result = self.app.order_relations("B")
        self.assertEqual([item["order"]["order_id"] for item in result["orders"]],
                         ["A", "B", "C"])
        self.assertEqual(
            [(relation["action"], relation["source_id"], relation["target_id"])
             for relation in result["relations"]],
            [("split-order", "A", "B"), ("merge-orders", "B", "C"), ("merge-orders", "C", "A")])

    def test_relations_survive_later_state_changes(self):
        self._chain()
        # B is cancelled by the merge; shipping C and cancelling nothing else
        # must not remove any recorded relation.
        self.app.ship("C", "DHL", "1")
        result = self.app.order_relations("A")
        self.assertEqual(len(result["relations"]), 2)
        self.assertEqual([item["order"]["status"] for item in result["orders"]],
                         ["placed", "cancelled", "shipped"])

    def test_remerge_after_reopen_keeps_all_evidence_in_one_relation(self):
        self.app.place("A", [{"sku": "T", "quantity": 1}])
        self.app.place("B", [{"sku": "T", "quantity": 1}])
        self.app.merge_orders("A", "B")
        self.app.reopen_order("A")
        self.app.merge_orders("A", "B")
        result = self.app.order_relations("A")
        self.assertEqual(len(result["relations"]), 1)
        relation = result["relations"][0]
        self.assertEqual((relation["action"], relation["source_id"], relation["target_id"]),
                         ("merge-orders", "A", "B"))
        self.assertEqual(relation["evidence"], [
            {"order_id": "A", "sequence": 2},
            {"order_id": "A", "sequence": 4},
            {"order_id": "B", "sequence": 2},
            {"order_id": "B", "sequence": 3},
        ])

    def test_split_and_merge_between_same_pair_are_separate_relations(self):
        self.app.place("A", [{"sku": "T", "quantity": 3}])
        self.app.split_order("A", "B", [{"sku": "T", "quantity": 1}])
        self.app.merge_orders("B", "A")
        result = self.app.order_relations("A")
        self.assertEqual(
            [(relation["action"], relation["source_id"], relation["target_id"])
             for relation in result["relations"]],
            [("split-order", "A", "B"), ("merge-orders", "B", "A")])

    def test_input_validation(self):
        self.app.place("A", [{"sku": "T", "quantity": 1}])
        for bad in (None, 123, 1.5, b"A", ["A"], {"x": 1}, "   ", "\t\n"):
            with self.assertRaises(ValueError):
                self.app.order_relations(bad)
        with self.assertRaises(ValueError):
            self.app.order_relations("unknown")
        # Surrounding whitespace is trimmed; ids are case sensitive.
        self.assertEqual(self.app.order_relations("  A  ")["order_id"], "A")
        with self.assertRaises(ValueError):
            self.app.order_relations("a")

    def test_query_does_not_write(self):
        self._chain()
        raw = self.app.path.read_bytes()
        self.app.order_relations("A")
        self.app.order_relations("  B  ")
        self.assertEqual(self.app.path.read_bytes(), raw)

    def test_query_creates_no_file_for_unknown_order(self):
        empty = self.root / "empty"
        app = OrderDesk(empty)
        with self.assertRaises(ValueError):
            app.order_relations("ghost")
        self.assertFalse(empty.exists())

    def _write_legacy(self, data):
        self.root.mkdir(parents=True, exist_ok=True)
        OrderDesk(self.root).path.write_text(json.dumps(data), encoding="utf-8")

    @staticmethod
    def _order(order_id, status="placed"):
        return {"order_id": order_id, "status": status,
                "lines": [{"sku": "T", "quantity": 1, "unit_price_cents": 100,
                           "subtotal_cents": 100}],
                "total_cents": 100}

    def _split_event(self, sequence, source_id, target_id):
        return {"sequence": sequence, "action": "split-order",
                "result": {"source": self._order(source_id), "target": self._order(target_id)}}

    def test_legacy_order_without_history_gets_nothing_fabricated(self):
        self._write_legacy({"orders": {"OLD": self._order("OLD", "shipped")}})
        result = OrderDesk(self.root).order_relations("OLD")
        self.assertEqual(result["orders"], [{"order": self._order("OLD", "shipped"),
                                             "complete": False}])
        self.assertEqual(result["relations"], [])
        self.assertFalse(result["complete"])

    def test_one_sided_event_still_links_both_ends(self):
        data = {"orders": {"A": self._order("A"), "B": self._order("B")},
                "history": {"A": {"complete": False, "events": [self._split_event(1, "A", "B")]}}}
        self._write_legacy(data)
        app = OrderDesk(self.root)
        for query in ("A", "B"):
            result = app.order_relations(query)
            self.assertEqual([item["order"]["order_id"] for item in result["orders"]],
                             ["A", "B"])
            self.assertEqual(result["relations"], [
                {"action": "split-order", "source_id": "A", "target_id": "B",
                 "evidence": [{"order_id": "A", "sequence": 1}]}])
            # B has no history: the group is incomplete.
            self.assertFalse(result["complete"])
            self.assertEqual([item["complete"] for item in result["orders"]], [False, False])

    def test_snapshot_ids_are_normalized_like_query_ids(self):
        event = self._split_event(1, " A ", "B")
        data = {"orders": {"A": self._order("A"), "B": self._order("B")},
                "history": {"A": {"complete": True, "events": [event]}}}
        self._write_legacy(data)
        result = OrderDesk(self.root).order_relations("A")
        self.assertEqual(result["relations"][0]["source_id"], "A")

    def test_malformed_split_merge_events_reject_the_whole_query(self):
        good = self._split_event(1, "A", "B")
        cases = []
        # result is not an object.
        cases.append({"sequence": 1, "action": "split-order", "result": None})
        # source/target are not objects.
        cases.append({"sequence": 1, "action": "merge-orders",
                      "result": {"source": "A", "target": {}}})
        # Snapshot id is invalid.
        bad_id = self._split_event(1, "A", "B")
        bad_id["result"]["target"]["order_id"] = "   "
        cases.append(bad_id)
        # Both ends are the same order.
        cases.append(self._split_event(1, "A", "A"))
        # The history owner is not one of the two ends.
        cases.append(self._split_event(1, "B", "C"))
        # An end points to an unknown order.
        cases.append(self._split_event(1, "A", "Z"))
        for event in cases:
            data = {"orders": {"A": self._order("A"), "B": self._order("B"),
                               "C": self._order("C")},
                    "history": {"A": {"complete": False, "events": [event]}}}
            self._write_legacy(data)
            with self.assertRaises(ValueError, msg=event.get("action")):
                OrderDesk(self.root).order_relations("A")
        # A well-formed event still works after the failures.
        self._write_legacy({"orders": {"A": self._order("A"), "B": self._order("B")},
                            "history": {"A": {"complete": True, "events": [good]}}})
        self.assertEqual(len(OrderDesk(self.root).order_relations("A")["relations"]), 1)

    def test_malformed_event_outside_the_group_still_rejects(self):
        self._chain()
        data = json.loads(self.app.path.read_text(encoding="utf-8"))
        data["orders"]["Z"] = self._order("Z")
        data["history"]["Z"] = {"complete": False,
                                "events": [{"sequence": 1, "action": "split-order",
                                            "result": {"source": self._order("Z"),
                                                       "target": self._order("ghost")}}]}
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            OrderDesk(self.root).order_relations("A")

    def test_cli_success_and_failure(self):
        self._chain()
        payload = self.root / "q.json"
        payload.write_text(json.dumps({"order_id": " B "}), encoding="utf-8")
        ok = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                             "order-relations", str(payload)],
                            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual(set(result), {"order_id", "orders", "relations", "complete"})
        self.assertEqual(result["order_id"], "B")
        self.assertEqual(len(result["orders"]), 3)
        for bad in ("unknown", "   "):
            payload.write_text(json.dumps({"order_id": bad}), encoding="utf-8")
            failed = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                                     "order-relations", str(payload)],
                                    text=True, capture_output=True)
            self.assertEqual(failed.returncode, 2, failed.stdout)
            self.assertIn("error", json.loads(failed.stderr))

    def test_cli_array_processes_each_entry_independently(self):
        self._chain()
        self.app.place("S", [{"sku": "T", "quantity": 1}])
        payload = self.root / "batch.json"
        payload.write_text(json.dumps([{"order_id": "A"}, {"order_id": "S"}]), encoding="utf-8")
        run = subprocess.run([sys.executable, "-m", "order_desk", "--root", str(self.root),
                              "order-relations", str(payload)],
                             text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        results = json.loads(run.stdout)
        self.assertEqual([len(item["orders"]) for item in results], [3, 1])

if __name__ == "__main__":
    unittest.main()
