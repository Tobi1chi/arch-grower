"""Unit tests for arch_engine.invariants (standard library only).

Run from the repository root:
    python3 -m unittest discover -s tests
"""

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import arch_cli  # noqa: E402
from arch_engine import ArchEngineError  # noqa: E402
from arch_engine.invariants import enforce, validate_graph  # noqa: E402


def node(nid, status="solid", **kw):
    return {"id": nid, "label": nid, "status": status, "spec": "", "file_path": "", "last_tag": "", **kw}


def edge(u, v, **kw):
    return {"from": u, "to": v, "contract": "TBD", **kw}


def base_graph():
    """a -> S{x -> y} -> c, with the boundary edges carrying provenance."""
    sub = node(
        "S", "super_node", type="super_node",
        children=[node("x"), node("y")],
        inner_edges=[edge("x", "y")],
        entry_children=["x"], exit_children=["y"],
    )
    return {
        "step": 1,
        "nodes": [node("a"), sub, node("c")],
        "edges": [
            {**edge("a", "S"), "auto_contract": "TBD", "members": [edge("a", "x")]},
            edge("S", "c"),
        ],
    }


def codes(graph, cfg=None, severity="error"):
    return sorted({v.code for v in validate_graph(graph, cfg) if v.severity == severity})


class ValidGraphTest(unittest.TestCase):
    def test_base_graph_is_clean(self):
        self.assertEqual(validate_graph(base_graph(), {"max_depth": 2}), [])

    def test_fog_node_may_hang_on_an_edge(self):
        # Fog branches are reserve ideas attached by add-node --status fog.
        g = base_graph()
        g["nodes"].append(node("later", "fog"))
        g["edges"].append(edge("c", "later"))
        self.assertEqual(codes(g), [])

    def test_feedback_reverse_of_primary_is_allowed(self):
        g = base_graph()
        g["edges"].append(edge("c", "a", kind="feedback"))
        g["edges"].append(edge("a", "c"))
        self.assertEqual(codes(g), [])


class HardViolationTest(unittest.TestCase):
    def test_i1_duplicate_id_across_levels(self):
        g = base_graph()
        g["nodes"].append(node("x"))
        self.assertIn("I1", codes(g))

    def test_i2_edge_endpoint_outside_container(self):
        g = base_graph()
        g["edges"].append(edge("a", "y"))  # y lives inside S
        self.assertIn("I2", codes(g))

    def test_i2_dangling_inner_edge(self):
        g = base_graph()
        g["nodes"][1]["inner_edges"].append(edge("y", "ghost"))
        self.assertIn("I2", codes(g))

    def test_i3_self_loop(self):
        g = base_graph()
        g["edges"].append(edge("c", "c"))
        self.assertIn("I3", codes(g))

    def test_i4_duplicate_edge(self):
        g = base_graph()
        g["edges"].append(edge("S", "c", kind="feedback"))
        self.assertIn("I4", codes(g))

    def test_i4_bidirectional_and_reverse(self):
        g = base_graph()
        g["edges"].append(edge("c", "a", kind="bidirectional"))
        g["edges"].append(edge("a", "c"))
        self.assertIn("I4", codes(g))

    def test_i5_bad_enums(self):
        for mutate in (
            lambda g: g["nodes"][0].update(status="done"),
            lambda g: g["nodes"][0].update(role="boss"),
            lambda g: g["edges"][1].update(kind="weird"),
            lambda g: g.update(topology="mesh"),
        ):
            g = base_graph()
            mutate(g)
            self.assertIn("I5", codes(g))

    def test_i6_super_node_without_body(self):
        g = base_graph()
        del g["nodes"][1]["inner_edges"]
        self.assertIn("I6", codes(g))

    def test_i6_plain_node_with_body(self):
        g = base_graph()
        g["nodes"][0]["children"] = []
        self.assertIn("I6", codes(g))

    def test_i7_port_not_a_direct_child(self):
        g = base_graph()
        g["nodes"][1]["entry_children"] = ["a"]
        self.assertIn("I7", codes(g))

    def test_i7_empty_port_list(self):
        g = base_graph()
        g["nodes"][1]["exit_children"] = []
        self.assertIn("I7", codes(g))

    def test_i9_member_outside_endpoint_subtree(self):
        g = base_graph()
        g["edges"][0]["members"] = [edge("c", "x")]
        self.assertIn("I9", codes(g))

    def test_i9_auto_contract_without_members(self):
        g = base_graph()
        g["edges"][1]["auto_contract"] = "TBD"
        self.assertIn("I9", codes(g))


class SoftViolationTest(unittest.TestCase):
    def test_i8_stale_parent_ports_is_warning_only(self):
        g = base_graph()
        g["nodes"][1]["parent_ports"] = {"entry_children": ["gone"]}
        self.assertEqual(codes(g), [])
        self.assertEqual(codes(g, severity="warning"), ["I8"])

    def test_i11_depth_over_preset_is_warning_only(self):
        g = base_graph()
        self.assertEqual(codes(g, {"max_depth": 0}), [])
        self.assertEqual(codes(g, {"max_depth": 0}, severity="warning"), ["I11"])


class EnforceModeTest(unittest.TestCase):
    def bad(self):
        g = base_graph()
        g["edges"].append(edge("c", "c"))
        return g

    def test_strict_raises(self):
        with mock.patch.dict(os.environ, {"ARCH_INVARIANTS": "strict"}):
            with self.assertRaises(ArchEngineError) as ctx:
                enforce(self.bad())
        self.assertIn("[GraphInvariant:I3]", str(ctx.exception))

    def test_warn_prints_and_returns(self):
        with mock.patch.dict(os.environ, {"ARCH_INVARIANTS": "warn"}), \
                mock.patch("sys.stderr") as err:
            enforce(self.bad())
        self.assertTrue(any("I3" in str(c) for c in err.write.call_args_list))

    def test_off_skips(self):
        with mock.patch.dict(os.environ, {"ARCH_INVARIANTS": "off"}):
            enforce(self.bad())


class SaveGraphGateTest(unittest.TestCase):
    """A broken mutation in strict mode must leave graph.json untouched."""

    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        arch_cli.op_init("test seed")

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def test_strict_blocks_write(self):
        from arch_engine import common

        before = Path(".arch/graph.json").read_text(encoding="utf-8")
        g = json.loads(before)
        broken = copy.deepcopy(g)
        broken["edges"].append(edge("ghost", g["nodes"][0]["id"]))
        with mock.patch.dict(os.environ, {"ARCH_INVARIANTS": "strict"}):
            with self.assertRaises(ArchEngineError):
                common._save_graph(broken)
        self.assertEqual(Path(".arch/graph.json").read_text(encoding="utf-8"), before)

    def test_real_op_output_is_clean(self):
        arch_cli.op_collapse("entry_input", "done", "spec")
        g = json.loads(Path(".arch/graph.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_graph(g), [])


if __name__ == "__main__":
    unittest.main()
