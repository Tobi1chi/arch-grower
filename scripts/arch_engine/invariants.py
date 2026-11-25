"""Whole-graph structural invariants, checked before graph.json is written.

Every op mutates nested containers by hand; this module is the single place
that says what a well-formed graph looks like, so a bad mutation fails before
it reaches disk instead of surfacing later as a broken fold / unfold.

Hard violations (`error`) describe a graph the engine cannot work with.
Soft ones (`warning`) are tolerated states worth surfacing (stale bookkeeping,
a preset tightened after the fact).

`ARCH_INVARIANTS` controls enforcement in `_save_graph`:
    off     skip the check
    warn    print violations to stderr, still write (default)
    strict  raise ArchEngineError on any hard violation, nothing is written

Depends only on `common` constants, so `common._save_graph` can import it.
"""

import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from .common import ArchEngineError, EDGE_KINDS, NODE_ROLES, TOPOLOGIES

NODE_STATUSES = ("hole", "solid", "fog", "super_node")
MODES = ("off", "warn", "strict")


@dataclass(frozen=True)
class Violation:
    code: str        # "I1".."I11"
    severity: str    # "error" | "warning"
    where: str       # container path, e.g. "root" or "root/sub_a"
    message: str

    def __str__(self) -> str:
        return f"[GraphInvariant:{self.code}] {self.where}: {self.message}"


def _children_key(c: Dict[str, Any]) -> str:
    return "children" if "children" in c else "nodes"


def _edges_key(c: Dict[str, Any]) -> str:
    return "inner_edges" if "inner_edges" in c else "edges"


def _iter_containers(
    graph: Dict[str, Any],
) -> Iterator[Tuple[str, Dict[str, Any], int]]:
    """Yield (path, container, depth) for root and every super node, depth-first."""
    stack: List[Tuple[str, Dict[str, Any], int]] = [("root", graph, 0)]
    while stack:
        path, c, depth = stack.pop()
        yield path, c, depth
        for n in c.get(_children_key(c)) or []:
            if isinstance(n, dict) and n.get("status") == "super_node" and isinstance(n.get("children"), list):
                stack.append((f"{path}/{n.get('id')}", n, depth + 1))


def _subtree_ids(node: Dict[str, Any]) -> Set[str]:
    out = {node.get("id")}
    for ch in node.get("children") or []:
        if isinstance(ch, dict):
            out |= _subtree_ids(ch)
    return out


def _edge_kind(e: Dict[str, Any]) -> str:
    return e.get("kind") or "primary"


def validate_graph(graph: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> List[Violation]:
    out: List[Violation] = []

    def add(code: str, where: str, msg: str, severity: str = "error") -> None:
        out.append(Violation(code, severity, where, msg))

    if not isinstance(graph.get("nodes"), list) or not isinstance(graph.get("edges"), list):
        add("I6", "root", "根图缺少 nodes / edges 列表")
        return out

    seen_ids: Dict[str, str] = {}
    max_depth_seen = 0

    for path, c, depth in _iter_containers(graph):
        max_depth_seen = max(max_depth_seen, depth)
        nodes = c.get(_children_key(c))
        edges = c.get(_edges_key(c))
        if not isinstance(nodes, list) or not isinstance(edges, list):
            add("I6", path, "容器缺少节点或连线列表")
            continue

        topo = c.get("topology")
        if topo is not None and topo not in TOPOLOGIES:
            add("I5", path, f"未知拓扑 '{topo}'")

        by_id: Dict[str, Dict[str, Any]] = {}
        for n in nodes:
            nid = n.get("id") if isinstance(n, dict) else None
            if not nid:
                add("I6", path, f"节点缺少 id：{n!r:.80}")
                continue
            # I1: ids unique across all levels.
            if nid in seen_ids:
                add("I1", path, f"节点 ID '{nid}' 重复（另一处在 {seen_ids[nid]}）")
            else:
                seen_ids[nid] = path
            by_id[nid] = n

            # I5: enums.
            status = n.get("status")
            if status not in NODE_STATUSES:
                add("I5", path, f"节点 '{nid}' 状态非法：{status!r}")
            role = n.get("role", "")
            if role not in NODE_ROLES:
                add("I5", path, f"节点 '{nid}' 角色非法：{role!r}")

            # I6: super node shape.
            has_body = "children" in n or "inner_edges" in n
            if status == "super_node":
                if not isinstance(n.get("children"), list) or not isinstance(n.get("inner_edges"), list):
                    add("I6", path, f"超级节点 '{nid}' 缺少 children / inner_edges")
                elif not n["children"]:
                    add("I6", path, f"超级节点 '{nid}' 没有子节点")
            elif has_body:
                add("I6", path, f"非超级节点 '{nid}'（{status}）带有 children / inner_edges")

        # I7: boundary ports of a super node point at its direct children.
        if c is not graph:
            for key in ("entry_children", "exit_children"):
                ports = c.get(key)
                if not isinstance(ports, list) or not ports:
                    add("I7", path, f"{key} 缺失或为空")
                    continue
                bad = [p for p in ports if p not in by_id]
                if bad:
                    add("I7", path, f"{key} 指向非直接子节点：{', '.join(map(str, bad))}")
            # I8 (soft): saved parent ports should still resolve inside the subtree.
            # Unfolding a nested super node can leave them stale; unfold copes.
            sub = _subtree_ids(c) - {c.get("id")}
            for key, ids in (c.get("parent_ports") or {}).items():
                stale = [p for p in ids or [] if p not in sub]
                if stale:
                    add("I8", path, f"parent_ports.{key} 含已失效 ID：{', '.join(map(str, stale))}", "warning")

        # Edges.
        pairs: Dict[Tuple[str, str], int] = {}
        bidir: Set[frozenset] = set()
        for i, e in enumerate(edges):
            u, v = e.get("from"), e.get("to")
            label = f"连线 #{i} '{u} -> {v}'"
            # I2: endpoints are direct children of this container.
            missing = [x for x in (u, v) if x not in by_id]
            if missing:
                add("I2", path, f"{label} 端点不在本容器内：{', '.join(map(str, missing))}")
                continue
            # I3: no self loops.
            if u == v:
                add("I3", path, f"{label} 是自环")
            kind = _edge_kind(e)
            if kind not in EDGE_KINDS:
                add("I5", path, f"{label} 类型非法：{kind!r}")
            # I4: no duplicates; a bidirectional pair also blocks its reverse.
            pairs[(u, v)] = pairs.get((u, v), 0) + 1
            if pairs[(u, v)] == 2:
                add("I4", path, f"{label} 重复")
            key = frozenset((u, v))
            if kind == "bidirectional":
                if key in bidir or (v, u) in pairs:
                    add("I4", path, f"{label} 与反向连线重复（双向边）")
                bidir.add(key)
            elif key in bidir and (v, u) in pairs:
                add("I4", path, f"{label} 与反向的双向边重复")

            # I9: provenance members resolve inside the edge's endpoints. Only a
            # bidirectional link may hold members running the other way (it
            # absorbs every edge between its pair).
            members = e.get("members")
            if "auto_contract" in e and not members:
                add("I9", path, f"{label} 有 auto_contract 但没有 members")
            if members:
                u_ids = _subtree_ids(by_id[u])
                v_ids = _subtree_ids(by_id[v])
                for m in members:
                    mf, mt = m.get("from"), m.get("to")
                    forward = mf in u_ids and mt in v_ids
                    backward = kind == "bidirectional" and mf in v_ids and mt in u_ids
                    if not (forward or backward):
                        add("I9", path, f"{label} 的溯源成员 '{mf} -> {mt}' 不在端点子树内")

    # I11 (soft): nesting depth vs preset. A preset may be tightened later.
    if cfg is not None:
        limit = int(cfg.get("max_depth", 2))
        if max_depth_seen > limit:
            add("I11", "root", f"嵌套深度 {max_depth_seen} 超过 preset 上限 {limit}", "warning")

    return out


def invariants_mode() -> str:
    mode = os.environ.get("ARCH_INVARIANTS", "warn").strip().lower()
    return mode if mode in MODES else "warn"


def enforce(
    graph: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None, mode: str = "",
) -> None:
    """Apply `mode` (default: ARCH_INVARIANTS); raise in strict mode on hard violations."""
    mode = mode if mode in MODES else invariants_mode()
    if mode == "off":
        return
    violations = validate_graph(graph, cfg)
    if not violations:
        return
    errors = [v for v in violations if v.severity == "error"]
    if mode == "strict" and errors:
        raise ArchEngineError(
            "图结构校验失败，本次修改未写入：\n  - "
            + "\n  - ".join(str(v) for v in errors)
            + "\nJIT 指令：这是引擎内部错误，不是用户输入问题。请保留以上信息并反馈；"
            "可先用 arch fork 回退到上一个快照。"
        )
    for v in violations:
        sys.stderr.write(f"{v} ({v.severity})\n")
