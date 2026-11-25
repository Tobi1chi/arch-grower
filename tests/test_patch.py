"""Tests for `arch patch` (standard library only).

Run from the repository root:
    python3 -m unittest discover -s tests
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
os.environ.setdefault("ARCH_INVARIANTS", "strict")

import arch_cli  # noqa: E402
import arch_mcp  # noqa: E402
from arch_engine import ArchEngineError  # noqa: E402
from arch_engine.common import _require_command  # noqa: E402


class PatchTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._old_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        arch_cli.op_init("patch seed")
        arch_cli.op_config(mode="advanced")

    def tearDown(self) -> None:
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    @staticmethod
    def disk() -> dict:
        return json.loads(Path(".arch/graph.json").read_text(encoding="utf-8"))


class PatchTest(PatchTestCase):
    def test_applies_all_ops_in_one_snapshot(self) -> None:
        step = self.disk()["step"]
        out = arch_cli.op_patch([
            {"op": "add_node", "id": "cache", "label": "Cache", "status": "solid"},
            {"op": "add_edge", "from": "core_transform", "to": "cache", "contract": "get/put"},
            {"op": "update_node", "id": "entry_input", "set": {"label": "HTTP 入口", "status": "solid"}},
            {"op": "update_edge", "from": "entry_input", "to": "core_transform", "contract": "JSON"},
        ])
        self.assertIn(f"patch-s{step + 1:02d}", out)
        g = self.disk()
        self.assertEqual(g["step"], step + 1)
        self.assertIn("cache", {n["id"] for n in g["nodes"]})
        self.assertEqual(g["nodes"][0]["label"], "HTTP 入口")
        contracts = {(e["from"], e["to"]): e["contract"] for e in g["edges"]}
        self.assertEqual(contracts[("core_transform", "cache")], "get/put")
        self.assertEqual(contracts[("entry_input", "core_transform")], "JSON")

    def test_failing_op_writes_nothing(self) -> None:
        before = self.disk()
        with self.assertRaises(ArchEngineError) as ctx:
            arch_cli.op_patch([
                {"op": "add_node", "id": "cache", "label": "Cache"},
                {"op": "remove_edge", "from": "cache", "to": "nowhere"},
            ])
        self.assertIn("第 2 个操作", str(ctx.exception))
        self.assertEqual(self.disk(), before)

    def test_strict_even_when_env_is_warn(self) -> None:
        # Removing a subsystem's entry port child leaves entry_children dangling.
        arch_cli.op_promote("core_transform", topology="pipeline")
        before = self.disk()
        with mock.patch.dict(os.environ, {"ARCH_INVARIANTS": "warn"}):
            with self.assertRaises(ArchEngineError) as ctx:
                arch_cli.op_patch([{"op": "remove_node", "id": "core_transform_in"}], scope="core_transform")
        self.assertIn("I7", str(ctx.exception))
        self.assertEqual(self.disk(), before)

    def test_remove_node_drops_its_edges(self) -> None:
        arch_cli.op_patch([{"op": "remove_node", "id": "final_output"}])
        g = self.disk()
        self.assertNotIn("final_output", {n["id"] for n in g["nodes"]})
        self.assertFalse(any("final_output" in (e["from"], e["to"]) for e in g["edges"]))

    def test_structural_fields_and_subsystems_are_off_limits(self) -> None:
        arch_cli.op_promote("core_transform", topology="pipeline")
        for ops in (
            [{"op": "update_node", "id": "entry_input", "set": {"children": []}}],
            [{"op": "update_node", "id": "entry_input", "set": {"status": "super_node"}}],
            [{"op": "update_node", "id": "core_transform", "set": {"label": "x"}}],
            [{"op": "remove_node", "id": "core_transform"}],
            [{"op": "add_node", "id": "s", "label": "S", "type": "super_node"}],
            [{"op": "move_node", "id": "entry_input"}],
        ):
            with self.assertRaises(ArchEngineError):
                arch_cli.op_patch(ops)

    def test_add_bidirectional_edge_merges_pair(self) -> None:
        arch_cli.op_patch([{"op": "add_edge", "from": "final_output", "to": "core_transform", "kind": "bidirectional"}])
        between = [e for e in self.disk()["edges"] if {e["from"], e["to"]} == {"core_transform", "final_output"}]
        self.assertEqual(len(between), 1)
        self.assertEqual(between[0]["kind"], "bidirectional")

    def test_accepts_json_text(self) -> None:
        arch_cli.op_patch('[{"op": "add_node", "id": "x", "label": "X", "status": "fog"}]')
        self.assertIn("x", {n["id"] for n in self.disk()["nodes"]})
        with self.assertRaises(ArchEngineError):
            arch_cli.op_patch("not json")

    def test_basic_mode_refuses_patch(self) -> None:
        arch_cli.op_config(mode="basic")
        with self.assertRaises(ArchEngineError) as ctx:
            _require_command("patch")
        self.assertIn("ADVANCED_MODE_REQUIRED", str(ctx.exception))

    def test_mcp_tool(self) -> None:
        out = arch_mcp._dispatch_tool("arch_patch", {"ops": [{"op": "add_node", "id": "m", "label": "M"}]})
        self.assertIn("已在顶层应用 1 个底层操作", out)

    def test_cli_file_and_ops(self) -> None:
        Path("ops.json").write_text(json.dumps([{"op": "add_node", "id": "f", "label": "F", "status": "fog"}]),
                                    encoding="utf-8")
        cli = [sys.executable, str(SCRIPTS / "arch_cli.py")]
        res = subprocess.run(cli + ["patch", "--file", "ops.json"], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        res = subprocess.run(cli + ["patch", "--ops", '[{"op":"remove_node","id":"f"}]'],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("f", {n["id"] for n in self.disk()["nodes"]})


if __name__ == "__main__":
    unittest.main()
