"""Graph model: topology, folded modules, walking skeleton, edge provenance,
node state (impl / verify / refs / files) and drift detection."""

import re
from collections import deque
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .common import ArchEngineError, SHADOW_GIT_DIR, _run_shadow_git


# A changed diff line that declares a function / type, across common languages
# (Python, JS/TS, Go, Rust, C#/Java-style modifiers). Anchored at the start of the
# line so keywords inside strings or comments do not count.
SIGNATURE_LINE = re.compile(
    r"^[+-]\s*(?:"
    # Declarations: def / class / function / fn / func / struct ...
    r"(?:(?:export|default|pub(?:\([^)]*\))?|public|private|protected|internal|static|abstract|async|unsafe)\s+)*"
    r"(?:def|class|function|interface|fn|func|struct|trait|enum|type|impl)\b"
    # Any export statement is public API: bindings (`export const f = ...`),
    # `export default`, export lists (`export { a }`), re-exports
    # (`export * from "./x"`), TypeScript `export =`; and CommonJS exports.
    r"|export\b"
    r"|(?:module\.)?exports(?:\.[\w$]+)?\s*="
    r")"
)


SOLID_STATUSES = ("solid", "super_node")


def _active_topology(graph: Dict[str, Any]) -> Tuple[Dict[str, List[str]], Dict[str, int], Dict[str, int]]:
    """Adjacency and degrees of the active graph (every node that is not fog)."""
    active_ids = {n["id"] for n in graph["nodes"] if n["status"] != "fog"}
    adj: Dict[str, List[str]] = {nid: [] for nid in active_ids}
    in_deg: Dict[str, int] = {nid: 0 for nid in active_ids}
    out_deg: Dict[str, int] = {nid: 0 for nid in active_ids}
    for e in graph["edges"]:
        u, v = e["from"], e["to"]
        if u in active_ids and v in active_ids:
            adj[u].append(v)
            in_deg[v] += 1
            out_deg[u] += 1
    return adj, in_deg, out_deg


def _iter_all_nodes(nodes: List[Dict[str, Any]]):
    """Yield every node, descending into the children of folded super nodes."""
    for n in nodes:
        yield n
        yield from _iter_all_nodes(n.get("children", []))


def _find_container(graph: Dict[str, Any], node_id: str) -> Optional[Dict[str, Any]]:
    """Return the top-level super node that (transitively) holds node_id."""
    for n in graph["nodes"]:
        if n["status"] == "super_node" and any(
            c["id"] == node_id for c in _iter_all_nodes(n.get("children", []))
        ):
            return n
    return None


def _get_node(graph: Dict[str, Any], node_id: str, allow_super: bool = True) -> Dict[str, Any]:
    """Look up a top-level node, with JIT guidance for folded or super nodes."""
    for n in graph["nodes"]:
        if n["id"] == node_id:
            if not allow_super and n["status"] == "super_node":
                raise ArchEngineError(
                    f"节点 '{node_id}' 是折叠后的超级节点，不能直接执行该操作。\n"
                    f"JIT 指令：先调用 arch_unfold(node_id='{node_id}') 展开，再对其中的子节点操作。"
                )
            return n
    container = _find_container(graph, node_id)
    if container:
        raise ArchEngineError(
            f"节点 '{node_id}' 已折叠在超级节点 '{container['id']}' 内。\n"
            f"JIT 指令：先调用 arch_unfold(node_id='{container['id']}') 展开后再操作。"
        )
    raise ArchEngineError(f"节点 '{node_id}' 不存在。")


def _next_focus_hole(graph: Dict[str, Any]) -> Optional[str]:
    """Cognitive serialization: the most upstream hole is settled first."""
    holes = [n["id"] for n in graph["nodes"] if n["status"] == "hole"]
    if not holes:
        return None
    hole_ids = set(holes)
    return next((nid for nid in _topological_order(graph) if nid in hole_ids), holes[0])


def _compact_node(node: Dict[str, Any]) -> Dict[str, Any]:
    """Super nodes appear in slices as one module: children shown as id/label only."""
    if node["status"] != "super_node":
        return node
    view = {k: v for k, v in node.items()
            if k not in ("children", "inner_edges")}
    view["folded_children"] = [
        {"id": c["id"], "label": c["label"]}
        if c["status"] == "super_node" else
        {"id": c["id"], "label": c["label"], "impl": _node_state(c)["impl"]}
        for c in node["children"]
    ]
    leaves = [c for c in _iter_all_nodes(node["children"]) if c["status"] != "super_node"]
    built = sum(1 for c in leaves if _node_state(c)["impl"] == "built")
    view["impl_progress"] = f"{built}/{len(leaves)} built"
    return view


def _node_view(node: Dict[str, Any]) -> Dict[str, Any]:
    """A node as shown in slices and task cards: state fields filled with defaults."""
    if node["status"] == "super_node":
        return _compact_node(node)
    state = _node_state(node)
    view = dict(node)
    view["impl"] = state["impl"]
    view["verify"] = state["verify"]
    view["refs"] = state["refs"]
    return view


def _flatten_folds(graph: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Expand every super node into its original nodes and edges.

    Returns the flat graph and a map from each flat node to the top-level node
    that currently shows it (itself, or the outermost super node holding it).
    Edges are taken from their members, so an edge drawn to a module attaches
    to the child it really reaches. An endpoint that is still a super node
    (an edge attached while folded) resolves through the module's entry /
    exit children; when there are several, the edge connects to all of them.
    """
    flat_nodes: List[Dict[str, Any]] = []
    top_of: Dict[str, str] = {}
    supers: Dict[str, Dict[str, Any]] = {}
    for top in graph["nodes"]:
        for n in _iter_all_nodes([top]):
            if n["status"] == "super_node":
                supers[n["id"]] = n
            else:
                flat_nodes.append(n)
                top_of[n["id"]] = top["id"]

    def resolve(nid: str, side: str, seen: frozenset = frozenset()) -> List[str]:
        if nid in top_of:
            return [nid]
        if nid not in supers or nid in seen:
            return []
        candidates = supers[nid]["entry_children" if side == "to" else "exit_children"]
        return [leaf for c in candidates for leaf in resolve(c, side, seen | {nid})]

    raw = list(graph["edges"]) + [e for sn in supers.values() for e in sn.get("inner_edges", [])]
    flat_edges = []
    for e in raw:
        for m in _edge_members(e):
            # An endpoint that is still ambiguous connects to every candidate:
            # extra dependencies can only remove entries and exits, so the
            # skeleton is never reported ready on a guess.
            for u in resolve(m["from"], "from"):
                for v in resolve(m["to"], "to"):
                    flat_edges.append({"from": u, "to": v, "contract": m.get("contract", "")})
    return {"nodes": flat_nodes, "edges": flat_edges}, top_of


def _solid_path(graph: Dict[str, Any]) -> Optional[List[str]]:
    """BFS for a path of >= 2 solid nodes from a real entry to a real exit."""
    nodes = {n["id"]: n for n in graph["nodes"]}
    adj, in_deg, out_deg = _active_topology(graph)
    solid_ids = {nid for nid in adj if nodes[nid]["status"] in SOLID_STATUSES}
    starts = [nid for nid in adj if in_deg[nid] == 0 and nid in solid_ids]
    ends = {nid for nid in adj if out_deg[nid] == 0 and nid in solid_ids}
    for s in starts:
        queue = deque([[s]])
        visited = {s}
        while queue:
            path = queue.popleft()
            curr = path[-1]
            if curr in ends and len(path) >= 2:
                return path
            for nxt in adj[curr]:
                if nxt in solid_ids and nxt not in visited:
                    visited.add(nxt)
                    queue.append(path + [nxt])
    return None


def _find_walking_skeleton(graph: Dict[str, Any]) -> Optional[List[str]]:
    """A complete solid path from a real entry to a real exit, in top-level ids.

    Entries and exits are judged on the whole active graph (fog excluded), not on
    the solid sub-graph alone; otherwise a solid node whose successor is still a
    hole would wrongly count as an exit. Folded modules are expanded first, so a
    path must really get through a module's interior (in through one child, out
    through a reachable one); steps inside one module collapse to its id, and a
    trunk folded whole is reported as that single module.
    """
    flat, top_of = _flatten_folds(graph)
    path = _solid_path(flat)
    if not path:
        return None
    shown: List[str] = []
    for nid in path:
        if not shown or shown[-1] != top_of[nid]:
            shown.append(top_of[nid])
    return shown


def _topological_order(graph: Dict[str, Any]) -> List[str]:
    """Kahn order of the active graph; nodes left over (cycles) keep insertion order."""
    adj, in_deg, _ = _active_topology(graph)
    order_index = {n["id"]: i for i, n in enumerate(graph["nodes"])}
    remaining = dict(in_deg)
    ready = sorted((nid for nid, d in remaining.items() if d == 0), key=order_index.get)
    order: List[str] = []
    while ready:
        curr = ready.pop(0)
        order.append(curr)
        for nxt in adj[curr]:
            remaining[nxt] -= 1
            if remaining[nxt] == 0:
                ready.append(nxt)
        ready.sort(key=order_index.get)
    seen = set(order)
    order += [n["id"] for n in graph["nodes"] if n["id"] in adj and n["id"] not in seen]
    return order


def _check_node_drift(node: Dict[str, Any]) -> None:
    files = [f for f in _node_files(node) if Path(f).exists()]
    last_tag = node.get("last_tag")
    if not files or not last_tag or not SHADOW_GIT_DIR.exists():
        return
    file_path = ", ".join(files)

    diff_res = _run_shadow_git(["diff", last_tag, "--"] + files, check=False)
    if diff_res.stdout.strip():
        sig_changed = any(
            not line.startswith(("+++", "---")) and SIGNATURE_LINE.match(line)
            for line in diff_res.stdout.splitlines()
        )
        if sig_changed:
            raise ArchEngineError(
                f"[ContractDriftError] 检测到人类手动修改了 {file_path} 的函数/接口签名！\n"
                f"JIT 指令：\n"
                f"1. 严禁直接覆盖该文件。\n"
                f"2. 立即向用户确认：是以当前代码签名为准同步更新上下游契约，还是回退签名？\n"
                f"3. 确认后运行 sync-manual 更新快照。"
            )
        raise ArchEngineError(
            f"[ManualDriftError] 检测到人类手动微调了 {file_path} 的内部实现！\n"
            f"JIT 指令：\n"
            f"1. 严禁直接覆盖该文件。\n"
            f"2. 拉起轻量子代理阅读 diff 并用一句话总结人类改动。\n"
            f"3. 调用 sync-manual (node='{node['id']}', summary='...') 更新影子快照后再继续。"
        )


# --------------------------------------------------------------- node state
#
# `status` (hole / solid / fog / super_node) says whether a decision is made.
# The fields below say how far the decided node has got in reality. Graphs
# written before they existed have none of them; _node_state fills defaults.

IMPL_LEVELS = ("planned", "building", "built")
VERIFY_LEVELS = ("none", "unit", "api", "device")
IMPL_TEXT = {"planned": "未实现", "building": "实现中", "built": "已实现"}
VERIFY_TEXT = {"none": "未验证", "unit": "单测", "api": "接口实测", "device": "真机实测"}


def _node_files(node: Dict[str, Any]) -> List[str]:
    """`file_path` holds one path (string, the original format) or several (list)."""
    raw = node.get("file_path") or ""
    items = raw if isinstance(raw, list) else [raw]
    return [str(p).strip() for p in items if str(p).strip()]


def _store_files(node: Dict[str, Any], files: Iterable[str]) -> None:
    unique = list(dict.fromkeys(f.strip() for f in files if f and f.strip()))
    node["file_path"] = unique[0] if len(unique) == 1 else (unique if unique else "")


def _node_state(node: Dict[str, Any]) -> Dict[str, Any]:
    """impl / verify / refs / files with defaults for graphs that predate them.

    A node without `impl` counts as built when it has an implementation
    snapshot (commit-node / sync-manual set `last_tag`), otherwise planned.
    """
    impl = node.get("impl")
    if impl not in IMPL_LEVELS:
        impl = "built" if node.get("last_tag") else "planned"
    verify = node.get("verify")
    if isinstance(verify, str):
        verify = {"level": verify, "date": ""}
    if not isinstance(verify, dict) or verify.get("level") not in VERIFY_LEVELS:
        verify = {"level": "none", "date": ""}
    refs = [str(r).strip() for r in (node.get("refs") or []) if str(r).strip()]
    return {
        "impl": impl,
        "verify": {"level": verify["level"], "date": verify.get("date") or ""},
        "refs": refs,
        "files": _node_files(node),
    }


def _state_text(node: Dict[str, Any]) -> str:
    st = _node_state(node)
    verify = VERIFY_TEXT[st["verify"]["level"]]
    if st["verify"]["date"]:
        verify += f" {st['verify']['date']}"
    return f"{IMPL_TEXT[st['impl']]} · {verify}"


def _find_any_node(graph: Dict[str, Any], node_id: str) -> Optional[Dict[str, Any]]:
    """Find a node at any depth, folded children included."""
    return next((n for n in _iter_all_nodes(graph["nodes"]) if n["id"] == node_id), None)


def _reject_manual_super_type(node_type: str) -> None:
    if node_type == "super_node":
        raise ArchEngineError(
            "超级节点只能通过 arch_fold 折叠已确定的子图生成，不能用 collapse / add-node 直接指定类型。"
        )


def _merge_contracts(contracts: List[str]) -> str:
    unique = list(dict.fromkeys(c for c in contracts if c))
    return " | ".join(unique) if unique else "TBD"


# Edge provenance for folding
# ---------------------------
# An edge that touches a super node may stand for several original edges. It
# carries them in "members" (each a plain {from, to, contract}) together with
# "auto_contract", the merged contract the engine wrote. Provenance lives on
# the edge rather than inside the super node, so it travels with the edge when
# the neighbour on the other side is folded or unfolded later, in any order.


def _edge_members(e: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Original edges this edge stands for; a contract edited while folded wins."""
    members = e.get("members")
    if not members:
        return [{"from": e["from"], "to": e["to"], "contract": e.get("contract", "")}]
    if "auto_contract" in e and e.get("contract") != e["auto_contract"]:
        return [{**m, "contract": e.get("contract", "")} for m in members]
    return [dict(m) for m in members]


def _edge_from_members(u: str, v: str, members: List[Dict[str, Any]]) -> Dict[str, Any]:
    if len(members) == 1 and (members[0]["from"], members[0]["to"]) == (u, v):
        return {"from": u, "to": v, "contract": members[0].get("contract", "")}
    contract = _merge_contracts([m.get("contract", "") for m in members])
    return {"from": u, "to": v, "contract": contract, "auto_contract": contract, "members": members}


def _public_edge(e: Dict[str, Any]) -> Dict[str, Any]:
    """Edge as shown to the agent: provenance bookkeeping left out."""
    return {k: v for k, v in e.items() if k not in ("members", "auto_contract")}
