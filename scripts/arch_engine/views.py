"""Read-only views: the focus slice, export-task cards, facts and status."""

import json
from collections import Counter
from typing import Any, Dict, List, Optional

from .common import (
    ArchEngineError, GRAVEYARD_FILE, GROUND_TRUTH_FILE, SEED_FILE, _load_config, _load_graph,
)
from .graph import (
    IMPL_LEVELS, IMPL_TEXT, SOLID_STATUSES, VERIFY_LEVELS, VERIFY_TEXT, _check_node_drift,
    _container_edges, _container_nodes, _container_topology, _container_warnings, _edge_kind,
    _find_any_node, _find_walking_skeleton, _get_node, _is_super_ready, _iter_all_nodes,
    _max_subtree_depth, _next_focus_hole, _node_state, _node_view, _parent_container_of,
    _public_edge, _resolve_container, _skeleton_banner, _state_text,
)
from .records import (
    NOT_MIGRATED_HINT, _active_facts, _budget_warning, _context_keys, _fact_line,
    _global_fact_budget, _grave_line, _is_global, _load_records, _matches, _open_line,
    _pending_md_edits, _read_records, _records_enabled, _scope_text, _superseded_ids, legacy_hint,
)
from .refs import _resolve_ref


def _boundary_edges(graph: Dict[str, Any], scope: str) -> Dict[str, List[Dict[str, Any]]]:
    """External incoming/outgoing edges attached to a super_node from its parent container."""
    if not scope:
        return {"in": [], "out": []}
    parent = _parent_container_of(graph, scope)
    if parent is None:
        return {"in": [], "out": []}
    p_edges = _container_edges(parent)
    b_in = [_public_edge(e) for e in p_edges if e["to"] == scope or (_edge_kind(e) == "bidirectional" and e["from"] == scope)]
    b_out = [_public_edge(e) for e in p_edges if e["from"] == scope or (_edge_kind(e) == "bidirectional" and e["to"] == scope)]
    return {"in": b_in, "out": b_out}


def op_focus(node_id: Optional[str] = None, scope: str = "") -> str:
    graph = _load_graph()
    cfg = _load_config()
    container, c_nodes, c_edges, depth, path = _resolve_container(graph, scope)
    nodes = {n["id"]: n for n in c_nodes}

    holes = [n for n in c_nodes if n["status"] == "hole"]
    settled = [n for n in c_nodes if n["status"] in SOLID_STATUSES]

    target = None
    if node_id:
        target = _get_node(container, node_id)
    elif holes:
        target = nodes[_next_focus_hole(container)]
    elif settled:
        target = settled[-1]

    seed_txt = SEED_FILE.read_text(encoding="utf-8").strip() if SEED_FILE.exists() else ""
    rec = _read_records() if _records_enabled() else None

    if rec is None:
        gt_txt = GROUND_TRUTH_FILE.read_text(encoding="utf-8").strip() if GROUND_TRUTH_FILE.exists() else ""
        gy_txt = GRAVEYARD_FILE.read_text(encoding="utf-8").strip() if GRAVEYARD_FILE.exists() else ""
        out: List[str] = [
            "=== 北极星锚点与硬约束 ===",
            seed_txt,
            gt_txt,
            gy_txt,
            "========================",
            NOT_MIGRATED_HINT,
        ]
    else:
        global_facts = [f for f in _active_facts(rec) if _is_global(f)]
        out = ["=== 北极星锚点与硬约束 ===", seed_txt, "--- 全局事实 (global) ---"]
        out += [_fact_line(f, show_scope=False) for f in global_facts] or ["（无）"]
        out.append("========================")
        warning = _budget_warning(rec)
        if warning:
            out.append(warning)
        if _pending_md_edits(rec):
            out.append(
                "[RECORDS_MD_EDITED] 生成的 md 里有 records.json 之外的内容，"
                "运行 arch migrate-records 导入，否则下次写入记录时会被拒绝。"
            )

    topo = _container_topology(container)
    if scope or topo != "pipeline":
        crumb = " › ".join(["root", *path])
        out.append(
            f"=== 当前作用域: {crumb} [topology: {topo} · depth {depth}/{cfg['max_depth']} · preset: {cfg['preset']}] ==="
        )

    out.extend(_container_warnings(container, cfg))

    skeleton = _find_walking_skeleton(graph, scope=scope)
    if skeleton:
        out.append(_skeleton_banner(container, skeleton))

    if not target:
        out.append("当前图为空。")
        return "\n".join(out)

    tid = target["id"]
    in_edges = [
        e for e in c_edges
        if e["to"] == tid or (_edge_kind(e) == "bidirectional" and e["from"] == tid)
    ]
    out_edges = [
        e for e in c_edges
        if e["from"] == tid or (_edge_kind(e) == "bidirectional" and e["to"] == tid)
    ]
    neighbor_ids = (
        {e["from"] for e in in_edges}
        | {e["to"] for e in in_edges}
        | {e["from"] for e in out_edges}
        | {e["to"] for e in out_edges}
    ) - {tid}
    fog_nodes = [n for n in c_nodes if n["status"] == "fog"]

    slice_payload: Dict[str, Any] = {
        "current_step": graph["step"],
        "focus_node": _node_view(target),
        "upstream_edges": [_public_edge(e) for e in in_edges],
        "downstream_edges": [_public_edge(e) for e in out_edges],
        "neighbors": [_node_view(nodes[nid]) for nid in neighbor_ids if nid in nodes],
        "fog_backlog": [{"id": f["id"], "label": f["label"]} for f in fog_nodes],
    }
    if topo != "pipeline" or scope:
        slice_payload["topology"] = topo
    if scope:
        boundary = _boundary_edges(graph, scope)
        slice_payload["scope"] = scope
        slice_payload["scope_path"] = path
        slice_payload["boundary_in"] = boundary["in"]
        slice_payload["boundary_out"] = boundary["out"]

    out.append("=== 当前焦点 1-Hop 切片 ===")
    out.append(json.dumps(slice_payload, indent=2, ensure_ascii=False))
    if rec is not None:
        keys = _context_keys(graph, tid, neighbor_ids)
        if scope:
            keys.add(scope)
        out.append(f"=== 相关记录（作用域：{', '.join(sorted(keys))}）===")
        out += _related_records(rec, keys)
    return "\n".join(out)


def _related_records(rec: Dict[str, Any], keys: set, global_open: str = "blocking") -> List[str]:
    """Facts, graveyard entries and open items scoped to `keys`, plus a hidden-count line.

    Global facts are shown separately by the callers. Global open items are
    included when `global_open` is "blocking" (only those that block work).
    """
    active = _active_facts(rec)
    facts = [f for f in active if not _is_global(f) and _matches(f, keys)]
    graves = [g for g in rec["graveyard"] if g["node"] in keys]
    open_all = [o for o in rec["open_items"] if o.get("status") != "resolved"]
    opens = [o for o in open_all if _matches(o, keys)
             or (global_open == "blocking" and _is_global(o) and o.get("blocking"))]

    out: List[str] = []
    if facts:
        out.append("事实：")
        out += [_fact_line(f) for f in facts]
    if graves:
        out.append("墓志（已废弃方案）：")
        out += [_grave_line(g) for g in graves]
    if opens:
        out.append("待办项：")
        out += [_open_line(o) for o in sorted(opens, key=lambda o: not o.get("blocking"))]
    if not out:
        out.append("（没有与当前节点相关的事实、墓志或待办项）")

    hidden = [
        (sum(1 for f in active if not _is_global(f)) - len(facts), "条节点/阶段事实", "arch facts --all"),
        (len(rec["graveyard"]) - len(graves), "条墓志", "arch facts --all"),
        (len(open_all) - len(opens), "条待办项", "arch open list"),
    ]
    hidden = [h for h in hidden if h[0] > 0]
    if hidden:
        out.append(
            "另有 " + "、".join(f"{n} {what}" for n, what, _ in hidden) + " 未展开（"
            + " / ".join(dict.fromkeys(cmd for _, _, cmd in hidden)) + " 查看）。"
        )
    legacy_open = sum(1 for lg in rec["legacy"] if lg.get("status") != "resolved")
    if legacy_open:
        out.append(legacy_hint(legacy_open))
    return out


def op_export_task(node_id: str, scope: str = "") -> str:
    graph = _load_graph()
    container, c_nodes, c_edges, _, _ = _resolve_container(graph, scope)
    nodes = {n["id"]: n for n in c_nodes}
    _get_node(container, node_id, allow_super=False)

    node = nodes[node_id]
    if node["status"] == "hole":
        raise ArchEngineError(f"节点 '{node_id}' 仍处于 '?' 待定状态，请先通过 collapse 确定节点规范后再导出编码任务。")

    _check_node_drift(node)

    in_edges = [
        e for e in c_edges
        if e["to"] == node_id or (_edge_kind(e) == "bidirectional" and e["from"] == node_id)
    ]
    out_edges = [
        e for e in c_edges
        if e["from"] == node_id or (_edge_kind(e) == "bidirectional" and e["to"] == node_id)
    ]

    task: Dict[str, Any] = {
        "worker_instruction": (
            "你是独立的实现子代理。严格仅实现以下单节点契约："
            "1) 采用 Port/Adapter 分离易变外部接口；"
            "2) 按主代理给定的变更范围和项目规则验证上下游契约；"
            "3) 若 type=='reality_gate'，按项目要求验证真实环境约束；"
            "4) 按主代理给定的快照安排处理 commit-node，并返回 <=3 行结果摘要。"
        ),
        "target_node": _node_view(node),
        "input_contracts": [_public_edge(e) for e in in_edges],
        "output_contracts": [_public_edge(e) for e in out_edges],
    }
    if scope:
        task["subsystem_scope"] = scope
        task["subsystem_topology"] = _container_topology(container)
        boundary = _boundary_edges(graph, scope)
        if node_id in (container.get("entry_children") or []) and boundary["in"]:
            task["boundary_in_contracts"] = boundary["in"]
        if node_id in (container.get("exit_children") or []) and boundary["out"]:
            task["boundary_out_contracts"] = boundary["out"]

    if _records_enabled():
        rec = _read_records()
        keys = _context_keys(graph, node_id)
        if scope:
            keys.add(scope)
        active = _active_facts(rec)
        task["global_facts"] = [_fact_line(f, show_scope=False) for f in active if _is_global(f)]
        task["node_facts"] = [_fact_line(f) for f in active if not _is_global(f) and _matches(f, keys)]
        task["graveyard"] = [_grave_line(g) for g in rec["graveyard"] if g["node"] == node_id]
        task["open_items"] = [
            _open_line(o) for o in rec["open_items"]
            if o.get("status") != "resolved"
            and (_matches(o, keys) or (_is_global(o) and o.get("blocking")))
        ]
    else:
        task["global_ground_truth"] = (
            GROUND_TRUTH_FILE.read_text(encoding="utf-8").strip() if GROUND_TRUTH_FILE.exists() else ""
        )
        task["global_graveyard"] = (
            GRAVEYARD_FILE.read_text(encoding="utf-8").strip() if GRAVEYARD_FILE.exists() else ""
        )
        task["records_hint"] = NOT_MIGRATED_HINT
    refs = _node_state(node)["refs"]
    if refs:
        task["ref_sections"] = [_resolve_ref(r) for r in refs]
    return json.dumps(task, indent=2, ensure_ascii=False)


def op_facts(node_id: str = "", all_scopes: bool = False, with_superseded: bool = False) -> str:
    """Facts for one node (its scope, stage and folded children), all facts, or global ones."""
    rec = _load_records()
    gone = _superseded_ids(rec)
    facts = rec["facts"] if with_superseded else _active_facts(rec)
    line = lambda f, **kw: _fact_line(f, superseded_by=gone.get(f["id"], ""), **kw)

    if node_id:
        graph = _load_graph()
        if _find_any_node(graph, node_id) is None:
            raise ArchEngineError(f"节点 '{node_id}' 不存在。")
        keys = _context_keys(graph, node_id)
        picked = [f for f in facts if not _is_global(f) and _matches(f, keys)]
        graves = [g for g in rec["graveyard"] if g["node"] in keys]
        out = [f"节点 {node_id} 的事实（作用域：{', '.join(sorted(keys))}）："]
        out += [line(f) for f in picked] or ["（无）"]
        if graves:
            out.append("墓志：")
            out += [_grave_line(g) for g in graves]
        n_global = sum(1 for f in facts if _is_global(f))
        out.append(f"另有 {n_global} 条 global 事实对所有节点生效（arch facts 查看）。")
        return "\n".join(out)

    if all_scopes:
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for f in sorted(facts, key=lambda f: not _is_global(f)):
            groups.setdefault(_scope_text(f.get("scope")), []).append(f)
        out = []
        for scope, items in groups.items():
            out.append(f"--- 作用域 {scope}（{len(items)} 条）---")
            out += [line(f, show_scope=False) for f in items]
        if rec["graveyard"]:
            out.append(f"--- 墓志（{len(rec['graveyard'])} 条）---")
            out += [_grave_line(g) for g in rec["graveyard"]]
        if not out:
            return "暂无事实记录。"
        if not with_superseded and gone:
            out.append(f"另有 {len(gone)} 条已被取代的事实未展示（加 --with-superseded 查看）。")
        return "\n".join(out)

    picked = [f for f in facts if _is_global(f)]
    used, limit = _global_fact_budget(rec)
    out = [f"global 事实 {len(picked)} 条，共 {used} 字符（上限 {limit}）："]
    out += [line(f, show_scope=False) for f in picked] or ["（无）"]
    warning = _budget_warning(rec)
    if warning:
        out.append(warning)
    return "\n".join(out)


def op_status() -> str:
    """Whole-project progress: preset/topology, node states, open items, record counts, migration state."""
    graph = _load_graph()
    cfg = _load_config()
    all_nodes = list(_iter_all_nodes(graph["nodes"]))
    leaves = [n for n in all_nodes if n["status"] != "super_node"]
    count = Counter(n["status"] for n in all_nodes)
    solid = [n for n in leaves if n["status"] == "solid"]
    impl = Counter(_node_state(n)["impl"] for n in solid)
    verify = Counter(_node_state(n)["verify"]["level"] for n in solid)
    depth = _max_subtree_depth(graph["nodes"])

    out = [f"=== 项目状态 · step {graph.get('step', 0)} ==="]
    out.append(
        f"拓扑与预设：topology={_container_topology(graph)} · preset={cfg['preset']} · "
        f"嵌套深度 {depth}/{cfg['max_depth']} · 链长上限 {cfg['max_chain_len']}"
    )
    out.append(
        f"节点：已确定 {count['solid']} · 待定 {count['hole']} · 储备 {count['fog']} · 折叠模块 {count['super_node']}"
    )
    out.append(
        "实现进度（已确定节点，含折叠子节点）："
        + " · ".join(f"{IMPL_TEXT[k]} {impl[k]}" for k in reversed(IMPL_LEVELS))
    )
    out.append("验证程度：" + " · ".join(f"{VERIFY_TEXT[k]} {verify[k]}" for k in reversed(VERIFY_LEVELS)))

    out.extend(_container_warnings(graph, cfg))

    out.append("--- 节点明细 ---")

    def describe(n: Dict[str, Any], lv: int) -> None:
        pad = "  " * lv + ("└ " if lv else "")
        if n["status"] == "super_node":
            inner = [c for c in _iter_all_nodes(n["children"]) if c["status"] != "super_node"]
            built = sum(1 for c in inner if _node_state(c)["impl"] == "built")
            topo = _container_topology(n)
            ready_txt = "就绪" if _is_super_ready(n) else "未贯通"
            out.append(f"{pad}{n['id']} [模块·{topo}·{ready_txt}] {built}/{len(inner)} 已实现")
            for c in n["children"]:
                describe(c, lv + 1)
            return
        st = _node_state(n)
        extra = []
        if n.get("role"):
            extra.append(f"role:{n['role']}")
        if st["files"]:
            extra.append(f"文件 {len(st['files'])}")
        if st["refs"]:
            extra.append(f"引用 {len(st['refs'])}")
        state = _state_text(n) if n["status"] == "solid" else {"hole": "待定", "fog": "储备"}.get(n["status"], n["status"])
        out.append(f"{pad}{n['id']} [{n['status']}] {state}" + (f" · {' · '.join(extra)}" if extra else ""))

    for n in graph["nodes"]:
        describe(n, 0)

    out.append("--- 记录 ---")
    if not _records_enabled():
        out.append(NOT_MIGRATED_HINT)
        return "\n".join(out)
    rec = _read_records()
    active = _active_facts(rec)
    used, limit = _global_fact_budget(rec)
    n_global = sum(1 for f in active if _is_global(f))
    out.append(
        f"事实：生效 {len(active)} 条（global {n_global} 条 / {used} 字符，上限 {limit}；"
        f"节点/阶段 {len(active) - n_global} 条），已被取代 {len(rec['facts']) - len(active)} 条"
    )
    out.append(f"墓志：{len(rec['graveyard'])} 条")
    opens = [o for o in rec["open_items"] if o.get("status") != "resolved"]
    blocking = [o for o in opens if o.get("blocking")]
    owners = Counter(o.get("owner") for o in opens)
    out.append(
        f"待办项：未关闭 {len(opens)} 条（卡开发 {len(blocking)}；用户 {owners['user']} / 代理 {owners['agent']}），"
        f"已解决 {len(rec['open_items']) - len(opens)} 条"
    )
    out += [_open_line(o) for o in blocking]
    legacy = [lg for lg in rec["legacy"] if lg.get("status") != "resolved"]
    if legacy:
        ids_text = ", ".join(lg["id"] for lg in legacy[:10]) + ("…" if len(legacy) > 10 else "")
        out.append(legacy_hint(len(legacy)) + f"（{ids_text}）")
    warning = _budget_warning(rec)
    if warning:
        out.append(warning)
    ids = {n["id"] for n in all_nodes}
    stages = {n.get("stage") for n in all_nodes if n.get("stage")}
    orphans = [
        f"{r['id']}→{s}"
        for r in active + opens
        for s in (r.get("scope") or [])
        if s != "global" and s not in ids and s not in stages
    ] + [f"{g['id']}→{g['node']}" for g in rec["graveyard"] if g["node"] not in ids]
    if orphans:
        out.append(f"[ORPHAN_SCOPES] 以下记录指向图中已不存在的节点或阶段：{', '.join(orphans)}（可用 rescope 改正）")
    if _pending_md_edits(rec):
        out.append("[RECORDS_MD_EDITED] 生成的 md 被手改过，运行 arch migrate-records 导入。")
    return "\n".join(out)
