"""Operations that change nodes and edges (everything except fold / unfold)."""

import json
import re
from datetime import datetime
from typing import List, Optional, Union

from .common import (
    ARCH_DIR, ArchEngineError, GRAVEYARD_FILE, GROUNDING_FILE, GROUND_TRUTH_FILE,
    MAX_ACTIVE_HOLES, OPEN_ITEMS_FILE, RECORDS_FILE, SEED_FILE, _init_shadow_git, _load_graph,
    _run_shadow_git, _save_graph, _shadow_commit, _today,
)
from .graph import (
    IMPL_LEVELS, VERIFY_LEVELS, VERIFY_TEXT, _edge_from_members, _edge_members, _find_any_node,
    _get_node, _iter_all_nodes, _node_files, _node_state, _reject_manual_super_type,
    _state_text, _store_files,
)
from .records import (
    GT_TITLE, GY_TITLE, _load_records, _md_is_boilerplate, _new_records, _next_id,
    _records_enabled, _save_records,
)
from .views import op_focus


def op_init(seed: str) -> str:
    ARCH_DIR.mkdir(parents=True, exist_ok=True)
    _init_shadow_git()

    SEED_FILE.write_text(f"# 项目初始意图 (SEED)\n\n{seed.strip()}\n", encoding="utf-8")
    old_md = [p for p in (GROUND_TRUTH_FILE, GRAVEYARD_FILE)
              if p.exists() and not _md_is_boilerplate(p.read_text(encoding="utf-8"))]
    if not _records_enabled() and not old_md:
        # New projects start with typed records. A re-init over old-format md
        # leaves them alone; migrate-records converts them explicitly.
        _save_records(_new_records())
    if not GROUND_TRUTH_FILE.exists():
        GROUND_TRUTH_FILE.write_text(f"{GT_TITLE}\n\n", encoding="utf-8")
    if not GRAVEYARD_FILE.exists():
        GRAVEYARD_FILE.write_text(f"{GY_TITLE}\n\n", encoding="utf-8")
    if not GROUNDING_FILE.exists():
        GROUNDING_FILE.write_text(json.dumps({"sources": []}, indent=2) + "\n", encoding="utf-8")

    graph = {
        "step": 1,
        "nodes": [
            {
                "id": "entry_input",
                "label": "? 触发与输入边界",
                "status": "hole",
                "type": "port",
                "spec": "待定义系统如何接收初始触发或输入数据",
                "file_path": "",
                "last_tag": "",
            },
            {
                "id": "core_transform",
                "label": "? 核心处理黑盒",
                "status": "hole",
                "type": "port",
                "spec": "待定义核心业务变换逻辑",
                "file_path": "",
                "last_tag": "",
            },
            {
                "id": "final_output",
                "label": "? 最终输出与动作",
                "status": "hole",
                "type": "port",
                "spec": "待定义系统的最终交付形态或副作用",
                "file_path": "",
                "last_tag": "",
            },
        ],
        "edges": [
            {"from": "entry_input", "to": "core_transform", "contract": "TBD"},
            {"from": "core_transform", "to": "final_output", "contract": "TBD"},
        ],
    }
    _save_graph(graph)
    _shadow_commit("arch-step-01", f"init: {seed[:40]}")
    return op_focus()


def op_collapse(
    node_id: str,
    label: str,
    spec: str,
    file_path: str = "",
    in_contract: str = "",
    out_contract: str = "",
    node_type: str = "port",
    stage: str = "",
) -> str:
    _reject_manual_super_type(node_type)
    graph = _load_graph()
    nodes = {n["id"]: n for n in graph["nodes"]}
    _get_node(graph, node_id, allow_super=False)

    node = nodes[node_id]
    node["label"] = label
    node["spec"] = spec
    node["status"] = "solid"
    node["type"] = node_type
    if stage:
        node["stage"] = stage
    if file_path:
        node["file_path"] = file_path

    if in_contract:
        for e in graph["edges"]:
            if e["to"] == node_id:
                e["contract"] = in_contract
    if out_contract:
        for e in graph["edges"]:
            if e["from"] == node_id:
                e["contract"] = out_contract

    graph["step"] += 1
    tag = f"arch-step-{graph['step']:02d}"
    _save_graph(graph)
    _shadow_commit(tag, f"collapse {node_id}: {label}")
    return f"已锁定节点 '{node_id}' ({label})，快照 {tag}。\n\n" + op_focus()


def op_set_edge_contract(from_node: str, to_node: str, contract: str) -> str:
    graph = _load_graph()
    matches = [
        edge for edge in graph["edges"]
        if edge["from"] == from_node and edge["to"] == to_node
    ]
    edge_label = f"{from_node} -> {to_node}"
    if len(matches) != 1:
        raise ArchEngineError(f"需要唯一的连线 '{edge_label}'，找到 {len(matches)} 条。")

    edge = matches[0]
    if edge.get("contract") == contract:
        return f"连线 '{edge_label}' 已使用该契约。"

    edge["contract"] = contract
    graph["step"] += 1
    tag = f"arch-step-{graph['step']:02d}"
    _save_graph(graph)
    _shadow_commit(tag, f"set-edge-contract {edge_label}")
    return f"已更新连线 '{edge_label}' 的契约，快照 {tag}。\n\n" + op_focus(from_node)


def op_add_node(
    node_id: str,
    label: str,
    spec: str,
    status: str = "hole",
    node_type: str = "port",
    stage: str = "",
    after_node: str = "",
    before_node: str = "",
    contract: str = "TBD",
) -> str:
    _reject_manual_super_type(node_type)
    graph = _load_graph()
    if any(n["id"] == node_id for n in _iter_all_nodes(graph["nodes"])):
        raise ArchEngineError(f"节点 '{node_id}' 已存在。")
    for ref in (after_node, before_node):
        if ref:
            _get_node(graph, ref)

    holes = [n for n in graph["nodes"] if n["status"] == "hole"]
    if status == "hole" and len(holes) >= MAX_ACTIVE_HOLES:
        raise ArchEngineError(
            f"[CircuitBreakerError] 未决空洞已达上限 ({MAX_ACTIVE_HOLES})！"
            "JIT 指令：禁止继续新增 '?' 空洞，必须先通过 collapse 收敛现有空洞，或将非核心想法设为 status='fog'。"
        )

    graph["nodes"].append({
        "id": node_id,
        "label": label,
        "status": status,
        "type": node_type,
        "stage": stage,
        "spec": spec,
        "file_path": "",
        "last_tag": "",
    })

    if after_node and before_node:
        graph["edges"] = [
            e for e in graph["edges"]
            if not (e["from"] == after_node and e["to"] == before_node)
        ]
        graph["edges"].append({"from": after_node, "to": node_id, "contract": contract})
        graph["edges"].append({"from": node_id, "to": before_node, "contract": contract})
    elif after_node:
        graph["edges"].append({"from": after_node, "to": node_id, "contract": contract})
    elif before_node:
        graph["edges"].append({"from": node_id, "to": before_node, "contract": contract})

    graph["step"] += 1
    tag = f"arch-step-{graph['step']:02d}"
    _save_graph(graph)
    _shadow_commit(tag, f"add-node {node_id} ({status})")
    return f"已添加节点 '{node_id}' ({status})。\n\n" + op_focus(node_id if status == "hole" else None)


def op_commit_node(node_id: str, file_path: Union[str, List[str]]) -> str:
    graph = _load_graph()
    nodes = {n["id"]: n for n in graph["nodes"]}
    _get_node(graph, node_id, allow_super=False)
    files = [file_path] if isinstance(file_path, str) else list(file_path)
    files = [f.strip() for f in files if f and f.strip()]
    if not files:
        raise ArchEngineError("commit-node 至少需要一个 --file-path。")

    graph["step"] += 1
    tag = f"node-{node_id}-s{graph['step']:02d}"
    node = nodes[node_id]
    _store_files(node, files)
    node["last_tag"] = tag
    node["impl"] = "built"
    _save_graph(graph)
    shown = ", ".join(files)
    _shadow_commit(tag, f"implement {node_id} -> {shown}", paths=[str(ARCH_DIR)] + files)
    return (
        f"节点 '{node_id}' 代码已锚定至 {shown}，影子快照 {tag} 已生成，实现进度记为 built。\n"
        f"验证程度仍为 {VERIFY_TEXT[_node_state(node)['verify']['level']]}；"
        "测过后用 arch set-node-state --verify unit|api|device 记录。"
    )


def op_sync_manual(node_id: str, summary: str) -> str:
    graph = _load_graph()
    nodes = {n["id"]: n for n in graph["nodes"]}
    _get_node(graph, node_id, allow_super=False)

    node = nodes[node_id]
    node["spec"] = f"{node['spec']} | [人工微调: {summary.strip()}]"
    graph["step"] += 1
    tag = f"manual-{node_id}-s{graph['step']:02d}"
    node["last_tag"] = tag
    _save_graph(graph)
    paths = [str(ARCH_DIR)] + _node_files(node)
    _shadow_commit(tag, f"sync-manual {node_id}: {summary[:40]}", paths=paths)
    return f"已同步人工修改摘要并更新快照 {tag}。"


def op_reset_node(node_id: str, reason: str) -> str:
    graph = _load_graph()
    nodes = {n["id"]: n for n in graph["nodes"]}
    _get_node(graph, node_id, allow_super=False)

    node = nodes[node_id]
    old_label = node["label"]
    rec = _load_records(for_write=True) if _records_enabled() else None
    node["status"] = "hole"
    node["label"] = f"? 待替换: {old_label}"
    node["last_tag"] = ""
    # The replacement scheme starts from scratch; references and files stay.
    node["impl"] = "planned"
    node["verify"] = {"level": "none", "date": ""}

    if rec is not None:
        rec["graveyard"].append({
            "id": _next_id(rec["graveyard"], "GY"),
            "node": node_id,
            "scheme": old_label,
            "reason": reason.strip(),
            "date": _today(),
            "detail": "",
        })
        _save_records(rec)
    else:
        existing_gy = GRAVEYARD_FILE.read_text(encoding="utf-8") if GRAVEYARD_FILE.exists() else "# 踩坑记录\n\n"
        GRAVEYARD_FILE.write_text(
            existing_gy + f"- ❌ [节点 {node_id} / {old_label}] 废弃原因：{reason.strip()}\n",
            encoding="utf-8",
        )

    graph["step"] += 1
    tag = f"reset-{node_id}-s{graph['step']:02d}"
    _save_graph(graph)
    _shadow_commit(tag, f"reset {node_id}: {reason[:40]}")
    return f"已将节点 '{node_id}' 重置为 '?' 待定状态并记录进踩坑备忘。\n\n" + op_focus(node_id)


def op_fork(from_tag: str) -> str:
    _init_shadow_git()
    # Records are knowledge about reality, not graph state: they survive a fork.
    kept = (GRAVEYARD_FILE, GROUND_TRUTH_FILE, OPEN_ITEMS_FILE, RECORDS_FILE)
    backups = {p: p.read_text(encoding="utf-8") for p in kept if p.exists()}

    branch_name = f"fork-{from_tag}-{datetime.now().strftime('%H%M%S')}"
    res = _run_shadow_git(["checkout", "-b", branch_name, from_tag], check=False)
    if res.returncode != 0:
        raise ArchEngineError(f"无法从快照 '{from_tag}' 分叉：{res.stderr.strip()}")

    for path, text in backups.items():
        if text:
            path.write_text(text, encoding="utf-8")
    _shadow_commit(f"{branch_name}-start", f"fork from {from_tag}")
    return f"已成功安全回滚至 {from_tag} 并开启新分支 {branch_name}（已保留所有避坑记录）。\n\n" + op_focus()


def op_deactivate_node(node_id: str, reason: str = "", bypass_contract: str = "") -> str:
    graph = _load_graph()
    nodes = {n["id"]: n for n in graph["nodes"]}
    _get_node(graph, node_id, allow_super=False)

    node = nodes[node_id]
    if node["status"] == "fog":
        return f"节点 '{node_id}' 已经在迷雾区 (fog) 中。\n\n" + op_focus()

    old_status = node["status"]
    node["status"] = "fog"
    node["last_tag"] = ""

    # 提取并缝合关联边
    incoming = [e for e in graph["edges"] if e["to"] == node_id]
    outgoing = [e for e in graph["edges"] if e["from"] == node_id]

    graph["edges"] = [
        e for e in graph["edges"]
        if e["from"] != node_id and e["to"] != node_id
    ]

    bridged_edges = []
    for inc in incoming:
        for out in outgoing:
            u, v = inc["from"], out["to"]
            if u == v:
                continue
            ct = bypass_contract or inc.get("contract") or out.get("contract") or "Bypass-Bridged"
            # Join the original endpoints on both sides, so a bypass touching a
            # folded module still knows which child it really starts or ends at.
            bypass = [
                {"from": a["from"], "to": b["to"], "contract": ct}
                for a in _edge_members(inc) for b in _edge_members(out) if a["from"] != b["to"]
            ]
            existing = next((e for e in graph["edges"] if e["from"] == u and e["to"] == v), None)
            if existing is None:
                graph["edges"].append(_edge_from_members(u, v, bypass))
                bridged_edges.append(f"{u} -> {v}")
                continue
            known = {(m["from"], m["to"]) for m in _edge_members(existing)}
            extra = [m for m in bypass if (m["from"], m["to"]) not in known]
            if extra:
                graph["edges"][graph["edges"].index(existing)] = _edge_from_members(
                    u, v, _edge_members(existing) + extra
                )

    graph["step"] += 1
    tag = f"deactivate-{node_id}-s{graph['step']:02d}"
    _save_graph(graph)
    msg = f"deactivate {node_id} ({old_status} -> fog)"
    if reason:
        msg += f": {reason[:30]}"
    _shadow_commit(tag, msg)

    info = f"已将节点 '{node_id}' 移入战争迷雾区 (fog)。"
    if bridged_edges:
        info += f"\n已自动缝合主干旁路：{', '.join(bridged_edges)}。"
    return info + "\n\n" + op_focus()


def op_activate_node(
    node_id: str,
    after_node: str = "",
    before_node: str = "",
    contract: str = "TBD",
) -> str:
    graph = _load_graph()
    nodes = {n["id"]: n for n in graph["nodes"]}
    _get_node(graph, node_id, allow_super=True)

    node = nodes[node_id]
    if node["status"] != "fog":
        return f"节点 '{node_id}' 当前状态为 '{node['status']}'，并非处于迷雾区。\n\n" + op_focus(node_id)

    holes = [n for n in graph["nodes"] if n["status"] == "hole"]
    if len(holes) >= MAX_ACTIVE_HOLES:
        raise ArchEngineError(
            f"[CircuitBreakerError] 未决空洞已达上限 ({MAX_ACTIVE_HOLES})！"
            "JIT 指令：必须先通过 collapse 收敛现有空洞，才能激活新的迷雾节点。"
        )
    for ref in (after_node, before_node):
        if ref:
            _get_node(graph, ref)

    node["status"] = "hole"
    if not node["label"].startswith("?"):
        node["label"] = f"? {node['label']}"

    if after_node and before_node:
        graph["edges"] = [
            e for e in graph["edges"]
            if not (e["from"] == after_node and e["to"] == before_node)
        ]
        graph["edges"].append({"from": after_node, "to": node_id, "contract": contract})
        graph["edges"].append({"from": node_id, "to": before_node, "contract": contract})
    elif after_node:
        graph["edges"].append({"from": after_node, "to": node_id, "contract": contract})
    elif before_node:
        graph["edges"].append({"from": node_id, "to": before_node, "contract": contract})

    graph["step"] += 1
    tag = f"activate-{node_id}-s{graph['step']:02d}"
    _save_graph(graph)
    _shadow_commit(tag, f"activate {node_id} (fog -> hole)")
    return f"已成功从迷雾区激活节点 '{node_id}' 并接入主干！\n\n" + op_focus(node_id)


def op_set_node_state(
    node_id: str,
    impl: str = "",
    verify: str = "",
    verify_date: str = "",
    refs: Optional[List[str]] = None,
    add_refs: Optional[List[str]] = None,
    drop_refs: Optional[List[str]] = None,
    file_paths: Optional[List[str]] = None,
) -> str:
    """Record implementation progress, verification depth, references and files.

    Works on folded children too: these fields are bookkeeping, not topology.
    `refs` / `file_paths` replace the whole list; add_refs / drop_refs edit it.
    """
    graph = _load_graph()
    node = _find_any_node(graph, node_id)
    if node is None:
        raise ArchEngineError(f"节点 '{node_id}' 不存在。")
    if node["status"] == "super_node":
        raise ArchEngineError(
            f"'{node_id}' 是折叠模块，它的进度由子节点汇总。请对其中的子节点调用 set-node-state。"
        )
    if impl and impl not in IMPL_LEVELS:
        raise ArchEngineError(f"impl 只能是 {' / '.join(IMPL_LEVELS)}，收到：{impl}")
    if verify and verify not in VERIFY_LEVELS:
        raise ArchEngineError(f"verify 只能是 {' / '.join(VERIFY_LEVELS)}，收到：{verify}")
    if verify_date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", verify_date):
        raise ArchEngineError(f"verify_date 需为 YYYY-MM-DD，收到：{verify_date}")

    before = json.dumps(_node_state(node), sort_keys=True)
    state = _node_state(node)
    if impl:
        node["impl"] = impl
    if verify or verify_date:
        level = verify or state["verify"]["level"]
        if level == "none" and verify_date:
            raise ArchEngineError("验证程度为 none 时不能设置验证日期，请同时给出 --verify。")
        node["verify"] = {"level": level, "date": "" if level == "none" else (verify_date or _today())}
    if refs is not None or add_refs or drop_refs:
        new_refs = list(refs) if refs is not None else list(state["refs"])
        new_refs += list(add_refs or [])
        drop = {r.strip() for r in (drop_refs or [])}
        missing = sorted(drop - {r.strip() for r in new_refs})
        if missing:
            raise ArchEngineError(f"要移除的引用不存在：{', '.join(missing)}。当前引用：{', '.join(state['refs']) or '无'}")
        node["refs"] = list(dict.fromkeys(r.strip() for r in new_refs if r.strip() and r.strip() not in drop))
    if file_paths is not None:
        _store_files(node, file_paths)

    if json.dumps(_node_state(node), sort_keys=True) == before:
        return f"节点 '{node_id}' 的状态没有变化：{_state_text(node)}。"
    graph["step"] += 1
    tag = f"state-{node_id}-s{graph['step']:02d}"
    _save_graph(graph)
    _shadow_commit(tag, f"set-node-state {node_id}")
    st = _node_state(node)
    lines = [f"已更新节点 '{node_id}' 的状态，快照 {tag}：{_state_text(node)}"]
    if st["files"]:
        lines.append(f"实现文件：{', '.join(st['files'])}")
    if st["refs"]:
        lines.append(f"引用：{', '.join(st['refs'])}")
    return "\n".join(lines)
