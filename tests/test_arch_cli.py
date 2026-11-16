"""Regression tests for the arch_cli engine (standard library only).

Run from the repository root:
    python3 -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import arch_cli  # noqa: E402


class EngineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._old_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        arch_cli.op_init("test seed")

    def tearDown(self) -> None:
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def collapse(self, node_id: str) -> str:
        return arch_cli.op_collapse(node_id, f"{node_id} done", "spec")


class WalkingSkeletonTest(EngineTestCase):
    def test_not_ready_while_final_output_is_hole(self) -> None:
        self.collapse("entry_input")
        out = self.collapse("core_transform")
        self.assertNotIn("WALKING_SKELETON_READY", out)

    def test_not_ready_while_entry_is_hole(self) -> None:
        self.collapse("core_transform")
        out = self.collapse("final_output")
        self.assertNotIn("WALKING_SKELETON_READY", out)

    def test_ready_when_whole_trunk_is_solid(self) -> None:
        self.collapse("entry_input")
        self.collapse("core_transform")
        out = self.collapse("final_output")
        self.assertIn(
            "[WALKING_SKELETON_READY] 检测到无空洞可运行主干: "
            "entry_input -> core_transform -> final_output",
            out,
        )

    def test_fog_branch_does_not_block_skeleton(self) -> None:
        arch_cli.op_add_node(
            "fog_backup", "云端备份", "later", status="fog", after_node="final_output"
        )
        for nid in ("entry_input", "core_transform", "final_output"):
            out = self.collapse(nid)
        self.assertIn("WALKING_SKELETON_READY", out)

    def test_parallel_hole_branch_does_not_block_solid_path(self) -> None:
        # A Fan-out sibling that is still a hole should not hide a finished trunk.
        self.collapse("entry_input")  # stay under MAX_ACTIVE_HOLES before adding
        arch_cli.op_add_node("wav_dump", "? 录音落盘", "tbd", after_node="core_transform")
        for nid in ("core_transform", "final_output"):
            out = self.collapse(nid)
        self.assertIn("entry_input -> core_transform -> final_output", out)


class FocusOrderTest(EngineTestCase):
    def test_focus_picks_most_upstream_hole(self) -> None:
        # Insert a hole between entry and core; it is appended last in the node
        # list but sits upstream of core_transform in the graph.
        self.collapse("entry_input")
        arch_cli.op_add_node(
            "decoder", "? 解码", "tbd", after_node="entry_input", before_node="core_transform"
        )
        out = arch_cli.op_focus()
        self.assertIn('"id": "decoder"', out.split('"focus_node"')[1].split("}")[0])


class SignatureDetectionTest(unittest.TestCase):
    def check(self, line: str) -> bool:
        return bool(arch_cli.SIGNATURE_LINE.match(line))

    def test_detects_declarations_across_languages(self) -> None:
        for line in (
            "+def read(frame):",
            "-    async def read(frame):",
            "+export function encode(x) {",
            "+export default class Router {",
            "+func Encode(b []byte) error {",
            "+pub fn encode(b: &[u8]) -> Vec<u8> {",
            "+pub(crate) struct Frame {",
            "+public interface Port {",
            "+type Frame = { ts: number }",
        ):
            self.assertTrue(self.check(line), line)

    def test_ignores_keywords_inside_ordinary_lines(self) -> None:
        for line in (
            '+    print("def is a keyword")',
            "+    node_type = 'port'",
            "+    # refactor: function signature unchanged",
            "+    return classify(x)",
        ):
            self.assertFalse(self.check(line), line)


if __name__ == "__main__":
    unittest.main()
