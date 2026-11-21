"""Fold a settled sub-graph into a super node, and unfold it exactly."""

from typing import Any, Dict, List, Optional, Tuple

from .common import ArchEngineError, _load_graph, _save_graph, _shadow_commit
from .graph import SOLID_STATUSES, _edge_from_members, _edge_members, _get_node, _iter_all_nodes
from .views import op_focus


def op_fold(
    node_ids: List[str],
    super_id: str,
    label: str,
    spec: str,
    stage: str = "",
) -> str:
    """Fold a settled, connected, convex sub-graph into a single super node.

    Children and inner edges are stored inside the super node. Edges crossing
    the fold boundary are re-pointed at the super node; parallel edges to the
    same neighbour are merged (contracts joined with " | ") and keep the
    original edges as members, so arch_unfold can restore them exactly.
    """
    graph = _load_graph()
    ids = list(dict.fromkeys(i.strip() for i in node_ids if i and i.strip()))
    if len(ids) < 2:
        raise ArchEngineError("折叠至少需要 2 个节点。")
    if any(n["id"] == super_id for n in _iter_all_nodes(graph["nodes"])):
        raise ArchEngineError(f"节点 '{super_id}' 已存在，请换一个超级节点 ID。")

    members = {nid: _get_node(graph, nid) for nid in ids}
    unsettled = [f"{nid}({n['status']})" for nid, n in members.items() if n["status"] not in SOLID_STATUSES]
    if unsettled:
        raise ArchEngineError(
            f"只能折叠已确定的节点，以下节点尚未确定：{', '.join(unsettled)}。\n"
            "JIT 指令：折叠用于收起已稳定的子图；待定节点请先 collapse，迷雾节点不应纳入折叠。"
        )

    member_set = set(ids)
    inner = [e for e in graph["edges"] if e["from"] in member_set and e["to"] in member_set]
    incoming = [e for e in graph["edges"] if e["to"] in member_set and e["from"] not in member_set]
    outgoing = [e for e in graph["edges"] if e["from"] in member_set and e["to"] not in member_set]

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

    # Convexity: no path may leave the set and come back in, or folding would
    # create a cycle through the super node.
    frontier = [e["to"] for e in outgoing]
    reachable_outside = set(frontier)
    while frontier:
        curr = frontier.pop()
        for e in graph["edges"]:
            if e["from"] == curr:
                if e["to"] in member_set:
                    raise ArchEngineError(
                        f"外部节点 '{curr}' 位于待折叠子图的中间（存在 子图 → {curr} → 子图 的路径），"
                        "折叠后会形成环。\nJIT 指令：把它一并纳入折叠，或缩小折叠范围。"
                    )
                if e["to"] not in reachable_outside:
                    reachable_outside.add(e["to"])
                    frontier.append(e["to"])

    entry_children = [nid for nid in ids if not any(e["to"] == nid for e in inner)]
    exit_children = [nid for nid in ids if not any(e["from"] == nid for e in inner)]

    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for e in incoming:
        grouped.setdefault((e["from"], super_id), []).extend(_edge_members(e))
    for e in outgoing:
        grouped.setdefault((super_id, e["to"]), []).extend(_edge_members(e))
    folded_edges = [_edge_from_members(u, v, ms) for (u, v), ms in grouped.items()]

    child_stages = {members[nid].get("stage", "") for nid in ids}
    super_node = {
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

    # Put the super node where its first member was, so ordering stays stable.
    insert_at = min(i for i, n in enumerate(graph["nodes"]) if n["id"] in member_set)
    remaining = [n for n in graph["nodes"] if n["id"] not in member_set]
    graph["nodes"] = remaining[:insert_at] + [super_node] + remaining[insert_at:]
    graph["edges"] = [
        e for e in graph["edges"] if e["from"] not in member_set and e["to"] not in member_set
    ] + folded_edges

    graph["step"] += 1
    tag = f"fold-{super_id}-s{graph['step']:02d}"
    _save_graph(graph)
    _shadow_commit(tag, f"fold {', '.join(ids)} -> {super_id}")
    return (
        f"已将 {len(ids)} 个节点折叠为超级节点 '{super_id}' ({label})，快照 {tag}。\n"
        f"入口子节点：{', '.join(entry_children)}；出口子节点：{', '.join(exit_children)}。\n\n"
        + op_focus()
    )


def op_unfold(super_id: str, attach: Optional[Dict[str, str]] = None) -> str:
    """Restore a super node's children, inner edges and boundary edges.

    Each edge on the super node is split back into the original edges it
    stands for (its members), regrouped by the child that now holds each
    endpoint. A contract edited while folded overrides the members' contracts.
    Edges attached after folding go to the unique entry / exit child, or to the
    child named in `attach` (neighbour -> child).
    """
    attach = attach or {}
    graph = _load_graph()
    sn = _get_node(graph, super_id)
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
    for e in (e for e in graph["edges"] if super_id in (e["from"], e["to"])):
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

    idx = next(i for i, n in enumerate(graph["nodes"]) if n["id"] == super_id)
    graph["nodes"] = graph["nodes"][:idx] + sn["children"] + graph["nodes"][idx + 1:]
    graph["edges"] = [e for e in graph["edges"] if super_id not in (e["from"], e["to"])] + restored

    graph["step"] += 1
    tag = f"unfold-{super_id}-s{graph['step']:02d}"
    _save_graph(graph)
    _shadow_commit(tag, f"unfold {super_id}")
    return (
        f"已展开超级节点 '{super_id}'，恢复 {len(sn['children'])} 个子节点，快照 {tag}。\n\n"
        + op_focus()
    )
