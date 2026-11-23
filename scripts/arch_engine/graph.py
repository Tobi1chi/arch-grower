"""Graph model: multi-topology containers, folded/promoted subsystems, walking
skeleton, edge provenance, node state (impl / verify / refs / files) and drift
detection."""

import re
from collections import deque
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .common import (
    ArchEngineError, EDGE_KINDS, NODE_ROLES, SHADOW_GIT_DIR, TOPOLOGIES, _run_shadow_git,
)


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


def _edge_kind(edge: Dict[str, Any]) -> str:
    kind = edge.get("kind") or "primary"
    return kind if kind in EDGE_KINDS else "primary"


def _is_feedback(edge: Dict[str, Any]) -> bool:
    return _edge_kind(edge) == "feedback"


def _validate_topology(topology: str) -> str:
    topo = (topology or "pipeline").strip()
    if topo not in TOPOLOGIES:
        raise ArchEngineError(
            f"未知拓扑类型 '{topo}'。可选：{' / '.join(TOPOLOGIES)}。"
        )
    return topo


def _validate_edge_kind(kind: str) -> str:
    k = (kind or "primary").strip()
    if k not in EDGE_KINDS:
        raise ArchEngineError(
            f"未知连线类型 '{k}'。可选：{' / '.join(EDGE_KINDS)}。"
        )
    return k


def _validate_node_role(role: str) -> str:
    r = (role or "").strip()
    if r not in NODE_ROLES:
        raise ArchEngineError(
            f"未知节点角色 '{r}'。可选：entry / exit / hub / spoke（或留空）。"
        )
    return r


def _container_nodes(container: Dict[str, Any]) -> List[Dict[str, Any]]:
    return container["children"] if "children" in container else container["nodes"]


def _container_edges(container: Dict[str, Any]) -> List[Dict[str, Any]]:
    return container["inner_edges"] if "inner_edges" in container else container["edges"]


def _container_topology(container: Dict[str, Any]) -> str:
    topo = container.get("topology") or "pipeline"
    return topo if topo in TOPOLOGIES else "pipeline"


def _resolve_container(
    graph: Dict[str, Any], scope: str = "",
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]], int, List[str]]:
    """Return (container, nodes, edges, depth, path) for root or a nested super_node."""
    if not scope:
        return graph, graph["nodes"], graph["edges"], 0, []

    def search(
        nodes: List[Dict[str, Any]], depth: int, path: List[str],
    ) -> Optional[Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]], int, List[str]]]:
        for n in nodes:
            if n["id"] == scope:
                if n["status"] != "super_node":
                    raise ArchEngineError(
                        f"节点 '{scope}' 不是子系统超级节点（当前状态：{n['status']}），不能作为作用域。"
                    )
                return n, n["children"], n["inner_edges"], depth + 1, path + [scope]
            if n["status"] == "super_node":
                found = search(n.get("children", []), depth + 1, path + [n["id"]])
                if found is not None:
                    return found
        return None

    res = search(graph["nodes"], 0, [])
    if res is None:
        raise ArchEngineError(f"子系统作用域 '{scope}' 不存在。")
    return res


def _parent_container_of(graph: Dict[str, Any], target_id: str) -> Optional[Dict[str, Any]]:
    """Return the immediate container (root graph or super_node) holding target_id."""
    def search(container: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for n in _container_nodes(container):
            if n["id"] == target_id:
                return container
            if n["status"] == "super_node":
                found = search(n)
                if found is not None:
                    return found
        return None

    return search(graph)


def _max_subtree_depth(nodes: List[Dict[str, Any]]) -> int:
    """Maximum nesting depth below a node list (0 when no super_nodes exist)."""
    best = 0
    for n in nodes:
        if n.get("status") == "super_node":
            best = max(best, 1 + _max_subtree_depth(n.get("children", [])))
    return best


def _active_topology(
    container: Dict[str, Any], include_feedback: bool = True,
) -> Tuple[Dict[str, List[str]], Dict[str, int], Dict[str, int]]:
    """Adjacency and degrees of the active nodes (every node that is not fog)."""
    nodes = _container_nodes(container)
    edges = _container_edges(container)
    active_ids = {n["id"] for n in nodes if n["status"] != "fog"}
    adj: Dict[str, List[str]] = {nid: [] for nid in active_ids}
    in_deg: Dict[str, int] = {nid: 0 for nid in active_ids}
    out_deg: Dict[str, int] = {nid: 0 for nid in active_ids}
    for e in edges:
        if not include_feedback and _is_feedback(e):
            continue
        u, v = e["from"], e["to"]
        if u in active_ids and v in active_ids:
            adj[u].append(v)
            in_deg[v] += 1
            out_deg[u] += 1
            if _edge_kind(e) == "bidirectional" and u != v:
                adj[v].append(u)
                in_deg[u] += 1
                out_deg[v] += 1
    return adj, in_deg, out_deg


def _iter_all_nodes(nodes: List[Dict[str, Any]]):
    """Yield every node, descending into the children of folded super nodes."""
    for n in nodes:
        yield n
        yield from _iter_all_nodes(n.get("children", []))


def _find_container(container: Dict[str, Any], node_id: str) -> Optional[Dict[str, Any]]:
    """Return the direct super_node child of `container` that (transitively) holds node_id."""
    for n in _container_nodes(container):
        if n["status"] == "super_node" and any(
            c["id"] == node_id for c in _iter_all_nodes(n.get("children", []))
        ):
            return n
    return None


def _get_node(
    container: Dict[str, Any], node_id: str, allow_super: bool = True,
) -> Dict[str, Any]:
    """Look up a direct child node in `container`, with JIT guidance for super nodes."""
    for n in _container_nodes(container):
        if n["id"] == node_id:
            if not allow_super and n["status"] == "super_node":
                raise ArchEngineError(
                    f"节点 '{node_id}' 是折叠后的超级节点，不能直接执行该操作。\n"
                    f"JIT 指令：可传入 --in {node_id} 进入该子系统操作其内部节点，或先调用 arch_unfold(node_id='{node_id}') 展开。"
                )
            return n
    holder = _find_container(container, node_id)
    if holder:
        raise ArchEngineError(
            f"节点 '{node_id}' 已折叠在超级节点 '{holder['id']}' 内。\n"
            f"JIT 指令：使用 --in {holder['id']} 在子系统作用域内操作，或先调用 arch_unfold(node_id='{holder['id']}') 展开后再操作。"
        )
    raise ArchEngineError(f"节点 '{node_id}' 不存在。")


def _sccs(
    node_ids: Iterable[str], edges: List[Dict[str, Any]], include_feedback: bool = True,
) -> Dict[str, int]:
    """Tarjan's strongly connected components: node_id -> component index."""
    ids = list(node_ids)
    id_set = set(ids)
    adj: Dict[str, List[str]] = {nid: [] for nid in ids}
    for e in edges:
        if not include_feedback and _is_feedback(e):
            continue
        u, v = e["from"], e["to"]
        if u in id_set and v in id_set:
            adj[u].append(v)
            if _edge_kind(e) == "bidirectional" and u != v:
                adj[v].append(u)

    index_counter = 0
    indices: Dict[str, int] = {}
    lowlink: Dict[str, int] = {}
    stack: List[str] = []
    on_stack: Set[str] = set()
    comp_of: Dict[str, int] = {}
    comp_id = 0

    def strongconnect(v: str) -> None:
        nonlocal index_counter, comp_id
        indices[v] = lowlink[v] = index_counter
        index_counter += 1
        stack.append(v)
        on_stack.add(v)
        for w in adj[v]:
            if w not in indices:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], indices[w])
        if lowlink[v] == indices[v]:
            while True:
                w = stack.pop()
                on_stack.remove(w)
                comp_of[w] = comp_id
                if w == v:
                    break
            comp_id += 1

    for nid in ids:
        if nid not in indices:
            strongconnect(nid)
    return comp_of


def _next_focus_hole(container: Dict[str, Any]) -> Optional[str]:
    """Cognitive serialization: pick the next hole to settle based on topology."""
    nodes = _container_nodes(container)
    holes = [n for n in nodes if n["status"] == "hole"]
    if not holes:
        return None
    hole_ids = {n["id"] for n in holes}
    topo = _container_topology(container)

    if topo == "star":
        hub_hole = next((n["id"] for n in holes if n.get("role") == "hub"), None)
        if hub_hole:
            return hub_hole
        adj, in_deg, out_deg = _active_topology(container, include_feedback=True)
        order_idx = {n["id"]: i for i, n in enumerate(nodes)}
        return max(
            (n["id"] for n in holes),
            key=lambda nid: (in_deg.get(nid, 0) + out_deg.get(nid, 0), -order_idx[nid]),
        )

    if topo == "ring":
        active = [n for n in nodes if n["status"] != "fog"]
        start = next((n["id"] for n in active if n.get("role") == "entry"), active[0]["id"])
        adj_pri, _, _ = _active_topology(container, include_feedback=False)
        adj_all, _, _ = _active_topology(container, include_feedback=True)
        visited: Set[str] = {start}
        queue: deque = deque([start])
        while queue:
            curr = queue.popleft()
            if curr in hole_ids:
                return curr
            for nxt in adj_pri.get(curr, []) + adj_all.get(curr, []):
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append(nxt)
        return holes[0]["id"]

    return next((nid for nid in _topological_order(container) if nid in hole_ids), holes[0]["id"])


def _compact_node(node: Dict[str, Any]) -> Dict[str, Any]:
    """Super nodes appear in slices as one module: children shown as id/label only."""
    if node["status"] != "super_node":
        return node
    view = {k: v for k, v in node.items()
            if k not in ("children", "inner_edges")}
    view["topology"] = _container_topology(node)
    view["sub_ready"] = _is_super_ready(node)
    view["folded_children"] = [
        {"id": c["id"], "label": c["label"], "status": c["status"], "topology": _container_topology(c)}
        if c["status"] == "super_node" else
        {"id": c["id"], "label": c["label"], "status": c["status"], "impl": _node_state(c)["impl"]}
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


def _is_super_ready(sn: Dict[str, Any], _seen: Optional[Set[str]] = None) -> bool:
    """True when a super_node subsystem has a runnable walking skeleton under its own topology."""
    if sn.get("status") != "super_node" or not sn.get("children"):
        return False
    seen = set(_seen or ())
    if sn["id"] in seen:
        return False
    seen.add(sn["id"])
    return bool(_container_skeleton(sn, _seen=seen))


def _flatten_folds(
    container: Dict[str, Any], _seen: Optional[Set[str]] = None,
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Expand pipeline super_nodes into their original nodes and edges.

    Heterogeneous subsystems (super_nodes with topology 'ring' or 'star') stay
    atomic so their internal cycles or hub-spoke graphs never break the parent
    pipeline's entry/exit analysis; their effective status is 'solid' iff their
    own subsystem skeleton is ready, else 'hole'.
    """
    flat_nodes: List[Dict[str, Any]] = []
    top_of: Dict[str, str] = {}
    supers: Dict[str, Dict[str, Any]] = {}

    def walk(node: Dict[str, Any], top_id: str) -> None:
        if node["status"] == "super_node":
            if _container_topology(node) != "pipeline":
                eff = dict(node)
                eff["status"] = "solid" if _is_super_ready(node, _seen=_seen) else "hole"
                flat_nodes.append(eff)
                top_of[node["id"]] = top_id
                return
            supers[node["id"]] = node
            for ch in node.get("children", []):
                walk(ch, top_id)
        else:
            flat_nodes.append(node)
            top_of[node["id"]] = top_id

    for top in _container_nodes(container):
        walk(top, top["id"])

    def resolve(nid: str, side: str, seen: frozenset = frozenset()) -> List[str]:
        if nid in top_of:
            return [nid]
        if nid not in supers or nid in seen:
            return []
        candidates = supers[nid]["entry_children" if side == "to" else "exit_children"]
        return [leaf for c in candidates for leaf in resolve(c, side, seen | {nid})]

    raw = list(_container_edges(container)) + [e for sn in supers.values() for e in sn.get("inner_edges", [])]
    flat_edges = []
    for e in raw:
        ekind = _edge_kind(e)
        for m in _edge_members(e):
            mkind = m.get("kind") or ekind
            # An endpoint that is still ambiguous connects to every candidate:
            # extra dependencies can only remove entries and exits, so the
            # skeleton is never reported ready on a guess.
            for u in resolve(m["from"], "from"):
                for v in resolve(m["to"], "to"):
                    fe: Dict[str, Any] = {"from": u, "to": v, "contract": m.get("contract", "")}
                    if mkind != "primary":
                        fe["kind"] = mkind
                    flat_edges.append(fe)
    return {"nodes": flat_nodes, "edges": flat_edges}, top_of


def _solid_path(container: Dict[str, Any]) -> Optional[List[str]]:
    """BFS for a path of >= 2 solid nodes from a real entry to a real exit (ignoring feedback edges)."""
    nodes = {n["id"]: n for n in _container_nodes(container)}
    adj, in_deg, out_deg = _active_topology(container, include_feedback=False)
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


def _effective_solid_ids(container: Dict[str, Any], _seen: Optional[Set[str]] = None) -> Set[str]:
    solid: Set[str] = set()
    for n in _container_nodes(container):
        if n["status"] == "solid":
            solid.add(n["id"])
        elif n["status"] == "super_node" and _is_super_ready(n, _seen=_seen):
            solid.add(n["id"])
    return solid


def _solid_ring(container: Dict[str, Any], _seen: Optional[Set[str]] = None) -> Optional[List[str]]:
    """Find a directed cycle of >= 2 distinct solid nodes in a ring container."""
    nodes = _container_nodes(container)
    adj, _, _ = _active_topology(container, include_feedback=True)
    solid_ids = _effective_solid_ids(container, _seen=_seen)
    ordered = [n["id"] for n in nodes if n["id"] in solid_ids]
    # Prefer starting from an explicit entry anchor so the reported cycle reads naturally.
    anchors = [n["id"] for n in nodes if n["id"] in solid_ids and n.get("role") == "entry"]
    starts = anchors + [nid for nid in ordered if nid not in anchors]

    for s in starts:
        queue: deque = deque([[s]])
        visited: Set[str] = set()
        while queue:
            path = queue.popleft()
            curr = path[-1]
            for nxt in adj.get(curr, []):
                if nxt == s and len(path) >= 2:
                    return path + [s]
                if nxt in solid_ids and nxt not in visited and nxt != s:
                    visited.add(nxt)
                    queue.append(path + [nxt])
    return None


def _star_hub_id(container: Dict[str, Any]) -> Optional[str]:
    """Return the hub node id of a star container (role=='hub', else highest active degree)."""
    nodes = [n for n in _container_nodes(container) if n["status"] != "fog"]
    if not nodes:
        return None
    explicit = next((n["id"] for n in nodes if n.get("role") == "hub"), None)
    if explicit:
        return explicit
    adj, in_deg, out_deg = _active_topology(container, include_feedback=True)
    order_idx = {n["id"]: i for i, n in enumerate(nodes)}
    return max(
        (n["id"] for n in nodes),
        key=lambda nid: (in_deg.get(nid, 0) + out_deg.get(nid, 0), -order_idx[nid]),
    )


def _solid_star(container: Dict[str, Any], _seen: Optional[Set[str]] = None) -> Optional[List[str]]:
    """A star skeleton is ready when its hub and >= 2 connected spokes are solid."""
    hub = _star_hub_id(container)
    if not hub:
        return None
    solid_ids = _effective_solid_ids(container, _seen=_seen)
    if hub not in solid_ids:
        return None
    nodes = _container_nodes(container)
    active_ids = {n["id"] for n in nodes if n["status"] != "fog"}
    neighbors: Set[str] = set()
    for e in _container_edges(container):
        u, v = e["from"], e["to"]
        if u not in active_ids or v not in active_ids:
            continue
        if u == hub and v != hub:
            neighbors.add(v)
        elif v == hub and u != hub:
            neighbors.add(u)
    ordered_spokes = [n["id"] for n in nodes if n["id"] in neighbors and n["id"] in solid_ids]
    all_spokes = [n["id"] for n in nodes if n["id"] in neighbors]
    holes = [n for n in nodes if n["status"] == "hole"]
    if len(ordered_spokes) >= 2 or (len(ordered_spokes) == 1 and len(all_spokes) == 1 and not holes):
        return [hub, *ordered_spokes]
    return None


def _container_skeleton(
    container: Dict[str, Any], _seen: Optional[Set[str]] = None,
) -> Optional[List[str]]:
    """Topology-aware walking skeleton for a single container (root or super_node)."""
    topo = _container_topology(container)
    if topo == "ring":
        return _solid_ring(container, _seen=_seen)
    if topo == "star":
        return _solid_star(container, _seen=_seen)

    flat, top_of = _flatten_folds(container, _seen=_seen)
    path = _solid_path(flat)
    if not path:
        return None
    shown: List[str] = []
    for nid in path:
        if not shown or shown[-1] != top_of[nid]:
            shown.append(top_of[nid])
    return shown


def _find_walking_skeleton(graph: Dict[str, Any], scope: str = "") -> Optional[List[str]]:
    """Find the walking skeleton of root or a named super_node subsystem."""
    container, _, _, _, _ = _resolve_container(graph, scope)
    return _container_skeleton(container)


def _skeleton_banner(container: Dict[str, Any], skeleton: List[str]) -> str:
    topo = _container_topology(container)
    if topo == "ring":
        return f"[WALKING_SKELETON_READY] 检测到无空洞可运行闭环: {' -> '.join(skeleton)}"
    if topo == "star":
        hub, spokes = skeleton[0], skeleton[1:]
        return f"[WALKING_SKELETON_READY] 检测到无空洞可运行星形中枢: {hub} <-> [{', '.join(spokes)}]"
    return f"[WALKING_SKELETON_READY] 检测到无空洞可运行主干: {' -> '.join(skeleton)}"


def _topological_order(container: Dict[str, Any]) -> List[str]:
    """Kahn order of the active container (ignoring feedback edges); cycles keep insertion order."""
    nodes = _container_nodes(container)
    adj, in_deg, _ = _active_topology(container, include_feedback=False)
    order_index = {n["id"]: i for i, n in enumerate(nodes)}
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
    order += [n["id"] for n in nodes if n["id"] in adj and n["id"] not in seen]
    return order


def _longest_primary_chain(container: Dict[str, Any]) -> int:
    """Longest directed node chain along primary (non-feedback, non-bidirectional) edges."""
    nodes = _container_nodes(container)
    active_ids = {n["id"] for n in nodes if n["status"] != "fog"}
    if not active_ids:
        return 0
    adj: Dict[str, List[str]] = {nid: [] for nid in active_ids}
    in_deg: Dict[str, int] = {nid: 0 for nid in active_ids}
    for e in _container_edges(container):
        if _edge_kind(e) != "primary":
            continue
        u, v = e["from"], e["to"]
        if u in active_ids and v in active_ids and u != v:
            adj[u].append(v)
            in_deg[v] += 1
    dist: Dict[str, int] = {nid: 1 for nid in active_ids}
    queue = deque(nid for nid, d in in_deg.items() if d == 0)
    while queue:
        u = queue.popleft()
        for v in adj[u]:
            if dist[u] + 1 > dist[v]:
                dist[v] = dist[u] + 1
            in_deg[v] -= 1
            if in_deg[v] == 0:
                queue.append(v)
    return max(dist.values(), default=0)


def _max_active_degree(container: Dict[str, Any]) -> Tuple[str, int]:
    """Highest distinct active-neighbor count in the container."""
    nodes = _container_nodes(container)
    active_ids = {n["id"] for n in nodes if n["status"] != "fog"}
    nbrs: Dict[str, Set[str]] = {nid: set() for nid in active_ids}
    for e in _container_edges(container):
        u, v = e["from"], e["to"]
        if u in active_ids and v in active_ids and u != v:
            nbrs[u].add(v)
            nbrs[v].add(u)
    if not nbrs:
        return "", 0
    best_id = max(nbrs, key=lambda nid: len(nbrs[nid]))
    return best_id, len(nbrs[best_id])


def _shortest_active_cycle_len(container: Dict[str, Any]) -> int:
    """Length (distinct nodes) of the main cycle in a ring container, or 0 if none."""
    nodes = [n for n in _container_nodes(container) if n["status"] != "fog"]
    if not nodes:
        return 0
    adj, _, _ = _active_topology(container, include_feedback=True)
    anchors = [n["id"] for n in nodes if n.get("role") == "entry"] or [nodes[0]["id"]]
    for s in anchors:
        queue: deque = deque([(s, 0)])
        visited: Set[str] = set()
        while queue:
            curr, d = queue.popleft()
            for nxt in adj.get(curr, []):
                if nxt == s and d + 1 >= 2:
                    return d + 1
                if nxt not in visited and nxt != s:
                    visited.add(nxt)
                    queue.append((nxt, d + 1))
    return 0


def _container_warnings(container: Dict[str, Any], cfg: Dict[str, Any]) -> List[str]:
    """Preset-driven soft guardrail warnings for a container."""
    nodes = _container_nodes(container)
    holes = [n for n in nodes if n["status"] == "hole"]
    solids = [n for n in nodes if n["status"] == "solid"]
    topo = _container_topology(container)
    warnings: List[str] = []

    max_holes = int(cfg.get("max_active_holes", 3))
    max_solids = int(cfg.get("max_unfolded_solid", 7))
    max_chain = int(cfg.get("max_chain_len", 6))
    max_hub = int(cfg.get("max_hub_degree", 6))
    max_cycle = int(cfg.get("max_cycle_len", 5))

    if len(holes) > max_holes:
        warnings.append(
            f"[CIRCUIT_BREAKER_WARNING] 当前未决空洞数 ({len(holes)}) 已超过上限 ({max_holes})！"
            "请先收敛现有空洞，禁止继续发散新分支。"
        )
    if len(solids) > max_solids:
        warnings.append(
            f"[CIRCUIT_BREAKER_WARNING] 当前活跃实心节点 ({len(solids)}) 超过 {max_solids} 个，"
            "建议调用 arch_fold 将已稳定的子图折叠为超级节点，以降低认知负荷。"
        )
    if topo == "pipeline":
        chain = _longest_primary_chain(container)
        if chain > max_chain:
            warnings.append(
                f"[CHAIN_TOO_LONG] 主干链长已达 {chain}（preset 上限 {max_chain}），"
                "建议用 arch_fold 将连续稳定段折叠为子系统，或用 arch_config --max-chain-len 调整。"
            )
    if topo == "ring":
        cyc = _shortest_active_cycle_len(container)
        if cyc > max_cycle:
            warnings.append(
                f"[CYCLE_TOO_LONG] 主闭环长度已达 {cyc}（preset 上限 {max_cycle}），"
                "建议用 arch_fold 收拢环内中间步骤，或用 arch_config --max-cycle-len 调整。"
            )
    top_node, deg = _max_active_degree(container)
    if deg > max_hub:
        warnings.append(
            f"[HUB_OVERLOADED] 节点 '{top_node}' 连接了 {deg} 个活跃邻居（preset 上限 {max_hub}），"
            "建议把同类分支收拢为子系统超级节点，或用 arch_config --max-hub-degree 调整。"
        )
    return warnings


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
            "超级节点只能通过 arch_fold 折叠子图或 arch_promote 升级子系统生成，不能用 collapse / add-node 直接指定类型。"
        )


def _merge_contracts(contracts: List[str]) -> str:
    unique = list(dict.fromkeys(c for c in contracts if c))
    return " | ".join(unique) if unique else "TBD"


# Edge provenance for folding
# ---------------------------
# An edge that touches a super node may stand for several original edges. It
# carries them in "members" (each a plain {from, to, contract[, kind]}) together
# with "auto_contract", the merged contract the engine wrote. Provenance lives
# on the edge rather than inside the super node, so it travels with the edge
# when the neighbour on the other side is folded or unfolded later, in any order.


def _edge_members(e: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Original edges this edge stands for; a contract edited while folded wins."""
    members = e.get("members")
    if not members:
        item = {"from": e["from"], "to": e["to"], "contract": e.get("contract", "")}
        if _edge_kind(e) != "primary":
            item["kind"] = _edge_kind(e)
        return [item]
    if "auto_contract" in e and e.get("contract") != e["auto_contract"]:
        return [{**m, "contract": e.get("contract", "")} for m in members]
    return [dict(m) for m in members]


def _edge_from_members(u: str, v: str, members: List[Dict[str, Any]]) -> Dict[str, Any]:
    kinds = {_edge_kind(m) for m in members}
    if len(members) == 1 and (members[0]["from"], members[0]["to"]) == (u, v):
        out: Dict[str, Any] = {"from": u, "to": v, "contract": members[0].get("contract", "")}
        if _edge_kind(members[0]) != "primary":
            out["kind"] = _edge_kind(members[0])
        return out
    contract = _merge_contracts([m.get("contract", "") for m in members])
    out = {"from": u, "to": v, "contract": contract, "auto_contract": contract, "members": members}
    if len(kinds) == 1:
        only_kind = next(iter(kinds))
        if only_kind != "primary":
            out["kind"] = only_kind
    return out


def _public_edge(e: Dict[str, Any]) -> Dict[str, Any]:
    """Edge as shown to the agent: provenance bookkeeping left out."""
    return {k: v for k, v in e.items() if k not in ("members", "auto_contract")}
