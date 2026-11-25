"""Fold a settled sub-graph into a super node, unfold it exactly, or promote a
node top-down into an isolated sub-system with its own topology."""

from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from .common import ArchEngineError
from .graph import (
    SOLID_STATUSES, _container_topology, _edge_kind, _is_feedback, _max_subtree_depth,
    _node_state, _sccs, _validate_topology,
)
from .kernel import GraphTx
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
    tx = GraphTx()
    cfg = tx.cfg
    c = tx.container(scope)
    container, c_nodes, c_edges, depth = c.raw, c.nodes, c.edges, c.depth

    ids = list(dict.fromkeys(i.strip() for i in node_ids if i and i.strip()))
    if len(ids) < 2:
        raise ArchEngineError("折叠至少需要 2 个节点。")
    if super_id in tx.all_ids():
        raise ArchEngineError(f"节点 '{super_id}' 已存在，请换一个超级节点 ID。")

    members = {nid: c.get(nid) for nid in ids}
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
    inner, incoming, outgoing = c.partition(member_set)

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
    # invent a new cycle. In a pipeline, feedback edges are not part of the
    # primary flow, so they must not count towards that exemption: one
    # end-to-start feedback edge would otherwise put every node in one SCC and
    # let a fold of non-adjacent layers create a primary cycle.
    scc_of = _sccs(
        (n["id"] for n in c_nodes), c_edges,
        include_feedback=_container_topology(container) != "pipeline",
    )
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
        "entry_children": entry_children,
        "exit_children": exit_children,
    }
    if sub_topo != "pipeline" or topology:
        super_node["topology"] = sub_topo
    c.contract_into(super_node, ids)

    tag = f"fold-{super_id}-s{tx.bump():02d}"
    tx.commit(tag, f"fold {', '.join(ids)} -> {super_id}")
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
    tx = GraphTx()
    cfg = tx.cfg
    c = tx.container(scope)
    depth = c.depth
    node = c.get(node_id, allow_super=False)
    if node["status"] == "fog":
        raise ArchEngineError(
            f"节点 '{node_id}' 处于储备区 (fog)，请先 activate 激活后再升级为子系统。"
        )
    # A super_node hides its own impl / files from status, export-task and drift
    # checks, so promoting an implemented node would silently orphan them.
    state = _node_state(node)
    if state["impl"] != "planned":
        bound = f"，已绑定文件：{', '.join(state['files'])}" if state["files"] else ""
        raise ArchEngineError(
            f"节点 '{node_id}' 已有实现（impl={state['impl']}{bound}），升级为子系统后这些实现记录将无处挂载。\n"
            f"JIT 指令：先向用户确认拆分方案；若确认重新拆解，请先 arch reset-node --node {node_id} "
            "--reason \"拆分为子系统\" 记录原实现，再 promote（已绑定文件会转到子系统的核心子节点上），"
            "之后在子系统内用 commit-node 重新绑定到对应子节点。"
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

    c.promote(node_id, children, inner_edges, entry_children, exit_children)

    clean_label = (label or node["label"]).lstrip("? ").strip() or node_id
    node["label"] = clean_label
    if spec:
        node["spec"] = spec
    node["topology"] = sub_topo
    node["promoted"] = True
    node["last_tag"] = ""
    # Files still bound to a planned node (e.g. kept by reset-node) move to the
    # core starter child, where status / export-task / drift checks see them.
    moved_files = state["files"]
    if moved_files:
        core_suffix = {"star": "_hub", "ring": "_decide"}.get(sub_topo, "_core")
        core = next(ch for ch in children if ch["id"] == f"{node_id}{core_suffix}")
        core["file_path"] = node["file_path"]
    for key in ("file_path", "impl", "verify"):
        node.pop(key, None)

    tag = f"promote-{node_id}-s{tx.bump():02d}"
    tx.commit(tag, f"promote {node_id} -> {sub_topo} subsystem")
    return (
        f"已将节点 '{node_id}' 升级为 {sub_topo} 子系统超级节点（快照 {tag}），"
        f"内部生成 {len(children)} 个待定空洞，外层连线已保留为边界端口。\n"
        + (f"原绑定文件已转到子节点 '{core['id']}'：{', '.join(moved_files)}。\n" if moved_files else "")
        + f"后续可用 --in {node_id} 在该子系统内部演进。\n\n"
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
    child named in `attach` (neighbour -> child). Outer subsystems' provenance
    that names this super node is re-pointed at its children the same way
    (see `Container.expand`).
    """
    tx = GraphTx()
    sn = tx.container(scope).expand(super_id, attach)

    tag = f"unfold-{super_id}-s{tx.bump():02d}"
    tx.commit(tag, f"unfold {super_id}")
    return (
        f"已展开超级节点 '{super_id}'，恢复 {len(sn['children'])} 个子节点，快照 {tag}。\n\n"
        + op_focus(scope=scope)
    )
