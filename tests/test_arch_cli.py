"""Regression tests for the arch_cli engine (standard library only).

Run from the repository root:
    python3 -m unittest discover -s tests
"""

import json
import os
from collections import Counter
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


def _snapshot(graph):
    """Order-insensitive view of the top-level graph for round-trip checks."""
    nodes = sorted((json.dumps(n, sort_keys=True, ensure_ascii=False) for n in graph["nodes"]))
    edges = sorted((e["from"], e["to"], e.get("contract", "")) for e in graph["edges"])
    return nodes, edges


class FoldTest(EngineTestCase):
    """Trunk used below: entry_input -> decoder -> core_transform -> final_output (all solid)."""

    def setUp(self) -> None:
        super().setUp()
        for nid in ("entry_input", "core_transform", "final_output"):
            self.collapse(nid)
        arch_cli.op_add_node(
            "decoder", "解码", "spec", status="solid",
            after_node="entry_input", before_node="core_transform", contract="Frame",
        )

    def graph(self):
        return arch_cli._load_graph()

    def test_fold_replaces_members_and_keeps_skeleton(self) -> None:
        out = arch_cli.op_fold(["decoder", "core_transform"], "proc", "处理模块", "解码并翻译")
        g = self.graph()
        ids = [n["id"] for n in g["nodes"]]
        self.assertNotIn("decoder", ids)
        self.assertIn("proc", ids)
        self.assertEqual(
            sorted((e["from"], e["to"]) for e in g["edges"]),
            [("entry_input", "proc"), ("proc", "final_output")],
        )
        self.assertIn("entry_input -> proc -> final_output", out)

    def test_unfold_round_trip_is_exact(self) -> None:
        before = _snapshot(self.graph())
        arch_cli.op_fold(["decoder", "core_transform"], "proc", "处理模块", "spec")
        arch_cli.op_unfold("proc")
        self.assertEqual(_snapshot(self.graph()), before)

    def test_nested_fold_round_trip(self) -> None:
        before = _snapshot(self.graph())
        arch_cli.op_fold(["decoder", "core_transform"], "proc", "处理模块", "spec")
        arch_cli.op_fold(["entry_input", "proc"], "front", "前端", "spec")
        with self.assertRaisesRegex(arch_cli.ArchEngineError, "front"):
            arch_cli.op_collapse("decoder", "x", "y")  # points at the outermost container
        arch_cli.op_unfold("front")
        arch_cli.op_unfold("proc")
        self.assertEqual(_snapshot(self.graph()), before)

    def test_rejects_unsettled_nodes(self) -> None:
        arch_cli.op_add_node("idea", "? 想法", "tbd", after_node="final_output")
        with self.assertRaisesRegex(arch_cli.ArchEngineError, "尚未确定"):
            arch_cli.op_fold(["final_output", "idea"], "s", "l", "s")

    def test_rejects_disconnected_members(self) -> None:
        with self.assertRaisesRegex(arch_cli.ArchEngineError, "不连通"):
            arch_cli.op_fold(["entry_input", "final_output"], "s", "l", "s")

    def test_rejects_fold_that_would_create_cycle(self) -> None:
        g = self.graph()
        g["edges"].append({"from": "entry_input", "to": "core_transform", "contract": "skip"})
        arch_cli._save_graph(g)
        # entry -> decoder -> core leaves the set and comes back in.
        with self.assertRaisesRegex(arch_cli.ArchEngineError, "形成环"):
            arch_cli.op_fold(["entry_input", "core_transform"], "s", "l", "s")

    def test_contract_changed_while_folded_is_restored(self) -> None:
        arch_cli.op_fold(["decoder", "core_transform"], "proc", "处理模块", "spec")
        arch_cli.op_collapse("final_output", "输出", "spec", in_contract="Subtitle[]")
        arch_cli.op_unfold("proc")
        edge = next(e for e in self.graph()["edges"] if e["to"] == "final_output")
        self.assertEqual((edge["from"], edge["contract"]), ("core_transform", "Subtitle[]"))

    def test_edge_added_while_folded_attaches_to_unique_exit(self) -> None:
        arch_cli.op_fold(["decoder", "core_transform"], "proc", "处理模块", "spec")
        arch_cli.op_add_node("logger", "日志", "spec", status="solid", after_node="proc")
        arch_cli.op_unfold("proc")
        edges = {(e["from"], e["to"]) for e in self.graph()["edges"]}
        self.assertIn(("core_transform", "logger"), edges)

    def test_ambiguous_new_edge_needs_attach(self) -> None:
        arch_cli.op_add_node("wav", "录音", "spec", status="solid", after_node="core_transform")
        arch_cli.op_fold(["core_transform", "final_output", "wav"], "back", "后端", "spec")
        arch_cli.op_add_node("archive", "归档", "spec", status="solid", after_node="back")
        with self.assertRaisesRegex(arch_cli.ArchEngineError, "attach"):
            arch_cli.op_unfold("back")
        arch_cli.op_unfold("back", {"archive": "wav"})
        edges = {(e["from"], e["to"]) for e in self.graph()["edges"]}
        self.assertIn(("wav", "archive"), edges)

    def test_super_node_blocks_node_level_operations(self) -> None:
        arch_cli.op_fold(["decoder", "core_transform"], "proc", "处理模块", "spec")
        for call in (
            lambda: arch_cli.op_collapse("proc", "x", "y"),
            lambda: arch_cli.op_export_task("proc"),
            lambda: arch_cli.op_reset_node("proc", "why"),
            lambda: arch_cli.op_deactivate_node("proc"),
        ):
            with self.assertRaisesRegex(arch_cli.ArchEngineError, "arch_unfold"):
                call()
        with self.assertRaisesRegex(arch_cli.ArchEngineError, "已折叠在超级节点 'proc'"):
            arch_cli.op_export_task("decoder")

    def test_focus_shows_super_node_compactly(self) -> None:
        arch_cli.op_fold(["decoder", "core_transform"], "proc", "处理模块", "spec")
        out = arch_cli.op_focus("proc")
        self.assertIn('"folded_children"', out)
        self.assertNotIn('"inner_edges"', out)

    def _adjacent_modules_graph(self):
        """entry -> decoder -> core -> final, plus a skip edge decoder -> final."""
        g = self.graph()
        for e in g["edges"]:
            if (e["from"], e["to"]) == ("decoder", "core_transform"):
                e["contract"] = "decoder->core"
        g["edges"].append({"from": "decoder", "to": "final_output", "contract": "decoder->final"})
        arch_cli._save_graph(g)
        return _snapshot(self.graph())

    def _assert_plain_edges(self) -> None:
        for e in self.graph()["edges"]:
            self.assertNotIn("members", e)
            self.assertNotIn("auto_contract", e)

    def test_adjacent_folds_round_trip_in_every_order(self) -> None:
        # Review finding: fold front, fold back, unfold front, unfold back lost
        # decoder -> final and merged its contract into decoder -> core.
        before = self._adjacent_modules_graph()
        orders = [
            (("front", "back"), ("front", "back")),
            (("front", "back"), ("back", "front")),
            (("back", "front"), ("front", "back")),
            (("back", "front"), ("back", "front")),
        ]
        groups = {"front": ["entry_input", "decoder"], "back": ["core_transform", "final_output"]}
        for fold_order, unfold_order in orders:
            with self.subTest(fold=fold_order, unfold=unfold_order):
                for sid in fold_order:
                    arch_cli.op_fold(groups[sid], sid, sid, "spec")
                for sid in unfold_order:
                    arch_cli.op_unfold(sid)
                self.assertEqual(_snapshot(self.graph()), before)
                self._assert_plain_edges()

    def test_contract_edited_between_two_modules_reaches_the_original_edges(self) -> None:
        self._adjacent_modules_graph()
        arch_cli.op_fold(["entry_input", "decoder"], "front", "前端", "spec")
        arch_cli.op_fold(["core_transform", "final_output"], "back", "后端", "spec")
        g = self.graph()
        edge = next(e for e in g["edges"] if (e["from"], e["to"]) == ("front", "back"))
        edge["contract"] = "Frame v2"  # edited while both sides are folded
        arch_cli._save_graph(g)
        arch_cli.op_unfold("front")
        arch_cli.op_unfold("back")
        edges = {(e["from"], e["to"]): e["contract"] for e in self.graph()["edges"]}
        self.assertEqual(edges[("decoder", "core_transform")], "Frame v2")
        self.assertEqual(edges[("decoder", "final_output")], "Frame v2")
        self._assert_plain_edges()

    def test_edge_attached_while_folded_survives_neighbour_folds(self) -> None:
        arch_cli.op_fold(["decoder", "core_transform"], "proc", "处理", "spec")
        arch_cli.op_add_node("logger", "日志", "spec", status="solid", after_node="proc")
        arch_cli.op_add_node("sink", "落盘", "spec", status="solid", after_node="logger")
        arch_cli.op_fold(["logger", "sink"], "obs", "观测", "spec")
        arch_cli.op_unfold("proc")   # logger edge is routed to proc's only exit child
        arch_cli.op_unfold("obs")
        edges = {(e["from"], e["to"]) for e in self.graph()["edges"]}
        self.assertIn(("core_transform", "logger"), edges)
        self.assertIn(("logger", "sink"), edges)
        self._assert_plain_edges()

    def test_focus_hides_edge_bookkeeping(self) -> None:
        arch_cli.op_fold(["decoder", "core_transform"], "proc", "处理模块", "spec")
        out = arch_cli.op_focus("proc")
        self.assertNotIn('"members"', out)
        self.assertNotIn('"auto_contract"', out)

    def test_folding_the_whole_trunk_keeps_the_skeleton(self) -> None:
        # Review finding: folding a finished trunk into one module dropped
        # WALKING_SKELETON_READY although nothing was undecided.
        out = arch_cli.op_fold(
            ["entry_input", "decoder", "core_transform", "final_output"], "all", "全部", "spec"
        )
        self.assertIn("[WALKING_SKELETON_READY] 检测到无空洞可运行主干: all", out)

    def test_whole_trunk_with_a_hole_inside_is_not_a_skeleton(self) -> None:
        g = self.graph()
        g["nodes"].append({"id": "gap", "label": "? 缺口", "status": "hole", "type": "port",
                           "spec": "", "file_path": "", "last_tag": ""})
        g["edges"].append({"from": "final_output", "to": "gap", "contract": "TBD"})
        arch_cli._save_graph(g)
        arch_cli.op_fold(["entry_input", "decoder", "core_transform", "final_output"], "all", "全部", "spec")
        self.assertIsNone(arch_cli._find_walking_skeleton(self.graph()))

    def _custom_graph(self, solids, holes, edges) -> None:
        mk = lambda i, st: {"id": i, "label": i, "status": st, "type": "port",
                            "spec": "", "file_path": "", "last_tag": ""}
        g = self.graph()
        g["nodes"] = [mk(i, "solid") for i in solids] + [mk(i, "hole") for i in holes]
        g["edges"] = [{"from": u, "to": v, "contract": "c"} for u, v in edges]
        arch_cli._save_graph(g)

    def test_skeleton_must_get_through_the_module_interior(self) -> None:
        # Review finding: s -> c enters the module at c, but only a -> t leaves
        # it and there is no path c -> a inside, so nothing is runnable.
        self._custom_graph(
            ["s", "a", "c", "t"], ["h1", "h2"],
            [("s", "c"), ("h1", "a"), ("a", "c"), ("a", "t"), ("c", "h2")],
        )
        self.assertIsNone(arch_cli._find_walking_skeleton(self.graph()))
        out = arch_cli.op_fold(["a", "c"], "m", "模块", "spec")
        self.assertNotIn("WALKING_SKELETON_READY", out)
        self.assertIsNone(arch_cli._find_walking_skeleton(self.graph()))

    def test_skeleton_through_module_when_interior_connects(self) -> None:
        self._custom_graph(["s", "a", "c", "t"], [], [("s", "a"), ("a", "c"), ("c", "t")])
        arch_cli.op_fold(["a", "c"], "m", "模块", "spec")
        self.assertEqual(arch_cli._find_walking_skeleton(self.graph()), ["s", "m", "t"])
        arch_cli.op_fold(["s", "m"], "outer", "外层", "spec")  # nested fold
        self.assertEqual(arch_cli._find_walking_skeleton(self.graph()), ["outer", "t"])

    def test_ambiguous_attachment_never_reports_a_false_skeleton(self) -> None:
        # Review finding: h2 hangs off a module with two exits; dropping that
        # undecided edge made the module look like a finished trunk.
        self._custom_graph(["a", "b", "c"], ["h1"], [("a", "b"), ("a", "c"), ("c", "h1")])
        arch_cli.op_fold(["a", "b", "c"], "m", "模块", "spec")
        out = arch_cli.op_add_node("h2", "? 待定", "spec", after_node="m")
        self.assertNotIn("WALKING_SKELETON_READY", out)
        # While h2's attachment is undecided the engine stays conservative.
        self.assertIsNone(arch_cli._find_walking_skeleton(self.graph()))
        folded = self.graph()
        arch_cli.op_unfold("m", {"h2": "b"})  # both exits blocked: not ready
        self.assertIsNone(arch_cli._find_walking_skeleton(self.graph()))
        arch_cli._save_graph(folded)
        arch_cli.op_unfold("m", {"h2": "c"})  # a -> b is free: ready
        self.assertEqual(arch_cli._find_walking_skeleton(self.graph()), ["a", "b"])

    def test_bypass_around_deactivated_neighbour_keeps_the_real_child(self) -> None:
        # Review finding: a -> x -> t with x deactivated came back as b -> t.
        self._custom_graph(["a", "b", "x", "t"], ["h"], [("a", "b"), ("a", "x"), ("x", "t"), ("b", "h")])
        arch_cli.op_fold(["a", "b"], "m", "模块", "spec")
        arch_cli.op_deactivate_node("x")
        arch_cli.op_unfold("m")
        edges = {(e["from"], e["to"]) for e in self.graph()["edges"]}
        self.assertIn(("a", "t"), edges)
        self.assertNotIn(("b", "t"), edges)
        self._assert_plain_edges()

    def test_bypass_merges_into_an_existing_module_edge(self) -> None:
        # m already reaches t through b; the bypass through x adds a -> t.
        self._custom_graph(["a", "b", "x", "t"], [], [("a", "b"), ("a", "x"), ("x", "t"), ("b", "t")])
        arch_cli.op_fold(["a", "b"], "m", "模块", "spec")
        arch_cli.op_deactivate_node("x")
        self.assertEqual(sum(1 for e in self.graph()["edges"] if (e["from"], e["to"]) == ("m", "t")), 1)
        arch_cli.op_unfold("m")
        edges = {(e["from"], e["to"]) for e in self.graph()["edges"]}
        self.assertTrue({("a", "t"), ("b", "t")} <= edges)

    def test_super_type_cannot_be_set_by_hand(self) -> None:
        with self.assertRaisesRegex(arch_cli.ArchEngineError, "arch_fold"):
            arch_cli.op_add_node("fake", "假的", "spec", status="solid", node_type="super_node")


class EdgeContractTest(EngineTestCase):
    def edge(self, u: str, v: str) -> dict:
        graph = json.loads(Path(".arch/graph.json").read_text(encoding="utf-8"))
        return next(e for e in graph["edges"] if (e["from"], e["to"]) == (u, v))

    def test_updates_exactly_one_edge(self) -> None:
        arch_cli.op_set_edge_contract("entry_input", "core_transform", "PCM 16k chunk")
        self.assertEqual(self.edge("entry_input", "core_transform")["contract"], "PCM 16k chunk")
        self.assertEqual(self.edge("core_transform", "final_output")["contract"], "TBD")

    def test_same_contract_is_a_no_op(self) -> None:
        arch_cli.op_set_edge_contract("entry_input", "core_transform", "x")
        step = json.loads(Path(".arch/graph.json").read_text(encoding="utf-8"))["step"]
        self.assertIn("已使用该契约", arch_cli.op_set_edge_contract("entry_input", "core_transform", "x"))
        self.assertEqual(json.loads(Path(".arch/graph.json").read_text(encoding="utf-8"))["step"], step)

    def test_missing_edge_is_rejected(self) -> None:
        with self.assertRaisesRegex(arch_cli.ArchEngineError, "找到 0 条"):
            arch_cli.op_set_edge_contract("final_output", "entry_input", "x")


class ExportUiTest(EngineTestCase):
    def export(self) -> str:
        arch_cli.op_export_ui()
        return Path(".arch/dashboard.html").read_text(encoding="utf-8")

    def embedded(self, page: str) -> dict:
        start = page.index('<script id="arch-data" type="application/json">') + len(
            '<script id="arch-data" type="application/json">')
        return json.loads(page[start:page.index("</script>", start)])

    def test_dashboard_is_self_contained(self) -> None:
        page = self.export()
        self.assertNotIn("__ARCH_", page)
        self.assertNotRegex(page, r"(src|href)=\"https?://")

    def test_node_text_cannot_break_out_of_the_data_script(self) -> None:
        arch_cli.op_collapse("entry_input", "</script><script>alert(1)</script>", "a < b && c")
        page = self.export()
        self.assertNotIn("</script><script>alert(1)", page)
        data = self.embedded(page)
        labels = [n["label"] for n in data["graph"]["nodes"]]
        self.assertIn("</script><script>alert(1)</script>", labels)

    def test_payload_carries_engine_decisions(self) -> None:
        self.collapse("entry_input")
        data = self.embedded(self.export())
        self.assertEqual(data["next_focus"], "core_transform")
        self.assertEqual(data["topo_order"], ["entry_input", "core_transform", "final_output"])
        self.assertIsNone(data["skeleton"])
        self.assertEqual(data["seed"], "test seed")

    def test_viewport_disables_text_selection(self) -> None:
        # Drag-to-pan used to sweep a selection across the stage labels; that
        # selection becomes an HTML5 drag source, so the next pan is taken over
        # by the browser's "search for text" drop action instead of panning.
        page = self.export()
        start = page.index("#viewport {")
        rule = page[start:page.index("}", start)]
        self.assertIn("user-select: none", rule)


# ---------------------------------------------------------------------------
# Typed records, node state and context slicing
# ---------------------------------------------------------------------------

import arch_mcp  # noqa: E402


def _records() -> dict:
    return json.loads(Path(".arch/records.json").read_text(encoding="utf-8"))


def _node(node_id: str) -> dict:
    return arch_cli._find_any_node(arch_cli._load_graph(), node_id)


class NodeStateTest(EngineTestCase):
    def test_old_nodes_get_default_state(self) -> None:
        g = arch_cli._load_graph()
        for n in g["nodes"]:
            for key in ("impl", "verify", "refs"):
                n.pop(key, None)
        g["nodes"][0]["last_tag"] = "node-entry_input-s02"  # committed by an older version
        arch_cli._save_graph(g)
        self.assertEqual(arch_cli._node_state(_node("entry_input"))["impl"], "built")
        state = arch_cli._node_state(_node("core_transform"))
        self.assertEqual(state, {"impl": "planned", "verify": {"level": "none", "date": ""},
                                 "refs": [], "files": []})
        out = arch_cli.op_focus("core_transform")
        self.assertIn('"impl": "planned"', out)
        self.assertIn('"level": "none"', out)

    def test_set_node_state_records_every_field(self) -> None:
        self.collapse("entry_input")
        out = arch_cli.op_set_node_state(
            "entry_input", impl="built", verify="device", verify_date="2026-11-01",
            refs=["docs/protocol.md#6"], add_refs=["analysis/x/case.json#key"],
            file_paths=["src/ble.py", "src/ble_queue.py"],
        )
        self.assertIn("已实现 · 真机实测 2026-11-01", out)
        node = _node("entry_input")
        self.assertEqual(node["impl"], "built")
        self.assertEqual(node["verify"], {"level": "device", "date": "2026-11-01"})
        self.assertEqual(node["refs"], ["docs/protocol.md#6", "analysis/x/case.json#key"])
        self.assertEqual(node["file_path"], ["src/ble.py", "src/ble_queue.py"])
        arch_cli.op_set_node_state("entry_input", drop_refs=["docs/protocol.md#6"], file_paths=["src/ble.py"])
        node = _node("entry_input")
        self.assertEqual(node["refs"], ["analysis/x/case.json#key"])
        self.assertEqual(node["file_path"], "src/ble.py")  # one file keeps the original string form

    def test_verify_defaults_to_today_and_none_clears_date(self) -> None:
        arch_cli.op_set_node_state("entry_input", verify="unit")
        self.assertEqual(_node("entry_input")["verify"]["date"], arch_cli._today())
        arch_cli.op_set_node_state("entry_input", verify="none")
        self.assertEqual(_node("entry_input")["verify"], {"level": "none", "date": ""})

    def test_rejects_bad_values(self) -> None:
        for kwargs, msg in (
            ({"impl": "done"}, "impl"),
            ({"verify": "manual"}, "verify"),
            ({"verify": "unit", "verify_date": "11/01"}, "YYYY-MM-DD"),
            ({"drop_refs": ["nope.md"]}, "不存在"),
        ):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(arch_cli.ArchEngineError, msg):
                arch_cli.op_set_node_state("entry_input", **kwargs)

    def test_unchanged_state_does_not_take_a_step(self) -> None:
        arch_cli.op_set_node_state("entry_input", impl="building")
        step = arch_cli._load_graph()["step"]
        self.assertIn("没有变化", arch_cli.op_set_node_state("entry_input", impl="building"))
        self.assertEqual(arch_cli._load_graph()["step"], step)

    def test_folded_child_state_and_module_progress(self) -> None:
        for nid in ("entry_input", "core_transform", "final_output"):
            self.collapse(nid)
        arch_cli.op_fold(["entry_input", "core_transform"], "front", "前端", "spec")
        arch_cli.op_set_node_state("entry_input", impl="built")  # no unfold needed
        with self.assertRaisesRegex(arch_cli.ArchEngineError, "子节点"):
            arch_cli.op_set_node_state("front", impl="built")
        out = arch_cli.op_focus("front")
        self.assertIn('"impl_progress": "1/2 built"', out)
        self.assertIn("entry_input [solid] 已实现", arch_cli.op_status())

    def test_commit_node_binds_several_files_and_detects_drift_in_any(self) -> None:
        self.collapse("entry_input")
        Path("a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
        Path("b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
        out = arch_cli.op_commit_node("entry_input", ["a.py", "b.py"])
        self.assertIn("built", out)
        node = _node("entry_input")
        self.assertEqual((node["file_path"], node["impl"]), (["a.py", "b.py"], "built"))
        arch_cli.op_export_task("entry_input")  # clean baseline
        Path("b.py").write_text("def b():\n    return 3\n", encoding="utf-8")
        with self.assertRaisesRegex(arch_cli.ArchEngineError, "ManualDriftError"):
            arch_cli.op_export_task("entry_input")

    def test_reset_node_starts_progress_over(self) -> None:
        self.collapse("entry_input")
        arch_cli.op_set_node_state("entry_input", impl="built", verify="api", refs=["docs/a.md#1"])
        arch_cli.op_reset_node("entry_input", "接口不稳定")
        state = arch_cli._node_state(_node("entry_input"))
        self.assertEqual((state["impl"], state["verify"]["level"], state["refs"]),
                         ("planned", "none", ["docs/a.md#1"]))

    def test_dashboard_shows_progress(self) -> None:
        self.collapse("entry_input")
        arch_cli.op_set_node_state("entry_input", impl="built", verify="device")
        arch_cli.op_open_add("丢包率？", "device_test", "user", scope="entry_input", blocking=True)
        arch_cli.op_export_ui()
        page = Path(".arch/dashboard.html").read_text(encoding="utf-8")
        data = ExportUiTest.embedded(None, page)
        self.assertEqual(data["node_state"]["entry_input"]["impl"], "built")
        self.assertEqual(data["node_state"]["entry_input"]["verify"]["level"], "device")
        self.assertTrue(data["records"]["enabled"])
        self.assertEqual([o["id"] for o in data["records"]["open_items"]], ["OI-01"])
        for snippet in ("impl-pill", "实现与验证", "实现进度"):
            self.assertIn(snippet, page)


class OpenItemsTest(EngineTestCase):
    def test_add_list_resolve(self) -> None:
        out = arch_cli.op_open_add("BLE 实际吞吐？", "device_test", "user", scope=["entry_input"], blocking=True)
        self.assertIn("[OI-01]", out)
        arch_cli.op_open_add("选哪个 ASR 服务", "research", "agent", scope="core_transform")
        item = _records()["open_items"][0]
        self.assertEqual(
            {k: item[k] for k in ("id", "scope", "kind", "owner", "blocking", "status")},
            {"id": "OI-01", "scope": ["entry_input"], "kind": "device_test", "owner": "user",
             "blocking": True, "status": "open"},
        )
        self.assertIn("OI-02", arch_cli.op_open_list(owner="agent"))
        self.assertNotIn("OI-02", arch_cli.op_open_list(blocking_only=True))
        self.assertNotIn("OI-01", arch_cli.op_open_list(node_id="core_transform"))

        out = arch_cli.op_open_resolve("OI-01", "实测 1.2 Mbps", constraint="单路 16k PCM 足够")
        self.assertIn("沉淀为事实", out)
        fact = _records()["facts"][-1]
        self.assertEqual((fact["scope"], fact["source"], fact["text"]),
                         (["entry_input"], "待办项 OI-01", "实测 1.2 Mbps"))
        self.assertNotIn("OI-01", arch_cli.op_open_list())
        self.assertIn("已解决", arch_cli.op_open_list(include_resolved=True))
        with self.assertRaisesRegex(arch_cli.ArchEngineError, "已在"):
            arch_cli.op_open_resolve("OI-01", "again")
        md = Path(".arch/open_items.md").read_text(encoding="utf-8")
        self.assertIn(arch_cli.GENERATED_MARKER, md)
        self.assertIn("## 已解决", md)

    def test_stage_scope_matches_nodes_of_that_stage(self) -> None:
        arch_cli.op_collapse("entry_input", "输入", "spec", stage="S1 采集")
        arch_cli.op_open_add("采样率？", "decision", "user", scope="S1 采集")
        self.assertIn("采样率", arch_cli.op_open_list(node_id="entry_input"))
        self.assertIn("采样率", arch_cli.op_focus("entry_input"))

    def test_rejects_bad_input(self) -> None:
        with self.assertRaisesRegex(arch_cli.ArchEngineError, "类型"):
            arch_cli.op_open_add("q", "guess", "user")
        with self.assertRaisesRegex(arch_cli.ArchEngineError, "处理方"):
            arch_cli.op_open_add("q", "research", "bot")
        with self.assertRaisesRegex(arch_cli.ArchEngineError, "未知作用域"):
            arch_cli.op_open_add("q", "research", "user", scope="no_such_node")
        with self.assertRaisesRegex(arch_cli.ArchEngineError, "混用"):
            arch_cli.op_open_add("q", "research", "user", scope=["global", "entry_input"])


class FactsTest(EngineTestCase):
    def test_scope_source_and_date_are_stored(self) -> None:
        arch_cli.op_verify("MTU 185", "分片 ≤ 180", scope="entry_input", source="analysis/mtu/case.json#mtu")
        fact = _records()["facts"][0]
        self.assertEqual((fact["id"], fact["scope"], fact["source"], fact["date"]),
                         ("GT-01", ["entry_input"], "analysis/mtu/case.json#mtu", arch_cli._today()))
        md = Path(".arch/ground_truth.md").read_text(encoding="utf-8")
        self.assertTrue(md.startswith(arch_cli.GENERATED_MARKER))
        self.assertIn("## 作用域 · entry_input", md)

    def test_superseded_facts_are_hidden_by_default(self) -> None:
        arch_cli.op_verify("旧板子 2 路 UART", "日志走 RTT", scope="entry_input")
        arch_cli.op_verify("新板子 4 路 UART", "日志走 UART3", supersedes="GT-01")
        facts = _records()["facts"]
        self.assertEqual(facts[1]["scope"], ["entry_input"])  # inherited from GT-01
        self.assertEqual(facts[1]["supersedes"], ["GT-01"])
        focus = arch_cli.op_focus("entry_input")
        self.assertIn("GT-02", focus)
        self.assertNotIn("GT-01]", focus)
        self.assertNotIn("GT-01]", arch_cli.op_facts(node_id="entry_input"))
        self.assertIn("已被 GT-02 取代", arch_cli.op_facts(all_scopes=True, with_superseded=True))
        md = Path(".arch/ground_truth.md").read_text(encoding="utf-8")
        self.assertIn("## 已被取代", md)
        with self.assertRaisesRegex(arch_cli.ArchEngineError, "不存在"):
            arch_cli.op_verify("x", "y", supersedes="GT-99")

    def test_facts_queries(self) -> None:
        arch_cli.op_collapse("entry_input", "输入", "spec", stage="S1")
        arch_cli.op_verify("全局", "约束 G")
        arch_cli.op_verify("阶段事实", "约束 S", scope="S1")
        arch_cli.op_verify("别的节点", "约束 C", scope="core_transform")
        arch_cli.op_reset_node("entry_input", "方案不行")
        by_node = arch_cli.op_facts(node_id="entry_input")
        self.assertIn("阶段事实", by_node)
        self.assertIn("方案不行", by_node)
        self.assertNotIn("别的节点", by_node)
        self.assertNotIn("约束 G", by_node)
        self.assertIn("约束 G", arch_cli.op_facts())
        everything = arch_cli.op_facts(all_scopes=True)
        for text in ("约束 G", "约束 S", "约束 C", "方案不行"):
            self.assertIn(text, everything)

    def test_global_budget_warning(self) -> None:
        self.assertIn("= 40", arch_cli.op_config(global_fact_limit=40))
        out = arch_cli.op_verify("一条很长的全局事实" * 3, "一条很长的全局约束" * 3)
        self.assertIn("GLOBAL_FACTS_OVER_BUDGET", out)
        self.assertIn("GLOBAL_FACTS_OVER_BUDGET", arch_cli.op_focus())
        self.assertIn("GLOBAL_FACTS_OVER_BUDGET", arch_cli.op_status())
        arch_cli.op_rescope("GT-01", ["entry_input"])
        self.assertNotIn("GLOBAL_FACTS_OVER_BUDGET", arch_cli.op_focus())
        self.assertEqual(arch_cli._global_fact_budget(_records())[1], 40)

    def test_rescope_moves_a_fact_out_of_every_focus(self) -> None:
        arch_cli.op_verify("只和输入有关", "约束 X")
        self.assertIn("约束 X", arch_cli.op_focus("final_output"))
        arch_cli.op_rescope("GT-01", "entry_input")
        self.assertNotIn("约束 X", arch_cli.op_focus("final_output"))
        self.assertIn("约束 X", arch_cli.op_focus("entry_input"))
        with self.assertRaisesRegex(arch_cli.ArchEngineError, "只适用于"):
            arch_cli.op_rescope("GY-01", "entry_input")

    def test_hand_edited_md_is_never_overwritten(self) -> None:
        arch_cli.op_verify("a", "b")
        gt = Path(".arch/ground_truth.md")
        gt.write_text(gt.read_text(encoding="utf-8") + "- [GT-02] 手写事实 -> 架构约束：手写约束\n", encoding="utf-8")
        self.assertIn("RECORDS_MD_EDITED", arch_cli.op_focus())
        with self.assertRaisesRegex(arch_cli.ArchEngineError, "migrate-records"):
            arch_cli.op_verify("c", "d")
        self.assertIn("手写约束", gt.read_text(encoding="utf-8"))
        arch_cli.op_migrate_records()
        self.assertEqual([f["constraint"] for f in _records()["facts"]], ["b"])
        self.assertEqual([lg["text"] for lg in _records()["legacy"]],
                         ["- [GT-02] 手写事实 -> 架构约束：手写约束"])  # kept verbatim for the agent
        arch_cli.op_verify("c", "d")  # unblocked
        self.assertIn("记录已是最新", arch_cli.op_migrate_records())


class FocusSliceTest(EngineTestCase):
    def setUp(self) -> None:
        super().setUp()
        for nid in ("entry_input", "core_transform", "final_output"):
            self.collapse(nid)
        arch_cli.op_add_node("decoder", "解码", "spec", status="solid",
                             after_node="entry_input", before_node="core_transform")
        # entry_input -> decoder -> core_transform -> final_output
        arch_cli.op_verify("全局事实", "全局约束")
        arch_cli.op_verify("输入事实", "输入约束", scope="entry_input")
        arch_cli.op_verify("解码事实", "解码约束", scope="decoder")
        arch_cli.op_verify("输出事实", "输出约束", scope="final_output")
        arch_cli.op_open_add("全局卡点", "decision", "user", blocking=True)
        arch_cli.op_open_add("全局闲事", "research", "agent")
        arch_cli.op_open_add("输出问题", "research", "agent", scope="final_output")

    def test_focus_carries_only_related_records(self) -> None:
        out = arch_cli.op_focus("entry_input")
        for text in ("全局约束", "输入约束", "解码约束", "全局卡点"):  # self, neighbour, blocking global
            self.assertIn(text, out)
        for text in ("输出约束", "输出问题", "全局闲事"):
            self.assertNotIn(text, out)
        self.assertIn("另有 1 条节点/阶段事实、2 条待办项 未展开", out)

    def test_graveyard_follows_its_node(self) -> None:
        arch_cli.op_reset_node("final_output", "播放延迟太高")
        self.assertIn("播放延迟太高", arch_cli.op_focus("final_output"))
        self.assertNotIn("播放延迟太高", arch_cli.op_focus("entry_input"))
        self.assertIn("1 条墓志", arch_cli.op_focus("entry_input"))

    def test_focus_on_a_module_includes_its_children(self) -> None:
        arch_cli.op_fold(["decoder", "core_transform"], "proc", "处理", "spec")
        out = arch_cli.op_focus("proc")
        self.assertIn("解码约束", out)        # scoped to a folded child
        self.assertIn("输入约束", out)        # neighbour
        self.assertIn("输出约束", out)        # neighbour

    def test_export_task_carries_only_the_target(self) -> None:
        task = json.loads(arch_cli.op_export_task("entry_input"))
        self.assertEqual(task["global_facts"], ["- [GT-01] 全局事实 -> 架构约束：全局约束"])
        self.assertEqual(len(task["node_facts"]), 1)
        self.assertIn("输入约束", task["node_facts"][0])
        self.assertEqual(len(task["open_items"]), 1)
        self.assertIn("全局卡点", task["open_items"][0])
        self.assertNotIn("global_ground_truth", task)
        self.assertEqual(task["target_node"]["impl"], "planned")


PROTOCOL_MD = """# 协议

## 5 概述
概述正文

## 6. 帧格式
帧头 4 字节

```
## 不是标题
```

### 6.1 帧头
magic 0xA5

## 60 别的章节
不该出现

## 7 Error Codes
E1
"""


class RefSectionTest(EngineTestCase):
    def setUp(self) -> None:
        super().setUp()
        Path("docs").mkdir()
        Path("docs/protocol.md").write_text(PROTOCOL_MD, encoding="utf-8")
        Path("docs/small.md").write_text("# 小文件\n全文\n", encoding="utf-8")
        Path("analysis").mkdir()
        Path("src").mkdir()
        Path("src/main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
        Path("analysis/case.json").write_text(json.dumps({"mtu": {"android": 185}}), encoding="utf-8")
        self.collapse("entry_input")

    def test_section_matching(self) -> None:
        section = arch_cli._extract_md_section(PROTOCOL_MD, "6")
        self.assertTrue(section.startswith("## 6. 帧格式"))
        self.assertIn("magic 0xA5", section)       # subsections belong to the section
        self.assertIn("## 不是标题", section)       # code fences are not headings
        self.assertNotIn("不该出现", section)        # 60 is not 6
        self.assertEqual(arch_cli._extract_md_section(PROTOCOL_MD, "6.1"), "### 6.1 帧头\nmagic 0xA5")
        self.assertTrue(arch_cli._extract_md_section(PROTOCOL_MD, "§7").startswith("## 7 Error Codes"))
        self.assertTrue(arch_cli._extract_md_section(PROTOCOL_MD, "7-error-codes").startswith("## 7"))
        self.assertTrue(arch_cli._extract_md_section(PROTOCOL_MD, "帧头").startswith("### 6.1"))
        self.assertIsNone(arch_cli._extract_md_section(PROTOCOL_MD, "9"))

    def test_export_task_inlines_referenced_sections(self) -> None:
        arch_cli.op_set_node_state("entry_input", refs=[
            "docs/protocol.md#6", "docs/small.md", "analysis/case.json#mtu.android",
            "docs/protocol.md#99", "docs/missing.md#1", "src/main.c",
        ])
        refs = {r["ref"]: r for r in json.loads(arch_cli.op_export_task("entry_input"))["ref_sections"]}
        self.assertIn("帧头 4 字节", refs["docs/protocol.md#6"]["content"])
        self.assertNotIn("概述正文", refs["docs/protocol.md#6"]["content"])
        self.assertIn("全文", refs["docs/small.md"]["content"])
        self.assertEqual(refs["analysis/case.json#mtu.android"]["content"], "185")
        self.assertIn("未找到章节", refs["docs/protocol.md#99"]["error"])
        self.assertIn("不存在", refs["docs/missing.md#1"]["error"])
        self.assertIn("note", refs["src/main.c"])

    def test_large_file_without_anchor_is_not_inlined(self) -> None:
        Path("docs/big.md").write_text("# 大\n" + "x" * (arch_cli.REF_WHOLE_FILE_LIMIT + 10), encoding="utf-8")
        arch_cli.op_set_node_state("entry_input", refs=["docs/big.md"])
        ref = json.loads(arch_cli.op_export_task("entry_input"))["ref_sections"][0]
        self.assertNotIn("content", ref)
        self.assertIn("#章节", ref["note"])


# Shaped like a real project's records before migration: GT entries with and
# without "-> 架构约束", a status table, nested headings, narrative with a code
# fence that looks like list items and headings, and an empty heading.
OLD_GROUND_TRUTH = """# 已验证现实事实与硬约束 (Ground Truth)

- [GT-01] (已验证) 板子只有 2 路 UART -> 架构约束：日志走 RTT
- [GT-02] BLE MTU 185 -> 架构约束：分片 ≤ 180
  补充：Android 12 以下默认 23
- [GT-03] 用户授权按已确认架构开发，不再逐项确认。
- [GT-05] 原生“小雷小雷”连续交互复用本机Agent对话。

## 实现状态

| 模块 | 状态 |
| --- | --- |
| ble | 已实现，未实测 |

## 调查记录
### 2026-10 延迟
先试了 A，指标 X=3.2，

又试了 B，指标 X=2.9。
```text
- 不是条目
# 也不是标题
```
1. 结论：选 B
## 空标题
"""

OLD_GRAVEYARD = """# 踩坑记录

- ❌ [节点 core_transform / 本地 Tesseract / OCR] 废弃原因：识别率不足 70%
- ❌ [节点 entry_input / 经典蓝牙] 废弃原因：iOS 不支持
随手记的一句话
"""

EXPECTED_CHUNKS = [
    ("ground_truth.md", "", "- [GT-01] (已验证) 板子只有 2 路 UART -> 架构约束：日志走 RTT"),
    ("ground_truth.md", "", "- [GT-02] BLE MTU 185 -> 架构约束：分片 ≤ 180\n  补充：Android 12 以下默认 23"),
    ("ground_truth.md", "", "- [GT-03] 用户授权按已确认架构开发，不再逐项确认。"),
    ("ground_truth.md", "", "- [GT-05] 原生“小雷小雷”连续交互复用本机Agent对话。"),
    ("ground_truth.md", "## 实现状态", "| 模块 | 状态 |\n| --- | --- |\n| ble | 已实现，未实测 |"),
    ("ground_truth.md", "## 调查记录 / ### 2026-10 延迟",
     "先试了 A，指标 X=3.2，\n\n又试了 B，指标 X=2.9。\n```text\n- 不是条目\n# 也不是标题\n```"),
    ("ground_truth.md", "## 调查记录 / ### 2026-10 延迟", "1. 结论：选 B"),
    ("ground_truth.md", "", "## 空标题"),
    ("graveyard.md", "", "- ❌ [节点 core_transform / 本地 Tesseract / OCR] 废弃原因：识别率不足 70%"),
    ("graveyard.md", "", "- ❌ [节点 entry_input / 经典蓝牙] 废弃原因：iOS 不支持"),
    ("graveyard.md", "", "随手记的一句话"),
]


class MigrationTest(EngineTestCase):
    def setUp(self) -> None:
        super().setUp()
        # Turn the fresh project into one written by the previous version.
        Path(".arch/records.json").unlink()
        Path(".arch/open_items.md").unlink()
        Path(".arch/ground_truth.md").write_text(OLD_GROUND_TRUTH, encoding="utf-8")
        Path(".arch/graveyard.md").write_text(OLD_GRAVEYARD, encoding="utf-8")

    def legacy(self) -> list:
        return [(lg["source"], lg["section"], lg["text"]) for lg in _records()["legacy"]]

    def test_unmigrated_project_keeps_working_the_old_way(self) -> None:
        out = arch_cli.op_focus()
        self.assertIn("又试了 B", out)
        self.assertIn("随手记的一句话", out)
        self.assertIn("migrate-records", out)
        self.collapse("entry_input")
        task = json.loads(arch_cli.op_export_task("entry_input"))
        self.assertIn("又试了 B", task["global_ground_truth"])
        self.assertIn("migrate-records", task["records_hint"])
        arch_cli.op_verify("新事实", "新约束")
        self.assertIn("新约束", Path(".arch/ground_truth.md").read_text(encoding="utf-8"))
        self.assertFalse(Path(".arch/records.json").exists())
        for call in (lambda: arch_cli.op_open_add("q", "research", "user"),
                     lambda: arch_cli.op_facts(),
                     lambda: arch_cli.op_graveyard_add("entry_input", "s", "r"),
                     lambda: arch_cli.op_verify("x", "y", scope="entry_input")):
            with self.assertRaisesRegex(arch_cli.ArchEngineError, "migrate-records"):
                call()
        self.assertIn("RECORDS_NOT_MIGRATED", arch_cli.op_status())

    def test_everything_becomes_small_legacy_chunks(self) -> None:
        out = arch_cli.op_migrate_records()
        self.assertIn("原文切为 11 条待处理记录", out)
        self.assertIn("legacy resolve --id LG-xx", out)       # the guide comes with the result
        self.assertIn("--supersedes", out)
        rec = _records()
        self.assertEqual((rec["facts"], rec["graveyard"]), ([], []))  # nothing is interpreted
        self.assertEqual(self.legacy(), EXPECTED_CHUNKS)
        self.assertTrue(all(lg["status"] == "open" for lg in rec["legacy"]))
        self.assertTrue(Path(".arch/ground_truth.md").read_text(encoding="utf-8").startswith(arch_cli.GENERATED_MARKER))

    def test_migration_is_lossless(self) -> None:
        arch_cli.op_migrate_records()
        chunks = _records()["legacy"]
        chunk_lines = Counter(ln for lg in chunks for ln in lg["text"].splitlines() if ln.strip())
        headings = {h for lg in chunks for h in lg["section"].split(" / ") if h}
        original = Counter()
        for text in (OLD_GROUND_TRUTH, OLD_GRAVEYARD):
            in_fence = False
            for line in text.splitlines():
                if line.startswith("```"):
                    in_fence = not in_fence
                if not line.strip() or line.strip() in arch_cli.DEFAULT_TITLES:
                    continue
                if not in_fence and arch_cli.MD_HEADING.match(line) and line in headings:
                    continue  # kept as the section of the chunks below it
                original[line] += 1
        self.assertEqual(chunk_lines, original)

    def test_migration_is_idempotent(self) -> None:
        arch_cli.op_migrate_records()
        before = Path(".arch/records.json").read_text(encoding="utf-8")
        md_before = Path(".arch/ground_truth.md").read_text(encoding="utf-8")
        self.assertIn("记录已是最新", arch_cli.op_migrate_records())
        self.assertEqual(Path(".arch/records.json").read_text(encoding="utf-8"), before)
        self.assertEqual(Path(".arch/ground_truth.md").read_text(encoding="utf-8"), md_before)
        # An old md restored over the generated one imports nothing twice,
        # including chunks that were already handled.
        arch_cli.op_legacy("resolve", "LG-01", "拆为 GT-01")
        Path(".arch/ground_truth.md").write_text(OLD_GROUND_TRUTH, encoding="utf-8")
        out = arch_cli.op_migrate_records()
        self.assertIn("跳过重复 8 条", out)
        self.assertEqual(len(_records()["legacy"]), 11)
        # A line added to the generated md by hand becomes one more chunk.
        gt = Path(".arch/ground_truth.md")
        gt.write_text(gt.read_text(encoding="utf-8") + "- 手写的新条目\n", encoding="utf-8")
        self.assertIn("原文切为 1 条", arch_cli.op_migrate_records())
        self.assertEqual(self.legacy()[-1], ("ground_truth.md", "", "- 手写的新条目"))

    def test_agent_works_through_the_chunks(self) -> None:
        arch_cli.op_migrate_records()
        self.assertIn("[LEGACY_RECORDS] 还有 11 条", arch_cli.op_status())
        self.assertIn("[LEGACY_RECORDS] 还有 11 条", arch_cli.op_focus())
        shown = arch_cli.op_legacy("show")                      # next open chunk
        self.assertIn("[LG-01] 来自 ground_truth.md", shown)
        self.assertIn("日志走 RTT", shown)
        self.assertIn("legacy resolve --id LG-01", shown)
        self.assertIn("graveyard add", arch_cli.op_legacy("list"))

        arch_cli.op_verify("板子只有 2 路 UART", "日志走 RTT", scope="entry_input", source="legacy LG-01")
        self.assertIn("剩余待处理 10 条", arch_cli.op_legacy("resolve", "LG-01", "拆为 GT-01"))
        self.assertIn("[LG-02]", arch_cli.op_legacy("show"))
        arch_cli.op_graveyard_add("core_transform", "本地 Tesseract / OCR", "识别率不足 70%")
        arch_cli.op_legacy("resolve", "LG-09", "拆为 GY-01")

        gt = Path(".arch/ground_truth.md").read_text(encoding="utf-8")
        self.assertIn("又试了 B", gt)                            # pending text stays visible
        self.assertNotIn("- [GT-01] (已验证)", gt)                # handled chunk leaves the md
        self.assertIn("- [GT-01] 板子只有 2 路 UART -> 架构约束：日志走 RTT", gt)
        self.assertIn("(已验证) 板子只有 2 路 UART", Path(".arch/records.json").read_text(encoding="utf-8"))
        self.assertIn("识别率不足", arch_cli.op_focus("core_transform"))

        for lg in _records()["legacy"]:
            if lg["status"] == "open":
                arch_cli.op_legacy("resolve", lg["id"], "测试：无用")
        self.assertIn("没有待处理", arch_cli.op_legacy("list"))
        self.assertIn("迁移已完成", arch_cli.op_legacy("show"))
        self.assertNotIn("LEGACY_RECORDS", arch_cli.op_status())
        self.assertIn("LG-01", arch_cli.op_legacy("list", include_resolved=True))

    def test_after_migration_focus_slices(self) -> None:
        arch_cli.op_migrate_records()
        out = arch_cli.op_focus("final_output")
        self.assertNotIn("又试了 B", out)                # migrated text is no longer concatenated
        self.assertNotIn("日志走 RTT", out)
        self.assertNotIn("RECORDS_NOT_MIGRATED", out)
        self.assertIn("[LEGACY_RECORDS]", out)

    def test_reinit_over_old_md_does_not_migrate_silently(self) -> None:
        arch_cli.op_init("again")
        self.assertFalse(Path(".arch/records.json").exists())
        self.assertIn("又试了 B", Path(".arch/ground_truth.md").read_text(encoding="utf-8"))


class GraveyardAddTest(EngineTestCase):
    def test_records_without_resetting_the_node(self) -> None:
        self.collapse("entry_input")
        out = arch_cli.op_graveyard_add("entry_input", "经典蓝牙", "iOS 不支持")
        self.assertIn("- ❌ [节点 entry_input / 经典蓝牙] 废弃原因：iOS 不支持", out)
        self.assertEqual(_node("entry_input")["status"], "solid")
        self.assertEqual(_records()["graveyard"][0]["id"], "GY-01")
        self.assertIn("iOS 不支持", Path(".arch/graveyard.md").read_text(encoding="utf-8"))
        with self.assertRaisesRegex(arch_cli.ArchEngineError, "不存在"):
            arch_cli.op_graveyard_add("nope", "s", "r")


class ForkKeepsRecordsTest(EngineTestCase):
    def test_records_survive_fork(self) -> None:
        self.collapse("entry_input")
        arch_cli.op_verify("事实", "约束", scope="entry_input")
        arch_cli.op_open_add("问题", "research", "agent")
        arch_cli.op_fork("arch-step-01")
        rec = _records()
        self.assertEqual([f["id"] for f in rec["facts"]], ["GT-01"])
        self.assertEqual([o["id"] for o in rec["open_items"]], ["OI-01"])
        self.assertEqual(arch_cli._load_graph()["step"], 1)
        self.assertIn("约束", Path(".arch/ground_truth.md").read_text(encoding="utf-8"))


class McpToolsTest(EngineTestCase):
    def test_new_tools_are_listed_and_dispatch(self) -> None:
        names = {t["name"] for t in arch_mcp.TOOLS}
        for name in ("arch_set_node_state", "arch_open", "arch_facts", "arch_status", "arch_graveyard_add",
                     "arch_migrate_records", "arch_rescope", "arch_legacy", "arch_config"):
            self.assertIn(name, names)
        arch_mcp._dispatch_tool("arch_set_node_state", {"node_id": "entry_input", "impl": "building"})
        self.assertEqual(_node("entry_input")["impl"], "building")
        arch_mcp._dispatch_tool("arch_open", {"action": "add", "question": "q", "kind": "research",
                                             "owner": "agent", "scope": ["entry_input"]})
        self.assertIn("OI-01", arch_mcp._dispatch_tool("arch_open", {"action": "list", "node_id": "entry_input"}))
        arch_mcp._dispatch_tool("arch_verify", {"fact": "f", "constraint": "c", "scope": ["entry_input"]})
        self.assertIn("f -> 架构约束：c", arch_mcp._dispatch_tool("arch_facts", {"node_id": "entry_input"}))
        self.assertIn("项目状态", arch_mcp._dispatch_tool("arch_status", {}))
        with self.assertRaisesRegex(arch_cli.ArchEngineError, "file-path"):
            arch_mcp._dispatch_tool("arch_commit_node", {"node_id": "entry_input"})


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
            # Review finding: exported bindings are public API as well.
            "+export const encode = (value: number) => value",
            "-export const encode = (value: string) => value",
            "+export let handler = async (req) => {",
            "+export default (frame) => frame",
            "+module.exports = { encode }",
            "-export { encode }",
            "+export { decode as encode } from './codec'",
            "+export * from './frames'",
            "+export type { Frame } from './types'",
            "+export = Codec;",
            "+exports.encode = function (v) {",
        ):
            self.assertTrue(self.check(line), line)

    def test_ignores_keywords_inside_ordinary_lines(self) -> None:
        for line in (
            '+    print("def is a keyword")',
            "+    node_type = 'port'",
            "+    # refactor: function signature unchanged",
            "+    return classify(x)",
            "+    const local = (x) => x * 2",
            "+    exportedCount = 3",
        ):
            self.assertFalse(self.check(line), line)


class MultiTopologyTest(EngineTestCase):
    def test_feedback_edge_in_pipeline_preserves_walking_skeleton(self) -> None:
        for nid in ("entry_input", "core_transform", "final_output"):
            self.collapse(nid)
        out = arch_cli.op_connect("final_output", "entry_input", "审批回流", kind="feedback")
        self.assertIn("[WALKING_SKELETON_READY] 检测到无空洞可运行主干: entry_input -> core_transform -> final_output", out)
        arch_cli.op_disconnect("final_output", "entry_input")
        edges = {(e["from"], e["to"]) for e in arch_cli._load_graph()["edges"]}
        self.assertNotIn(("final_output", "entry_input"), edges)

    def test_ring_topology_focus_order_and_skeleton(self) -> None:
        out = arch_cli.op_init("Agent 闭环", topology="ring", preset="agent")
        self.assertIn("topology: ring", out)
        # First focus should be the entry anchor `perceive`
        self.assertIn('"id": "perceive"', out.split('"focus_node"')[1].split("}")[0])
        self.collapse("perceive")
        out2 = self.collapse("decide")
        self.assertNotIn("WALKING_SKELETON_READY", out2)
        out3 = self.collapse("act")
        self.assertIn("[WALKING_SKELETON_READY] 检测到无空洞可运行闭环: perceive -> decide -> act -> perceive", out3)

    def test_ring_allows_folding_contiguous_arc_and_unfolds_cleanly(self) -> None:
        arch_cli.op_init("Ring fold", topology="ring")
        for nid in ("perceive", "decide", "act"):
            self.collapse(nid)
        arch_cli.op_add_node("reflect", "反思", "spec", status="solid", after_node="act", before_node="perceive")
        arch_cli.op_set_edge_contract("reflect", "perceive", "记忆回写", kind="feedback")
        # Folding an open arc without explicit --topology must infer `pipeline`,
        # not inherit `ring`, so the parent ring stays WALKING_SKELETON_READY.
        out = arch_cli.op_fold(["perceive", "decide"], "front_loop", "感知决策", "spec")
        self.assertIn("已将 2 个节点折叠为超级节点 'front_loop'", out)
        self.assertIn("topology=pipeline", out)
        self.assertIn("WALKING_SKELETON_READY", out)
        arch_cli.op_unfold("front_loop")
        ids = {n["id"] for n in arch_cli._load_graph()["nodes"]}
        self.assertTrue({"perceive", "decide", "act", "reflect"} <= ids)

    def test_deactivate_preserves_feedback_kind_on_bypass_edge(self) -> None:
        for nid in ("entry_input", "core_transform", "final_output"):
            self.collapse(nid)
        arch_cli.op_connect("final_output", "entry_input", "审批回流", kind="feedback")
        out = arch_cli.op_deactivate_node("final_output", "本轮先不做最终输出")
        bypass = next(
            e for e in arch_cli._load_graph()["edges"]
            if e["from"] == "core_transform" and e["to"] == "entry_input"
        )
        self.assertEqual(bypass.get("kind"), "feedback")
        self.assertIn(
            "[WALKING_SKELETON_READY] 检测到无空洞可运行主干: entry_input -> core_transform",
            out,
        )

    def test_star_topology_hub_first_focus_and_skeleton(self) -> None:
        out = arch_cli.op_init("事件总线", topology="star", preset="platform")
        self.assertIn("topology: star", out)
        # Focus prioritizes hub first
        self.assertIn('"id": "core_hub"', out.split('"focus_node"')[1].split("}")[0])
        out1 = self.collapse("core_hub")
        self.assertNotIn("WALKING_SKELETON_READY", out1)
        out2 = self.collapse("spoke_a")
        self.assertNotIn("WALKING_SKELETON_READY", out2)
        out3 = self.collapse("spoke_b")
        self.assertIn("[WALKING_SKELETON_READY] 检测到无空洞可运行星形中枢: core_hub <-> [spoke_a, spoke_b]", out3)

    def test_star_hub_deactivate_guard_and_no_bridge(self) -> None:
        arch_cli.op_init("事件总线", topology="star")
        with self.assertRaisesRegex(arch_cli.ArchEngineError, "AutoBridgeLimitError"):
            arch_cli.op_deactivate_node("core_hub", "先下线")
        out = arch_cli.op_deactivate_node("core_hub", "拆除", no_bridge=True)
        self.assertIn("未自动缝合旁路", out)
        self.assertEqual(arch_cli._load_graph()["edges"], [])

    def test_folding_feedback_arc_and_whole_ring_preserves_readiness(self) -> None:
        # Codex round-3 regression: folding `act,perceive` (feedback-only arc) or
        # folding the entire ring must preserve parent readiness.
        arch_cli.op_init("Ring whole fold", topology="ring")
        for nid in ("perceive", "decide", "act"):
            self.collapse(nid)
        out = arch_cli.op_fold(["act", "perceive"], "wrap", "回流感知", "spec")
        self.assertIn("WALKING_SKELETON_READY", out)
        self.assertEqual(
            arch_cli._find_walking_skeleton(arch_cli._load_graph()),
            ["wrap", "decide", "wrap"],
        )
        # Folding the remaining node together collapses the entire ring into one ready module.
        out2 = arch_cli.op_fold(["wrap", "decide"], "loop_all", "完整闭环", "spec")
        self.assertIn("[WALKING_SKELETON_READY] 检测到无空洞可运行闭环: loop_all", out2)
        self.assertEqual(arch_cli._find_walking_skeleton(arch_cli._load_graph()), ["loop_all"])

    def test_folding_star_pair_and_whole_star_preserves_readiness(self) -> None:
        # Codex round-3 regression: folding `core_hub,spoke_a` (bidirectional pair) or
        # folding the entire star must preserve parent readiness, and resetting hub clears it.
        arch_cli.op_init("Star whole fold", topology="star")
        for nid in ("core_hub", "spoke_a", "spoke_b"):
            self.collapse(nid)
        out = arch_cli.op_fold(["core_hub", "spoke_a"], "core_a", "中枢+A", "spec")
        self.assertIn("WALKING_SKELETON_READY", out)
        self.assertEqual(arch_cli._find_walking_skeleton(arch_cli._load_graph()), ["core_a", "spoke_b"])

        out2 = arch_cli.op_fold(["core_a", "spoke_b"], "bus_all", "完整总线", "spec")
        self.assertIn("[WALKING_SKELETON_READY] 检测到无空洞可运行星形中枢: bus_all", out2)
        self.assertEqual(arch_cli._find_walking_skeleton(arch_cli._load_graph()), ["bus_all"])

        # Resetting hub inside nested fold must immediately clear readiness.
        arch_cli.op_reset_node("core_hub", "重选中枢", scope="core_a")
        self.assertIsNone(arch_cli._find_walking_skeleton(arch_cli._load_graph()))

    def test_promoted_star_spoke_gates_parent_star_readiness(self) -> None:
        # Codex round-4 regression: promoting a star's spoke into another star must
        # keep the spoke gated on its own subsystem readiness (collapsing only its
        # inner hub must NOT prematurely make the parent star ready).
        arch_cli.op_init("Nested star", topology="star")
        for nid in ("core_hub", "spoke_a", "spoke_b"):
            self.collapse(nid)
        arch_cli.op_promote("spoke_a", topology="star")
        arch_cli.op_collapse("spoke_a_hub", "子中枢", "spec", scope="spoke_a")
        self.assertIsNone(arch_cli._find_walking_skeleton(arch_cli._load_graph(), scope="spoke_a"))
        self.assertIsNone(arch_cli._find_walking_skeleton(arch_cli._load_graph()))

        # Once the promoted spoke's own star becomes ready, parent star becomes ready too.
        arch_cli.op_collapse("spoke_a_spoke_a", "子通道A", "spec", scope="spoke_a")
        arch_cli.op_collapse("spoke_a_spoke_b", "子通道B", "spec", scope="spoke_a")
        self.assertEqual(
            arch_cli._find_walking_skeleton(arch_cli._load_graph()),
            ["core_hub", "spoke_a", "spoke_b"],
        )


class SubsystemSuperNodeTest(EngineTestCase):
    def test_promote_creates_isolated_subsystem_with_compositional_skeleton(self) -> None:
        self.collapse("entry_input")
        self.collapse("final_output")
        out = arch_cli.op_promote("core_transform", topology="star", label="路由中枢")
        self.assertIn("升级为 star 子系统", out)
        self.assertIn('"scope": "core_transform"', out)
        self.assertIn('"boundary_in"', out)
        # Parent pipeline MUST NOT report ready while star subsystem still has holes inside
        self.assertIsNone(arch_cli._find_walking_skeleton(arch_cli._load_graph()))

        # Settle all nodes inside the star subsystem using --in scope (without unfolding!)
        arch_cli.op_collapse("core_transform_hub", "中心总线", "spec", scope="core_transform")
        arch_cli.op_collapse("core_transform_spoke_a", "通道A", "spec", scope="core_transform")
        sub_out = arch_cli.op_collapse("core_transform_spoke_b", "通道B", "spec", scope="core_transform")
        self.assertIn("检测到无空洞可运行星形中枢", sub_out)

        # Now parent pipeline skeleton becomes ready through the star subsystem!
        self.assertEqual(
            arch_cli._find_walking_skeleton(arch_cli._load_graph()),
            ["entry_input", "core_transform", "final_output"],
        )
        # Export task inside subsystem carries boundary contracts
        task = json.loads(arch_cli.op_export_task("core_transform_hub", scope="core_transform"))
        self.assertEqual(task["subsystem_scope"], "core_transform")
        self.assertEqual(task["subsystem_topology"], "star")
        self.assertTrue(task["boundary_in_contracts"])
        self.assertTrue(task["boundary_out_contracts"])

    def test_dashboard_embeds_subsystem_topology_and_drilldown_markers(self) -> None:
        self.collapse("entry_input")
        self.collapse("final_output")
        arch_cli.op_promote("core_transform", topology="ring", label="思考环")
        arch_cli.op_export_ui()
        page = Path(".arch/dashboard.html").read_text(encoding="utf-8")
        data = ExportUiTest.embedded(None, page)
        self.assertEqual(data["topology"], "pipeline")
        self.assertEqual(data["subsystems"]["core_transform"]["topology"], "ring")
        self.assertFalse(data["subsystems"]["core_transform"]["ready"])
        self.assertEqual(data["config"]["preset"], "standard")
        for snippet in ("layoutSelect", "进入子系统", "buildStarLayout", "buildRingLayout"):
            self.assertIn(snippet, page)

    def test_folded_star_subsystem_resolves_provenance_and_blocks_premature_skeleton(self) -> None:
        # Codex P1 regression: folding a star inside a pipeline with a 2-node solid prefix
        # must keep parent boundary edges resolved to the atomic subsystem node.
        for nid in ("entry_input", "core_transform", "final_output"):
            self.collapse(nid)
        arch_cli.op_add_node(
            "pre", "前置校验", "spec", status="solid",
            after_node="entry_input", before_node="core_transform",
        )
        arch_cli.op_add_node("sp1", "通道1", "spec", status="solid", after_node="core_transform", edge_kind="bidirectional")
        arch_cli.op_add_node("sp2", "通道2", "spec", status="solid", after_node="core_transform", edge_kind="bidirectional")
        arch_cli.op_collapse("core_transform", "中枢", "spec", role="hub")

        arch_cli.op_fold(
            ["core_transform", "sp1", "sp2"], "router", "星形路由", "spec", topology="star",
        )
        # Complete pipeline through folded star subsystem stays ready.
        self.assertEqual(
            arch_cli._find_walking_skeleton(arch_cli._load_graph()),
            ["entry_input", "pre", "router", "final_output"],
        )
        # Resetting hub inside `router` makes subsystem unready; parent MUST NOT
        # drop `pre -> router` and falsely report `entry_input -> pre` as ready!
        arch_cli.op_reset_node("core_transform", "重选协议", scope="router")
        self.assertIsNone(arch_cli._find_walking_skeleton(arch_cli._load_graph()))

    def test_edge_kind_edit_on_folded_module_survives_unfold_and_affects_skeleton(self) -> None:
        # Codex P2 regression: editing `kind` on an edge touching a folded module
        # must propagate into `members` provenance so both flattening and unfold keep it.
        for nid in ("entry_input", "core_transform", "final_output"):
            self.collapse(nid)
        arch_cli.op_add_node(
            "decoder", "解码", "spec", status="solid",
            after_node="entry_input", before_node="core_transform",
        )
        arch_cli.op_fold(["entry_input", "decoder"], "front", "前端", "spec")
        self.assertEqual(
            arch_cli._find_walking_skeleton(arch_cli._load_graph()),
            ["front", "core_transform", "final_output"],
        )
        arch_cli.op_set_edge_contract("front", "core_transform", "反馈流", kind="feedback")
        # Feedback edge is ignored by pipeline skeleton, so the path no longer crosses into core_transform.
        self.assertEqual(arch_cli._find_walking_skeleton(arch_cli._load_graph()), ["front"])
        arch_cli.op_unfold("front")
        restored = next(
            e for e in arch_cli._load_graph()["edges"]
            if e["from"] == "decoder" and e["to"] == "core_transform"
        )
        self.assertEqual(restored.get("kind"), "feedback")
        self.assertEqual(restored.get("contract"), "反馈流")

        # Also verify feedback -> primary override on a folded edge survives unfold.
        arch_cli.op_fold(["entry_input", "decoder"], "front2", "前端2", "spec")
        arch_cli.op_set_edge_contract("front2", "core_transform", "主流", kind="primary")
        self.assertEqual(
            arch_cli._find_walking_skeleton(arch_cli._load_graph()),
            ["front2", "core_transform", "final_output"],
        )
        arch_cli.op_unfold("front2")
        restored2 = next(
            e for e in arch_cli._load_graph()["edges"]
            if e["from"] == "decoder" and e["to"] == "core_transform"
        )
        self.assertNotIn("kind", restored2)

    def test_scoped_fold_and_unfold_remap_subsystem_boundary_ports(self) -> None:
        # Codex round-5 regression: folding/unfolding children inside a promoted
        # subsystem must remap its entry_children / exit_children so unfolding the
        # outer subsystem wires parent edges to existing direct children.
        for nid in ("entry_input", "final_output"):
            self.collapse(nid)
        arch_cli.op_promote("core_transform", topology="pipeline")
        for cid in ("core_transform_in", "core_transform_core", "core_transform_out"):
            arch_cli.op_collapse(cid, cid, "spec", scope="core_transform")

        arch_cli.op_fold(
            ["core_transform_in", "core_transform_core"], "front", "前段", "spec",
            scope="core_transform",
        )
        sub = arch_cli._find_any_node(arch_cli._load_graph(), "core_transform")
        self.assertEqual(sub["entry_children"], ["front"])
        self.assertEqual(sub["exit_children"], ["core_transform_out"])

        # Scoped unfold restores original boundary port IDs.
        arch_cli.op_unfold("front", scope="core_transform")
        sub = arch_cli._find_any_node(arch_cli._load_graph(), "core_transform")
        self.assertEqual(sub["entry_children"], ["core_transform_in"])

        # Re-fold inside subsystem, then unfold outer subsystem: parent edge must target `front`.
        arch_cli.op_fold(
            ["core_transform_in", "core_transform_core"], "front", "前段", "spec",
            scope="core_transform",
        )
        arch_cli.op_unfold("core_transform")
        g = arch_cli._load_graph()
        top_ids = {n["id"] for n in g["nodes"]}
        edges = {(e["from"], e["to"]) for e in g["edges"]}
        self.assertIn("front", top_ids)
        self.assertIn(("entry_input", "front"), edges)
        self.assertIn(("front", "core_transform_out"), edges)
        self.assertEqual(
            arch_cli._find_walking_skeleton(g),
            ["entry_input", "front", "core_transform_out", "final_output"],
        )


class PresetAndBootstrapTest(EngineTestCase):
    def test_preset_enforces_max_depth_and_feedback_and_survives_fork(self) -> None:
        arch_cli.op_init("lean tool", preset="lean")
        cfg = arch_cli._load_config()
        self.assertEqual((cfg["preset"], cfg["max_depth"], cfg["allow_feedback"]), ("lean", 1, False))
        with self.assertRaisesRegex(arch_cli.ArchEngineError, "allow_feedback=false"):
            arch_cli.op_connect("final_output", "entry_input", "loop", kind="feedback")

        # Promote to depth 1 succeeds; promoting a child to depth 2 hits max_depth=1
        arch_cli.op_promote("core_transform", topology="pipeline")
        with self.assertRaisesRegex(arch_cli.ArchEngineError, "MaxDepthError"):
            arch_cli.op_promote("core_transform_core", topology="ring", scope="core_transform")

        # Raising max_depth via config unblocks depth 2 and survives fork
        arch_cli.op_config(max_depth=2, allow_feedback=True)
        arch_cli.op_promote("core_transform_core", topology="ring", scope="core_transform")
        arch_cli.op_fork("arch-step-01")
        self.assertEqual(arch_cli._load_config()["max_depth"], 2)
        self.assertTrue(arch_cli._load_config()["allow_feedback"])

    def test_chain_length_soft_warning(self) -> None:
        arch_cli.op_config(max_chain_len=3)
        for nid in ("entry_input", "core_transform", "final_output"):
            self.collapse(nid)
        out = arch_cli.op_add_node("extra", "第4节", "spec", status="solid", after_node="final_output")
        self.assertIn("[CHAIN_TOO_LONG]", out)

    def test_bootstrap_embeds_cli_and_shadow_git_excludes_scripts(self) -> None:
        out = arch_cli.op_bootstrap()
        self.assertIn(".arch/scripts", out)
        embedded_cli = Path(".arch/scripts/arch_cli.py")
        self.assertTrue(embedded_cli.exists())
        self.assertTrue(Path(".arch/scripts/arch_engine/graph.py").exists())
        self.assertTrue(Path(".arch/scripts/VERSION").exists())

        # Make a state commit and fork: Shadow Git must NOT track or roll back .arch/scripts/
        self.collapse("entry_input")
        marker = Path(".arch/scripts/CUSTOM_LOCAL_MARKER")
        marker.write_text("keep-me\n", encoding="utf-8")
        arch_cli.op_fork("arch-step-01")
        self.assertTrue(embedded_cli.exists())
        self.assertTrue(marker.exists())

    def test_shadow_git_uses_non_dot_git_dir_and_migrates_legacy(self) -> None:
        # Issue #4 regression: `.arch/shadow.git` avoids Git 2.34 nested-repo `.arch/.git` bug.
        self.assertTrue(Path(".arch/shadow.git").exists())
        self.assertFalse(Path(".arch/.git").exists())
        self.assertIn("shadow.git/", Path(".arch/.gitignore").read_text(encoding="utf-8"))

        from arch_engine.common import _run_shadow_git
        tracked = set(_run_shadow_git(["ls-files"]).stdout.splitlines())
        self.assertIn(".arch/graph.json", tracked)
        self.assertIn(".arch/config.json", tracked)
        self.assertFalse(any(p.startswith(".arch/shadow.git") for p in tracked))

        # Simulate a legacy project whose shadow repo still lives at `.arch/.git`
        self.collapse("entry_input")
        Path(".arch/shadow.git").rename(Path(".arch/.git"))
        self.assertTrue(Path(".arch/.git").exists())
        arch_cli.op_fork("arch-step-01")
        self.assertTrue(Path(".arch/shadow.git").exists())
        self.assertFalse(Path(".arch/.git").exists())
        self.assertEqual(arch_cli._load_graph()["step"], 1)

    def test_mcp_exposes_and_dispatches_v2_tools(self) -> None:
        names = {t["name"] for t in arch_mcp.TOOLS}
        for name in ("arch_bootstrap", "arch_connect", "arch_disconnect", "arch_promote"):
            self.assertIn(name, names)
        arch_mcp._dispatch_tool("arch_init", {"seed": "star app", "topology": "star", "preset": "platform"})
        arch_mcp._dispatch_tool("arch_promote", {"node_id": "spoke_a", "topology": "ring"})
        res = arch_mcp._dispatch_tool("arch_focus", {"scope": "spoke_a"})
        self.assertIn("topology: ring", res)
        arch_mcp._dispatch_tool("arch_config", {"max_chain_len": 9})
        self.assertEqual(arch_cli._load_config()["max_chain_len"], 9)


if __name__ == "__main__":
    unittest.main()
