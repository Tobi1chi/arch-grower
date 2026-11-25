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


if __name__ == "__main__":
    unittest.main()
