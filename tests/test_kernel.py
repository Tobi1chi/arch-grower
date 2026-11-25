"""Tests for arch_engine.kernel (standard library only).

Run from the repository root:
    python3 -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
os.environ.setdefault("ARCH_INVARIANTS", "strict")

import arch_cli  # noqa: E402
from arch_engine import ArchEngineError  # noqa: E402
from arch_engine.kernel import GraphTx  # noqa: E402


class KernelTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._old_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        arch_cli.op_init("kernel seed")
        arch_cli.op_config(mode="advanced", max_active_holes=9)

    def tearDown(self) -> None:
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    @staticmethod
    def disk() -> dict:
        return json.loads(Path(".arch/graph.json").read_text(encoding="utf-8"))


class GraphTxTest(KernelTestCase):
    def test_nothing_written_before_commit(self) -> None:
        before = self.disk()
        tx = GraphTx()
        tx.root.get("entry_input")["label"] = "changed"
        tx.bump()
        self.assertEqual(self.disk(), before)

    def test_commit_writes_and_bumps(self) -> None:
        tx = GraphTx()
        step = tx.bump()
        tx.root.get("entry_input")["label"] = "changed"
        tx.commit(f"t-{step}", "test")
        self.assertEqual(self.disk()["step"], step)
        self.assertEqual(self.disk()["nodes"][0]["label"], "changed")

    def test_strict_commit_overrides_warn_mode(self) -> None:
        tx = GraphTx()
        tx.root.raw["edges"].append({"from": "entry_input", "to": "ghost", "contract": "TBD"})
        with mock.patch.dict(os.environ, {"ARCH_INVARIANTS": "warn"}):
            with self.assertRaises(ArchEngineError):
                tx.commit("t", "bad", strict=True)

    def test_rejected_reset_leaves_graveyard_untouched(self) -> None:
        # reset-node writes a graveyard record; it must not land when the
        # graph change is rejected.
        arch_cli.op_collapse("core_transform", "Core", "spec")
        records_before = Path(".arch/records.json").read_text(encoding="utf-8")
        graph_before = self.disk()
        with mock.patch("arch_engine.kernel.enforce", side_effect=ArchEngineError("boom")):
            with self.assertRaises(ArchEngineError):
                arch_cli.op_reset_node("core_transform", "try again")
        self.assertEqual(Path(".arch/records.json").read_text(encoding="utf-8"), records_before)
        self.assertEqual(self.disk(), graph_before)


class ContainerTest(KernelTestCase):
    def test_set_edges_merges_bidirectional_pairs(self) -> None:
        tx = GraphTx()
        c = tx.root
        c.add_edge({"from": "final_output", "to": "core_transform", "contract": "s", "kind": "bidirectional"})
        between = c.edges_between("core_transform", "final_output")
        self.assertEqual(len(between), 1)
        self.assertEqual(between[0]["kind"], "bidirectional")

    def test_link_skips_covered_direction(self) -> None:
        c = GraphTx().root
        self.assertFalse(c.link("entry_input", "core_transform"))
        self.assertTrue(c.link("final_output", "entry_input", kind="feedback"))
        self.assertFalse(c.link("final_output", "entry_input"))

    def test_insert_node_rejects_duplicate_id_anywhere(self) -> None:
        arch_cli.op_promote("core_transform", topology="pipeline")
        c = GraphTx().root
        with self.assertRaises(ArchEngineError):
            c.insert_node({"id": "core_transform_core", "label": "x", "status": "hole"})

    def test_ancestors_nearest_first(self) -> None:
        arch_cli.op_promote("core_transform", topology="pipeline")
        arch_cli.op_promote("core_transform_core", topology="pipeline", scope="core_transform")
        tx = GraphTx()
        inner = tx.container("core_transform_core")
        self.assertEqual(inner.path, ["core_transform", "core_transform_core"])
        self.assertEqual([a.id for a in inner.ancestors()], ["core_transform", ""])

    def test_replace_node_refs_covers_ports_and_members(self) -> None:
        arch_cli.op_promote("core_transform", topology="pipeline")
        tx = GraphTx()
        tx.replace_node_refs("core_transform_in", "renamed_in")
        sub = tx.container("core_transform")
        self.assertEqual(sub.raw["entry_children"], ["renamed_in"])
        self.assertTrue(any(e["from"] == "renamed_in" for e in sub.edges))


def _edge_set(edges) -> list:
    return sorted((e["from"], e["to"], e.get("kind", "primary"), e["contract"]) for e in edges)


class FoldRoundTripTest(KernelTestCase):
    def settle(self, *ids: str, scope: str = "") -> None:
        for nid in ids:
            arch_cli.op_collapse(nid, nid, "spec", scope=scope)

    def test_fold_then_unfold_restores_structure(self) -> None:
        self.settle("entry_input", "core_transform", "final_output")
        arch_cli.op_add_node("audit", "Audit", "spec", status="solid", after_node="core_transform")
        arch_cli.op_connect("final_output", "entry_input", "retry", kind="feedback")
        before = self.disk()
        arch_cli.op_fold(["core_transform", "final_output"], "back", "后端", "spec")
        arch_cli.op_unfold("back")
        after = self.disk()
        self.assertEqual(_edge_set(after["edges"]), _edge_set(before["edges"]))
        self.assertEqual([n["id"] for n in after["nodes"]], [n["id"] for n in before["nodes"]])

    def test_unfold_inner_then_outer_keeps_real_endpoints(self) -> None:
        # Bug D: unfolding a subsystem nested inside a fold left the outer
        # boundary edges' provenance pointing at the vanished subsystem.
        arch_cli.op_promote("core_transform", topology="pipeline")
        self.settle("core_transform_in", "core_transform_core", "core_transform_out", scope="core_transform")
        self.settle("entry_input", "final_output")
        arch_cli.op_fold(["entry_input", "core_transform"], "front", "前端", "spec")

        arch_cli.op_unfold("core_transform", scope="front")
        [boundary] = [e for e in self.disk()["edges"] if e["from"] == "front"]
        self.assertEqual({m["from"] for m in boundary.get("members", [boundary])}, {"core_transform_out"})

        arch_cli.op_unfold("front")
        pairs = {(e["from"], e["to"]) for e in self.disk()["edges"]}
        self.assertIn(("entry_input", "core_transform_in"), pairs)
        self.assertIn(("core_transform_out", "final_output"), pairs)

    def test_unfold_inner_updates_outer_parent_ports(self) -> None:
        # The outer subsystem's saved parent ports named the inner one; after
        # the inner unfold they must name its real port child.
        arch_cli.op_config(max_depth=3)
        arch_cli.op_promote("core_transform", topology="pipeline")
        self.settle("core_transform_in", "core_transform_core", "core_transform_out", scope="core_transform")
        arch_cli.op_promote("core_transform_core", topology="pipeline", scope="core_transform")
        sub = "core_transform_core"
        self.settle(f"{sub}_in", f"{sub}_core", f"{sub}_out", scope=sub)
        arch_cli.op_fold(["core_transform_in", sub], "head", "头部", "spec", scope="core_transform")
        head = GraphTx().find_node("head")
        self.assertEqual(head["parent_ports"]["entry_children"], ["core_transform_in"])

        arch_cli.op_unfold(sub, scope="head")
        tx = GraphTx()
        for c in tx.containers():
            for ids in (c.raw.get("parent_ports") or {}).values():
                self.assertNotIn(sub, ids)

    def test_unfold_deep_node_updates_ancestor_parent_ports(self) -> None:
        # An outer subsystem's parent_ports keep naming a node after it is
        # folded deeper; unfolding that node must update them (found by fuzzing).
        arch_cli.op_config(max_depth=5)
        arch_cli.op_promote("core_transform", topology="pipeline")
        self.settle("core_transform_in", "core_transform_core", scope="core_transform")
        arch_cli.op_promote("core_transform_out", topology="pipeline", scope="core_transform")
        out = "core_transform_out"
        self.settle(f"{out}_in", f"{out}_core", f"{out}_out", scope=out)
        arch_cli.op_fold(["core_transform_core", out], "A", "A", "spec", scope="core_transform")
        self.assertEqual(GraphTx().find_node("A")["parent_ports"], {"exit_children": [out]})
        arch_cli.op_fold(["core_transform_core", out], "B", "B", "spec", scope="A")

        arch_cli.op_unfold(out, scope="B")
        self.assertEqual(GraphTx().find_node("A")["parent_ports"], {"exit_children": [f"{out}_out"]})

        # And the outer unfolds still restore the subsystem's exit port.
        arch_cli.op_unfold("B", scope="A")
        arch_cli.op_unfold("A", scope="core_transform")
        self.assertEqual(GraphTx().find_node("core_transform")["exit_children"], [f"{out}_out"])

    def test_unfold_reversed_merged_link_routes_each_flow(self) -> None:
        # The merged link is stored `final_output <-> core_transform` but holds
        # `core_transform -> final_output` too; each member must be routed by
        # its own direction (found by fuzzing).
        arch_cli.op_promote("core_transform", topology="pipeline")
        arch_cli.op_connect("final_output", "core_transform", "sync", kind="bidirectional")
        arch_cli.op_unfold("core_transform")
        kinds = {(e["from"], e["to"]): e.get("kind", "primary") for e in self.disk()["edges"]}
        self.assertEqual(kinds[("core_transform_out", "final_output")], "primary")
        self.assertEqual(kinds[("final_output", "core_transform_in")], "bidirectional")

    def test_ambiguous_outer_provenance_asks_for_attach(self) -> None:
        self.settle("entry_input", "core_transform", "final_output")
        arch_cli.op_add_node("side", "Side", "spec", status="solid", after_node="core_transform")
        arch_cli.op_fold(["core_transform", "side"], "mid", "中段", "spec")
        arch_cli.op_fold(["mid", "final_output"], "tail", "尾段", "spec")
        g = self.disk()
        [inbound] = [e for e in g["edges"] if e["to"] == "tail"]
        # An outer member naming `mid` itself (as a promoted subsystem's port
        # would), with `mid` offering two entry children: ambiguous.
        inbound["members"] = [{"from": "entry_input", "to": "mid", "contract": "late"}]
        inbound["auto_contract"] = inbound["contract"] = "late"
        mid = next(n for n in g["nodes"][1]["children"] if n["id"] == "mid")
        mid["entry_children"] = ["core_transform", "side"]
        Path(".arch/graph.json").write_text(json.dumps(g), encoding="utf-8")

        with self.assertRaises(ArchEngineError) as ctx:
            arch_cli.op_unfold("mid", scope="tail")
        self.assertIn("root 层连线的溯源", str(ctx.exception))
        self.assertEqual(json.loads(Path(".arch/graph.json").read_text(encoding="utf-8")), g)

        arch_cli.op_unfold("mid", {"entry_input": "core_transform"}, scope="tail")
        [inbound] = [e for e in self.disk()["edges"] if e["to"] == "tail"]
        self.assertEqual([m["to"] for m in inbound["members"]], ["core_transform"])


if __name__ == "__main__":
    unittest.main()
