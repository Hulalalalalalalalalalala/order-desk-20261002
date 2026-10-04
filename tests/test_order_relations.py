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
        self.app.restock("T", 30)

    def _chain(self):
        # A splits out B, B merges into C.
        self.app.place("A", [{"sku": "T", "quantity": 5}])
        self.app.split_order("A", "B", [{"sku": "T", "quantity": 2}])
        self.app.place("C", [{"sku": "T", "quantity": 1}])
        self.app.merge_orders("B", "C")

    def test_result_shape_and_transitive_two_way_trace(self):
        self._chain()
        for query in ("A", " B ", "B", "C"):
            result = self.app.order_relations(query)
            self.assertEqual(set(result), {"order_id", "orders", "relations", "complete"})
            self.assertEqual(result["order_id"], query.strip())
            self.assertEqual([item["order"]["order_id"] for item in result["orders"]], ["A", "B", "C"])
            for item in result["orders"]:
                self.assertEqual(set(item), {"order", "complete"})
                self.assertEqual(item["order"], self.app.get(item["order"]["order_id"]))
                self.assertTrue(item["complete"])
            self.assertTrue(result["complete"])
            self.assertEqual(
                [(r["action"], r["source_id"], r["target_id"]) for r in result["relations"]],
                [("split-order", "A", "B"), ("merge-orders", "B", "C")],
            )
            for relation in result["relations"]:
                self.assertEqual(set(relation), {"action", "source_id", "target_id", "evidence"})
                for evidence in relation["evidence"]:
                    self.assertEqual(set(evidence), {"order_id", "sequence"})

    def test_evidence_keeps_mirror_records_and_sequences(self):
        self._chain()
        result = self.app.order_relations("A")
        split, merge = result["relations"]
        self.assertEqual(
            [(e["order_id"], e["sequence"]) for e in split["evidence"]],
            [("A", 2), ("B", 1)],
        )
        self.assertEqual(
            [(e["order_id"], e["sequence"]) for e in merge["evidence"]],
            [("B", 2), ("C", 2)],
        )

    def test_re_merge_after_reopen_adds_evidence_but_keeps_old_relation(self):
        self._chain()
        self.app.reopen_order("B")
        self.app.place("D", [{"sku": "T", "quantity": 1}])
        self.app.merge_orders("B", "D")
        result = self.app.order_relations("D")
        self.assertEqual([o["order"]["order_id"] for o in result["orders"]], ["A", "B", "C", "D"])
        pairs = {(r["source_id"], r["target_id"]): r for r in result["relations"]}
        self.assertEqual(set(pairs), {("A", "B"), ("B", "C"), ("B", "D")})
        # B's history: split(1), first merge(2), reopen(3), second merge(4).
        self.assertEqual(
            [(e["order_id"], e["sequence"]) for e in pairs[("B", "D")]["evidence"]],
            [("B", 4), ("D", 2)],
        )
        self.assertEqual(
            [(e["order_id"], e["sequence"]) for e in pairs[("B", "C")]["evidence"]],
            [("B", 2), ("C", 2)],
        )

    def test_cycle_does_not_repeat_orders_or_loop(self):
        # Triangle: A splits B and C, then B merges into C.
        self.app.place("A", [{"sku": "T", "quantity": 6}])
        self.app.split_order("A", "B", [{"sku": "T", "quantity": 2}])
        self.app.split_order("A", "C", [{"sku": "T", "quantity": 1}])
        self.app.merge_orders("B", "C")
        result = self.app.order_relations("B")
        self.assertEqual([o["order"]["order_id"] for o in result["orders"]], ["A", "B", "C"])
        self.assertEqual(len(result["relations"]), 3)

    def test_unrelated_order_returns_only_itself(self):
        self._chain()
        self.app.place("Z", [{"sku": "T", "quantity": 1}])
        result = self.app.order_relations("Z")
        self.assertEqual(result, {
            "order_id": "Z",
            "orders": [{"order": self.app.get("Z"), "complete": True}],
            "relations": [],
            "complete": True,
        })

    def test_other_actions_establish_no_relation(self):
        self.app.place("A", [{"sku": "T", "quantity": 1}])
        self.app.cancel("A")
        result = self.app.order_relations("A")
        self.assertEqual([o["order"]["order_id"] for o in result["orders"]], ["A"])
        self.assertEqual(result["relations"], [])

    def test_missing_history_builds_no_relation_and_marks_incomplete(self):
        line = {"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100}
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "orders": {
                "L1": {"order_id": "L1", "status": "placed",
                       "lines": [dict(line, quantity=2, subtotal_cents=200)], "total_cents": 200},
                "L2": {"order_id": "L2", "status": "placed", "lines": [line], "total_cents": 100},
            },
            "history": {"L1": {"complete": False, "events": []}},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.app.order_relations("L1")["relations"], [])
        # A relation event recorded on only one side is still kept.
        data["history"]["L1"]["events"].append({
            "sequence": 1, "action": "merge-orders",
            "result": {"source": data["orders"]["L1"], "target": data["orders"]["L2"]},
        })
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        result = self.app.order_relations("L2")
        self.assertEqual([o["order"]["order_id"] for o in result["orders"]], ["L1", "L2"])
        self.assertFalse(result["complete"])
        self.assertEqual({o["order"]["order_id"]: o["complete"] for o in result["orders"]},
                         {"L1": False, "L2": False})
        self.assertEqual(len(result["relations"]), 1)
        self.assertEqual(
            [(e["order_id"], e["sequence"]) for e in result["relations"][0]["evidence"]],
            [("L1", 1)],
        )

    def test_input_validation(self):
        self.app.place("O1", [{"sku": "T", "quantity": 1}])
        for bad in (None, 123, 1.5, b"O1", ["O1"], {"x": 1}, "   ", "\t\n"):
            with self.assertRaises(ValueError):
                self.app.order_relations(bad)
        with self.assertRaises(ValueError):
            self.app.order_relations("unknown")
        with self.assertRaises(ValueError):
            self.app.order_relations("o1")
        self.assertEqual(self.app.order_relations("  O1  ")["order_id"], "O1")

    def test_corrupt_relation_events_raise_and_return_nothing_partial(self):
        line = {"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100}
        base = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "orders": {
                "L1": {"order_id": "L1", "status": "placed",
                       "lines": [dict(line, quantity=2, subtotal_cents=200)], "total_cents": 200},
                "L2": {"order_id": "L2", "status": "placed", "lines": [line], "total_cents": 100},
            },
            "history": {"L1": {"complete": False, "events": []}},
        }
        base["history"]["L1"]["events"].append({
            "sequence": 1, "action": "merge-orders",
            "result": {"source": base["orders"]["L1"], "target": base["orders"]["L2"]},
        })

        def check(mutate):
            data = json.loads(json.dumps(base))
            mutate(data)
            self.app.path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError):
                self.app.order_relations("L1")

        check(lambda d: d["history"]["L1"]["events"][0].__setitem__("result", []))
        check(lambda d: d["history"]["L1"]["events"][0]["result"].__setitem__("source", None))
        check(lambda d: d["history"]["L1"]["events"][0]["result"].__setitem__("target", {}))
        check(lambda d: d["history"]["L1"]["events"][0]["result"]["source"].__setitem__("order_id", 123))
        check(lambda d: d["history"]["L1"]["events"][0]["result"]["source"].__setitem__("order_id", "  "))
        check(lambda d: d["history"]["L1"]["events"][0]["result"]["target"].__setitem__("order_id", True))
        check(lambda d: d["history"]["L1"]["events"][0]["result"]["target"].__setitem__("order_id", "L1"))
        check(lambda d: d["history"]["L1"]["events"][0]["result"]["target"].__setitem__("order_id", "GHOST"))

        def under_unrelated_owner(d):
            d["orders"]["Z"] = {"order_id": "Z", "status": "placed", "lines": [line], "total_cents": 100}
            d["history"]["Z"] = {"complete": True, "events": [d["history"]["L1"]["events"][0]]}
        check(under_unrelated_owner)

        # A corrupt event anywhere fails even a query on an unrelated order.
        data = json.loads(json.dumps(base))
        data["orders"]["Z"] = {"order_id": "Z", "status": "placed", "lines": [line], "total_cents": 100}
        data["history"]["Z"] = {"complete": True, "events": [{
            "sequence": 1, "action": "split-order",
            "result": {"source": dict(data["orders"]["L1"], order_id=7), "target": data["orders"]["Z"]},
        }]}
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.app.order_relations("L2")

    def test_snapshot_ids_are_trimmed_and_case_sensitive(self):
        line = {"sku": "T", "quantity": 1, "unit_price_cents": 100, "subtotal_cents": 100}
        data = {
            "products": {"T": {"sku": "T", "name": "Tea", "price_cents": 100}},
            "orders": {
                "L1": {"order_id": "L1", "status": "placed", "lines": [line], "total_cents": 100},
                "L2": {"order_id": "L2", "status": "placed", "lines": [line], "total_cents": 100},
            },
            "history": {"L1": {"complete": False, "events": [{
                "sequence": 1, "action": "merge-orders",
                "result": {"source": {"order_id": " L1 "}, "target": {"order_id": "\tL2\t"}},
            }]}},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        self.app.path.write_text(json.dumps(data), encoding="utf-8")
        relation = self.app.order_relations("L1")["relations"][0]
        self.assertEqual((relation["source_id"], relation["target_id"]), ("L1", "L2"))

    def test_query_does_not_write_and_persists_across_reopen(self):
        self._chain()
        raw = self.app.path.read_bytes()
        self.app.order_relations("A")
        self.app.order_relations("  C  ")
        self.assertEqual(self.app.path.read_bytes(), raw)
        reopened = OrderDesk(self.root).order_relations("B")
        self.assertEqual([o["order"]["order_id"] for o in reopened["orders"]], ["A", "B", "C"])

    def test_query_creates_no_file_for_unknown_order(self):
        empty = self.root / "empty"
        app = OrderDesk(empty)
        with self.assertRaises(ValueError):
            app.order_relations("ghost")
        self.assertFalse(empty.exists())

    def test_cli_success_and_failure(self):
        self._chain()
        payload = self.root / "r.json"
        payload.write_text(json.dumps({"order_id": " A "}), encoding="utf-8")
        ok = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "order-relations", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual(set(result), {"order_id", "orders", "relations", "complete"})
        self.assertEqual([o["order"]["order_id"] for o in result["orders"]], ["A", "B", "C"])
        for bad in ("unknown", "   "):
            payload.write_text(json.dumps({"order_id": bad}), encoding="utf-8")
            failed = subprocess.run(
                [sys.executable, "-m", "order_desk", "--root", str(self.root), "order-relations", str(payload)],
                text=True, capture_output=True)
            self.assertEqual(failed.returncode, 2, failed.stdout)
            self.assertIn("error", json.loads(failed.stderr))

    def test_cli_array_processes_each_item(self):
        self._chain()
        payload = self.root / "batch.json"
        payload.write_text(json.dumps([{"order_id": "A"}, {"order_id": "C"}]), encoding="utf-8")
        run = subprocess.run(
            [sys.executable, "-m", "order_desk", "--root", str(self.root), "order-relations", str(payload)],
            text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        results = json.loads(run.stdout)
        self.assertEqual([r["order_id"] for r in results], ["A", "C"])
        self.assertTrue(all(len(r["relations"]) == 2 for r in results))


if __name__ == "__main__":
    unittest.main()
