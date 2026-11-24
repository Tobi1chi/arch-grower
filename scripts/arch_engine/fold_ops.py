"""Fold a settled sub-graph into a super node, unfold it exactly, or promote a
node top-down into an isolated sub-system with its own topology."""

from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from .common import ArchEngineError, _load_config, _load_graph, _save_graph, _shadow_commit
from .graph import (
    SOLID_STATUSES, _container_topology, _edge_from_members, _edge_kind, _edge_members,
    _get_node, _is_feedback, _iter_all_nodes, _max_subtree_depth, _resolve_container, _sccs,
    _validate_topology,
)
from .views import op_focus


def _infer_folded_topology(
    members: Dict[str, Dict[str, Any]], inner: List[Dict[str, Any]],
) -> str:
    """Infer an internal topology for a folded subgraph when none was given explicitly.

    Avoids inheriting `ring` onto an open arc (which has no internal cycle and
    would make `_is_super_ready` permanently False), while keeping star fragments
    connected by bidirectional edges runnable as `star`.
    """
    hubs = [nid for nid, n in members.items() if n.get("role") == "hub"]
    for h in hubs:
        nbrs = {
            e["to"] if e["from"] == h else e["from"]
            for e in inner if h in (e["from"], e["to"]) and e["from"] != e["to"]
        }
        if nbrs:
            return "star"

    directed_inner = [e for e in inner if _edge_kind(e) != "bidirectional"]
    scc_sizes = Counter(_sccs(members.keys(), directed_inner, include_feedback=True).values())
    if any(sz >= 2 for sz in scc_sizes.values()):
        return "ring"

    if inner and all(_edge_kind(e) == "bidirectional" for e in inner):
        return "star"
    return "pipeline"


def _replace_container_lists(
    container: Dict[str, Any], nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]],
) -> None:
    if "children" in container:
        container["children"] = nodes
        container["inner_edges"] = edges
    else:
        container["nodes"] = nodes
        container["edges"] = edges


def _starter_subsystem_skeleton(
    prefix: str, topology: str, stage: str = "",
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str], List[str]]:
    """Build starter hole nodes, inner_edges, entry_children and exit_children for `promote`."""
    def mk(nid: str, label: str, spec: str, role: str = "") -> Dict[str, Any]:
        node: Dict[str, Any] = {
            "id": nid,
            "label": label,
            "status": "hole",
            "type": "port",
            "spec": spec,
            "file_path": "",
            "last_tag": "",
        }
        if stage:
            node["stage"] = stage
        if role:
            node["role"] = role
        return node

    if topology == "star":
        hub, sa, sb = f"{prefix}_hub", f"{prefix}_spoke_a", f"{prefix}_spoke_b"
        children = [
            mk(hub, "? 子系统中枢", "待定义子系统中心总线/路由器的协议与分发职责", role="hub"),
            mk(sa, "? 外围模块 A", "待定义接入子系统中枢的第一个模块", role="spoke"),
            mk(sb, "? 外围模块 B", "待定义接入子系统中枢的第二个模块", role="spoke"),
        ]
        inner_edges = [
            {"from": hub, "to": sa, "contract": "TBD", "kind": "bidirectional"},
            {"from": hub, "to": sb, "contract": "TBD", "kind": "bidirectional"},
        ]
        return children, inner_edges, [hub], [hub]

    if topology == "ring":
        p, d, a = f"{prefix}_perceive", f"{prefix}_decide", f"{prefix}_act"
        children = [
            mk(p, "? 感知与输入边界", "待定义子系统循环每轮接收的观测或状态", role="entry"),
            mk(d, "? 决策与推演核心", "待定义子系统循环内的决策与路由逻辑"),
            mk(a, "? 执行与状态更新", "待定义子系统循环内的动作执行与输出", role="exit"),
        ]
        inner_edges = [
            {"from": p, "to": d, "contract": "TBD"},
            {"from": d, "to": a, "contract": "TBD"},
            {"from": a, "to": p, "contract": "TBD (闭环反馈)", "kind": "feedback"},
        ]
        return children, inner_edges, [p], [a]

    # Default: pipeline
    ni, nc, no = f"{prefix}_in", f"{prefix}_core", f"{prefix}_out"
    children = [
        mk(ni, "? 子系统输入边界", "待定义子系统的入口适配与协议解包", role="entry"),
        mk(nc, "? 子系统核心处理", "待定义子系统的内部核心逻辑"),
        mk(no, "? 子系统输出边界", "待定义子系统的结果封装与出口交付", role="exit"),
    ]
    inner_edges = [
        {"from": ni, "to": nc, "contract": "TBD"},
        {"from": nc, "to": no, "contract": "TBD"},
    ]
    return children, inner_edges, [ni], [no]


def op_fold(
    node_ids: List[str],
    super_id: str,
    label: str,
    spec: str,
    stage: str = "",
    topology: str = "",
    scope: str = "",
) -> str:
    """Fold a settled, connected sub-graph into a single super node.

    Children and inner edges are stored inside the super node. Edges crossing
    the fold boundary are re-pointed at the super node; parallel edges to the
    same neighbour are merged (contracts joined with " | ") and keep the
    original edges as members, so arch_unfold can restore them exactly.
    """
    graph = _load_graph()
    cfg = _load_config()
    container, c_nodes, c_edges, depth, _ = _resolve_container(graph, scope)

    ids = list(dict.fromkeys(i.strip() for i in node_ids if i and i.strip()))
    if len(ids) < 2:
        raise ArchEngineError("折叠至少需要 2 个节点。")
    if any(n["id"] == super_id for n in _iter_all_nodes(graph["nodes"])):
        raise ArchEngineError(f"节点 '{super_id}' 已存在，请换一个超级节点 ID。")

    members = {nid: _get_node(container, nid) for nid in ids}
    unsettled = [f"{nid}({n['status']})" for nid, n in members.items() if n["status"] not in SOLID_STATUSES]
    if unsettled:
        raise ArchEngineError(
            f"只能折叠已确定的节点，以下节点尚未确定：{', '.join(unsettled)}。\n"
            "JIT 指令：折叠用于收起已稳定的子图；待定节点请先 collapse，迷雾节点不应纳入折叠。"
        )

    # Nesting depth guard from project preset.
    inner_depth = _max_subtree_depth(list(members.values()))
    new_depth = depth + 1 + inner_depth
    max_depth = int(cfg.get("max_depth", 2))
    if new_depth > max_depth:
        raise ArchEngineError(
            f"[MaxDepthError] 折叠后嵌套深度将达到 {new_depth}，超过项目 preset 上限 ({max_depth})。\n"
            f"JIT 指令：请先向用户确认是否需要拆分或精简层级；若确需更深嵌套，经用户确认后执行 "
            f"arch config --max-depth {new_depth} 再重试。"
        )

    member_set = set(ids)
    inner = [e for e in c_edges if e["from"] in member_set and e["to"] in member_set]
    incoming = [e for e in c_edges if e["to"] in member_set and e["from"] not in member_set]
    outgoing = [e for e in c_edges if e["from"] in member_set and e["to"] not in member_set]

    # Weak connectivity: the members must form one module.
    undirected: Dict[str, set] = {nid: set() for nid in ids}
    for e in inner:
        undirected[e["from"]].add(e["to"])
        undirected[e["to"]].add(e["from"])
    seen, stack = {ids[0]}, [ids[0]]
    while stack:
        for nxt in undirected[stack.pop()]:
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    if seen != member_set:
        raise ArchEngineError(
            f"待折叠节点之间并不连通（与 {ids[0]} 不相连：{', '.join(sorted(member_set - seen))}）。\n"
            "JIT 指令：只折叠彼此有连线的同一模块，不相关的部分请分别折叠。"
        )

    # Convexity (SCC-aware): forbid folding non-adjacent DAG layers that would
    # introduce a *new* cycle through an external node `curr`. If `curr` is
    # already in the same strongly connected component as the member set (e.g.
    # folding a contiguous arc of an existing ring), contracting it does not
    # invent a new cycle.
    scc_of = _sccs((n["id"] for n in c_nodes), c_edges, include_feedback=True)
    member_sccs = {scc_of[nid] for nid in ids}
    frontier = [e["to"] for e in outgoing if not _is_feedback(e)]
    reachable_outside = set(frontier)
    while frontier:
        curr = frontier.pop()
        for e in c_edges:
            if _is_feedback(e):
                continue
            if e["from"] == curr:
                if e["to"] in member_set:
                    if scc_of.get(curr) not in member_sccs:
                        raise ArchEngineError(
                            f"外部节点 '{curr}' 位于待折叠子图的中间（存在 子图 → {curr} → 子图 的路径），"
                            "折叠后会形成环。\nJIT 指令：把它一并纳入折叠，或缩小折叠范围。"
                        )
                elif e["to"] not in reachable_outside:
                    reachable_outside.add(e["to"])
                    frontier.append(e["to"])

    primary_inner = [e for e in inner if not _is_feedback(e)]
    entry_children = [nid for nid in ids if not any(e["to"] == nid for e in primary_inner)]
    if not entry_children:
        entry_children = list(dict.fromkeys(e["to"] for e in incoming)) or [
            nid for nid in ids if members[nid].get("role") in ("hub", "entry")
        ] or [ids[0]]
    exit_children = [nid for nid in ids if not any(e["from"] == nid for e in primary_inner)]
    if not exit_children:
        exit_children = list(dict.fromkeys(e["from"] for e in outgoing)) or [
            nid for nid in ids if members[nid].get("role") in ("hub", "exit")
        ] or [ids[-1]]

    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for e in incoming:
        grouped.setdefault((e["from"], super_id), []).extend(_edge_members(e))
    for e in outgoing:
        grouped.setdefault((super_id, e["to"]), []).extend(_edge_members(e))
    folded_edges = [_edge_from_members(u, v, ms) for (u, v), ms in grouped.items()]

    sub_topo = _validate_topology(topology) if topology else _infer_folded_topology(members, inner)
    child_stages = {members[nid].get("stage", "") for nid in ids}
    super_node: Dict[str, Any] = {
        "id": super_id,
        "label": label,
        "status": "super_node",
        "type": "super_node",
        "stage": stage or (child_stages.pop() if len(child_stages) == 1 else ""),
        "spec": spec,
        "file_path": "",
        "last_tag": "",
        "children": [members[nid] for nid in ids],
        "inner_edges": inner,
        "entry_children": entry_children,
        "exit_children": exit_children,
    }
    if sub_topo != "pipeline" or topology:
        super_node["topology"] = sub_topo

    # Put the super node where its first member was, so ordering stays stable.
    insert_at = min(i for i, n in enumerate(c_nodes) if n["id"] in member_set)
    remaining = [n for n in c_nodes if n["id"] not in member_set]
    new_nodes = remaining[:insert_at] + [super_node] + remaining[insert_at:]
    new_edges = [
        e for e in c_edges if e["from"] not in member_set and e["to"] not in member_set
    ] + folded_edges
    _replace_container_lists(container, new_nodes, new_edges)
    if "children" in container:
        parent_ports: Dict[str, List[str]] = {}
        for port_key in ("entry_children", "exit_children"):
            ports = container.get(port_key) or []
            matched = [cid for cid in ports if cid in member_set]
            if matched:
                parent_ports[port_key] = matched
            container[port_key] = list(dict.fromkeys(
                super_id if cid in member_set else cid for cid in ports
            ))
        if parent_ports:
            super_node["parent_ports"] = parent_ports

    graph["step"] += 1
    tag = f"fold-{super_id}-s{graph['step']:02d}"
    _save_graph(graph)
    _shadow_commit(tag, f"fold {', '.join(ids)} -> {super_id}")
    return (
        f"已将 {len(ids)} 个节点折叠为超级节点 '{super_id}' ({label} · topology={sub_topo})，快照 {tag}。\n"
        f"入口子节点：{', '.join(entry_children)}；出口子节点：{', '.join(exit_children)}。\n\n"
        + op_focus(scope=scope)
    )


def op_promote(
    node_id: str,
    topology: str = "",
    label: str = "",
    spec: str = "",
    scope: str = "",
) -> str:
    """Promote an existing node top-down into a sub-system super_node with its own topology."""
    graph = _load_graph()
    cfg = _load_config()
    container, c_nodes, c_edges, depth, _ = _resolve_container(graph, scope)
    node = _get_node(container, node_id, allow_super=False)
    if node["status"] == "fog":
        raise ArchEngineError(
            f"节点 '{node_id}' 处于储备区 (fog)，请先 activate 激活后再升级为子系统。"
        )

    max_depth = int(cfg.get("max_depth", 2))
    if depth + 1 > max_depth:
        raise ArchEngineError(
            f"[MaxDepthError] 升级为子系统后嵌套深度将达到 {depth + 1}，超过项目 preset 上限 ({max_depth})。\n"
            f"JIT 指令：若确需更深层级，请先与用户确认后运行 arch config --max-depth {depth + 1}。"
        )

    sub_topo = _validate_topology(topology or cfg.get("default_topology", "pipeline"))
    children, inner_edges, entry_children, exit_children = _starter_subsystem_skeleton(
        node_id, sub_topo, stage=node.get("stage", ""),
    )

    existing_ids = {n["id"] for n in _iter_all_nodes(graph["nodes"])}
    collisions = [c["id"] for c in children if c["id"] in existing_ids]
    if collisions:
        raise ArchEngineError(f"子系统初始子节点 ID 冲突：{', '.join(collisions)}。")

    clean_label = (label or node["label"]).lstrip("? ").strip() or node_id
    node["label"] = clean_label
    if spec:
        node["spec"] = spec
    node["status"] = "super_node"
    node["type"] = "super_node"
    node["topology"] = sub_topo
    node["promoted"] = True
    node["children"] = children
    node["inner_edges"] = inner_edges
    node["entry_children"] = entry_children
    node["exit_children"] = exit_children
    node["last_tag"] = ""

    graph["step"] += 1
    tag = f"promote-{node_id}-s{graph['step']:02d}"
    _save_graph(graph)
    _shadow_commit(tag, f"promote {node_id} -> {sub_topo} subsystem")
    return (
        f"已将节点 '{node_id}' 升级为 {sub_topo} 子系统超级节点（快照 {tag}），"
        f"内部生成 {len(children)} 个待定空洞，外层连线已保留为边界端口。\n"
        f"后续可用 --in {node_id} 在该子系统内部演进。\n\n"
        + op_focus(scope=node_id)
    )


def op_unfold(
    super_id: str, attach: Optional[Dict[str, str]] = None, scope: str = "",
) -> str:
    """Restore a super node's children, inner edges and boundary edges.

    Each edge on the super node is split back into the original edges it
    stands for (its members), regrouped by the child that now holds each
    endpoint. A contract edited while folded overrides the members' contracts.
    Edges attached after folding go to the unique entry / exit child, or to the
    child named in `attach` (neighbour -> child).
    """
    attach = attach or {}
    graph = _load_graph()
    container, c_nodes, c_edges, _, _ = _resolve_container(graph, scope)
    sn = _get_node(container, super_id)
    if sn["status"] != "super_node":
        raise ArchEngineError(f"节点 '{super_id}' 不是超级节点，无需展开。")

    child_ids = {c["id"] for c in sn["children"]}
    bad = {k: v for k, v in attach.items() if v not in child_ids}
    if bad:
        raise ArchEngineError(
            f"attach 指定的子节点不在 '{super_id}' 内：{bad}。可选子节点：{', '.join(sorted(child_ids))}。"
        )

    # Which child of this super node holds a given node id (at any depth).
    owner = {d["id"]: c["id"] for c in sn["children"] for d in _iter_all_nodes([c])}

    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    unresolved: List[str] = []
    for e in (e for e in c_edges if super_id in (e["from"], e["to"])):
        inbound = e["to"] == super_id
        neighbour = e["from"] if inbound else e["to"]
        inner_end = "to" if inbound else "from"
        for m in _edge_members(e):
            child = owner.get(m[inner_end])
            if child is None:
                # Attached while folded: route it to the right child and record
                # that child as the real endpoint for any later unfold.
                candidates = sn["entry_children"] if inbound else sn["exit_children"]
                child = attach.get(neighbour) or (candidates[0] if len(candidates) == 1 else "")
                if not child:
                    arrow = f"{neighbour} -> {super_id}" if inbound else f"{super_id} -> {neighbour}"
                    item = f"{arrow}（可选：{', '.join(candidates or sorted(child_ids))}）"
                    if item not in unresolved:
                        unresolved.append(item)
                    continue
                m = {**m, inner_end: child}
            key = (neighbour, child) if inbound else (child, neighbour)
            grouped.setdefault(key, []).append(m)

    restored: List[Dict[str, Any]] = [dict(e) for e in sn["inner_edges"]]
    restored += [_edge_from_members(u, v, ms) for (u, v), ms in grouped.items()]

    if unresolved:
        raise ArchEngineError(
            f"超级节点 '{super_id}' 折叠期间新增了无法自动归位的连线：\n  - "
            + "\n  - ".join(unresolved)
            + "\nJIT 指令：向用户确认每条连线应接到哪个子节点，然后带上 attach={外部节点ID: 子节点ID} 重新调用 arch_unfold。"
        )

    idx = next(i for i, n in enumerate(c_nodes) if n["id"] == super_id)
    new_nodes = c_nodes[:idx] + sn["children"] + c_nodes[idx + 1:]
    new_edges = [e for e in c_edges if super_id not in (e["from"], e["to"])] + restored
    _replace_container_lists(container, new_nodes, new_edges)
    if "children" in container:
        saved_ports = sn.get("parent_ports") or {}
        for port_key in ("entry_children", "exit_children"):
            ports = container.get(port_key) or []
            expanded: List[str] = []
            for cid in ports:
                if cid == super_id:
                    expanded.extend(
                        saved_ports.get(port_key)
                        or sn.get(port_key)
                        or [c["id"] for c in sn["children"]]
                    )
                else:
                    expanded.append(cid)
            container[port_key] = list(dict.fromkeys(expanded))

    graph["step"] += 1
    tag = f"unfold-{super_id}-s{graph['step']:02d}"
    _save_graph(graph)
    _shadow_commit(tag, f"unfold {super_id}")
    return (
        f"已展开超级节点 '{super_id}'，恢复 {len(sn['children'])} 个子节点，快照 {tag}。\n\n"
        + op_focus(scope=scope)
    )
