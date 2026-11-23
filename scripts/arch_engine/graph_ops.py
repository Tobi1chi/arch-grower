"""Operations that change nodes, edges, project initialization and local CLI bootstrap."""

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .common import (
    ARCH_DIR, ArchEngineError, CONFIG_FILE, ENGINE_VERSION, GRAVEYARD_FILE, GROUNDING_FILE,
    GROUND_TRUTH_FILE, OPEN_ITEMS_FILE, RECORDS_FILE, SEED_FILE, _default_config, _init_shadow_git,
    _load_config, _load_graph, _run_shadow_git, _save_config, _save_graph, _shadow_commit, _today,
)
from .graph import (
    IMPL_LEVELS, VERIFY_LEVELS, VERIFY_TEXT, _edge_from_members, _edge_kind, _edge_members,
    _find_any_node, _get_node, _iter_all_nodes, _node_files, _node_state,
    _reject_manual_super_type, _resolve_container, _state_text, _store_files,
    _validate_edge_kind, _validate_node_role, _validate_topology,
)
from .records import (
    GT_TITLE, GY_TITLE, _load_records, _md_is_boilerplate, _new_records, _next_id,
    _records_enabled, _save_records,
)
from .views import op_focus


def _replace_edges(container: Dict[str, Any], new_edges: List[Dict[str, Any]]) -> None:
    if "inner_edges" in container:
        container["inner_edges"] = new_edges
    else:
        container["edges"] = new_edges


def _initial_skeleton(topology: str) -> Dict[str, Any]:
    """Return initial nodes and edges for `op_init` according to topology."""
    if topology == "star":
        return {
            "topology": "star",
            "nodes": [
                {
                    "id": "core_hub",
                    "label": "? 核心调度与事件中枢",
                    "status": "hole",
                    "type": "port",
                    "role": "hub",
                    "spec": "待定义中心中枢的通信协议、状态边界与事件分发规则",
                    "file_path": "",
                    "last_tag": "",
                },
                {
                    "id": "spoke_a",
                    "label": "? 外围通道 / 模块 A",
                    "status": "hole",
                    "type": "port",
                    "role": "spoke",
                    "spec": "待定义接入中枢的第一个核心能力或交互通道",
                    "file_path": "",
                    "last_tag": "",
                },
                {
                    "id": "spoke_b",
                    "label": "? 外围通道 / 模块 B",
                    "status": "hole",
                    "type": "port",
                    "role": "spoke",
                    "spec": "待定义接入中枢的第二个核心能力或执行模块",
                    "file_path": "",
                    "last_tag": "",
                },
            ],
            "edges": [
                {"from": "core_hub", "to": "spoke_a", "contract": "TBD", "kind": "bidirectional"},
                {"from": "core_hub", "to": "spoke_b", "contract": "TBD", "kind": "bidirectional"},
            ],
        }

    if topology == "ring":
        return {
            "topology": "ring",
            "nodes": [
                {
                    "id": "perceive",
                    "label": "? 感知与观测入口",
                    "status": "hole",
                    "type": "port",
                    "role": "entry",
                    "spec": "待定义系统每轮闭环如何接收外部触发或观测最新状态",
                    "file_path": "",
                    "last_tag": "",
                },
                {
                    "id": "decide",
                    "label": "? 决策与规划核心",
                    "status": "hole",
                    "type": "port",
                    "spec": "待定义基于当前状态做决策、路由或生成行动计划的逻辑",
                    "file_path": "",
                    "last_tag": "",
                },
                {
                    "id": "act",
                    "label": "? 执行与状态回写",
                    "status": "hole",
                    "type": "port",
                    "role": "exit",
                    "spec": "待定义执行外部动作并将结果反馈回下一轮感知的逻辑",
                    "file_path": "",
                    "last_tag": "",
                },
            ],
            "edges": [
                {"from": "perceive", "to": "decide", "contract": "TBD"},
                {"from": "decide", "to": "act", "contract": "TBD"},
                {"from": "act", "to": "perceive", "contract": "TBD (闭环反馈)", "kind": "feedback"},
            ],
        }

    # Default: pipeline (exact backward-compatible 3-node skeleton)
    return {
        "topology": "pipeline",
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


def op_bootstrap(target_dir: str = "") -> str:
    """Copy the zero-dependency CLI & engine into `.arch/scripts/` so the project runs directly with python3."""
    src_scripts = Path(__file__).resolve().parent.parent
    dst_scripts = Path(target_dir).resolve() if target_dir else (ARCH_DIR / "scripts").resolve()

    if src_scripts.resolve() == dst_scripts:
        return f"CLI 脚本已内置在当前项目目录：{dst_scripts}（版本 {ENGINE_VERSION}），可直接运行 python3 .arch/scripts/arch_cli.py。"

    dst_scripts.mkdir(parents=True, exist_ok=True)
    for filename in ("arch_cli.py", "dashboard_template.html"):
        src_file = src_scripts / filename
        if src_file.exists():
            shutil.copy2(src_file, dst_scripts / filename)

    src_engine = src_scripts / "arch_engine"
    dst_engine = dst_scripts / "arch_engine"
    if dst_engine.exists():
        shutil.rmtree(dst_engine)
    shutil.copytree(
        src_engine,
        dst_engine,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    (dst_scripts / "VERSION").write_text(f"{ENGINE_VERSION}\n", encoding="utf-8")
    return (
        f"已将 arch-grower CLI 引擎（v{ENGINE_VERSION}）内嵌至：{dst_scripts}\n"
        "后续在项目根目录直接运行：python3 .arch/scripts/arch_cli.py <命令>（无需 uv --no-project，且不会被 Shadow Git 纳入快照）。"
    )


def op_init(seed: str, topology: str = "", preset: str = "standard", embed: bool = False) -> str:
    cfg = _default_config(preset)
    topo = _validate_topology(topology or cfg["default_topology"])
    if topology:
        cfg["default_topology"] = topo

    ARCH_DIR.mkdir(parents=True, exist_ok=True)
    _init_shadow_git()

    SEED_FILE.write_text(f"# 项目初始意图 (SEED)\n\n{seed.strip()}\n", encoding="utf-8")
    _save_config(cfg)

    old_md = [p for p in (GROUND_TRUTH_FILE, GRAVEYARD_FILE)
              if p.exists() and not _md_is_boilerplate(p.read_text(encoding="utf-8"))]
    if not _records_enabled() and not old_md:
        # New projects start with typed records. A re-init over old-format md
        # leaves them alone; migrate-records converts them explicitly.
        rec = _new_records()
        rec["settings"]["global_fact_char_limit"] = int(cfg["global_fact_char_limit"])
        _save_records(rec)
    if not GROUND_TRUTH_FILE.exists():
        GROUND_TRUTH_FILE.write_text(f"{GT_TITLE}\n\n", encoding="utf-8")
    if not GRAVEYARD_FILE.exists():
        GRAVEYARD_FILE.write_text(f"{GY_TITLE}\n\n", encoding="utf-8")
    if not GROUNDING_FILE.exists():
        GROUNDING_FILE.write_text(json.dumps({"sources": []}, indent=2) + "\n", encoding="utf-8")

    skeleton = _initial_skeleton(topo)
    graph: Dict[str, Any] = {
        "step": 1,
        "topology": skeleton["topology"],
        "nodes": skeleton["nodes"],
        "edges": skeleton["edges"],
    }
    _save_graph(graph)
    _shadow_commit("arch-step-01", f"init ({topo}/{preset}): {seed[:40]}")
    extra = f"{op_bootstrap()}\n\n" if embed else ""
    return extra + op_focus()


def op_collapse(
    node_id: str,
    label: str,
    spec: str,
    file_path: str = "",
    in_contract: str = "",
    out_contract: str = "",
    node_type: str = "port",
    stage: str = "",
    role: str = "",
    scope: str = "",
) -> str:
    _reject_manual_super_type(node_type)
    if role:
        _validate_node_role(role)
    graph = _load_graph()
    container, c_nodes, c_edges, _, _ = _resolve_container(graph, scope)
    node = _get_node(container, node_id, allow_super=False)

    node["label"] = label
    node["spec"] = spec
    node["status"] = "solid"
    node["type"] = node_type
    if stage:
        node["stage"] = stage
    if role:
        node["role"] = role
    if file_path:
        node["file_path"] = file_path

    if in_contract:
        for e in c_edges:
            if e["to"] == node_id:
                e["contract"] = in_contract
    if out_contract:
        for e in c_edges:
            if e["from"] == node_id:
                e["contract"] = out_contract

    graph["step"] += 1
    tag = f"arch-step-{graph['step']:02d}"
    _save_graph(graph)
    _shadow_commit(tag, f"collapse {node_id}: {label}")
    return f"已锁定节点 '{node_id}' ({label})，快照 {tag}。\n\n" + op_focus(scope=scope)


def op_connect(
    from_node: str,
    to_node: str,
    contract: str = "TBD",
    kind: str = "primary",
    scope: str = "",
) -> str:
    """Connect two existing nodes within the same container (supports feedback / bidirectional edges)."""
    ekind = _validate_edge_kind(kind)
    if from_node == to_node:
        raise ArchEngineError("不支持将节点连向自身（自环）。")

    graph = _load_graph()
    cfg = _load_config()
    if ekind == "feedback" and not cfg.get("allow_feedback", True):
        raise ArchEngineError(
            "当前项目 preset 禁止回流边 (allow_feedback=false)。\n"
            "JIT 指令：若该架构确实需要闭环反馈，向用户确认后运行 arch config --allow-feedback true。"
        )

    container, _, c_edges, _, _ = _resolve_container(graph, scope)
    _get_node(container, from_node)
    _get_node(container, to_node)

    dup = any(
        (e["from"] == from_node and e["to"] == to_node)
        or (ekind == "bidirectional" and e["from"] == to_node and e["to"] == from_node and _edge_kind(e) == "bidirectional")
        or (_edge_kind(e) == "bidirectional" and e["from"] == to_node and e["to"] == from_node)
        for e in c_edges
    )
    if dup:
        raise ArchEngineError(f"连线 '{from_node} -> {to_node}' 已存在；如需修改契约请使用 set-edge-contract。")

    edge: Dict[str, Any] = {"from": from_node, "to": to_node, "contract": contract}
    if ekind != "primary":
        edge["kind"] = ekind
    c_edges.append(edge)

    graph["step"] += 1
    tag = f"arch-step-{graph['step']:02d}"
    _save_graph(graph)
    arrow = "<->" if ekind == "bidirectional" else "->"
    _shadow_commit(tag, f"connect {from_node} {arrow} {to_node} ({ekind})")
    return (
        f"已建立连线 '{from_node} {arrow} {to_node}'（类型：{ekind}，契约：{contract}），快照 {tag}。\n\n"
        + op_focus(from_node, scope=scope)
    )


def op_disconnect(from_node: str, to_node: str, scope: str = "") -> str:
    """Remove an edge between two nodes in the same container."""
    graph = _load_graph()
    container, _, c_edges, _, _ = _resolve_container(graph, scope)

    idx = next(
        (i for i, e in enumerate(c_edges) if e["from"] == from_node and e["to"] == to_node),
        None,
    )
    if idx is None:
        idx = next(
            (
                i for i, e in enumerate(c_edges)
                if _edge_kind(e) == "bidirectional" and e["from"] == to_node and e["to"] == from_node
            ),
            None,
        )
    if idx is None:
        raise ArchEngineError(f"连线 '{from_node} -> {to_node}' 不存在。")

    removed = c_edges.pop(idx)
    graph["step"] += 1
    tag = f"arch-step-{graph['step']:02d}"
    _save_graph(graph)
    _shadow_commit(tag, f"disconnect {removed['from']} -> {removed['to']}")
    return f"已移除连线 '{removed['from']} -> {removed['to']}'，快照 {tag}。\n\n" + op_focus(scope=scope)


def op_set_edge_contract(
    from_node: str,
    to_node: str,
    contract: str,
    kind: str = "",
    scope: str = "",
) -> str:
    graph = _load_graph()
    cfg = _load_config()
    container, _, c_edges, _, _ = _resolve_container(graph, scope)
    matches = [
        edge for edge in c_edges
        if edge["from"] == from_node and edge["to"] == to_node
    ]
    if not matches:
        matches = [
            edge for edge in c_edges
            if _edge_kind(edge) == "bidirectional" and edge["from"] == to_node and edge["to"] == from_node
        ]
    edge_label = f"{from_node} -> {to_node}"
    if len(matches) != 1:
        raise ArchEngineError(f"需要唯一的连线 '{edge_label}'，找到 {len(matches)} 条。")

    edge = matches[0]
    new_kind = _validate_edge_kind(kind) if kind else _edge_kind(edge)
    if new_kind == "feedback" and not cfg.get("allow_feedback", True):
        raise ArchEngineError(
            "当前项目 preset 禁止回流边 (allow_feedback=false)。请先通过 arch config --allow-feedback true 放开。"
        )

    members_match = not kind or all(_edge_kind(m) == new_kind for m in (edge.get("members") or []))
    if edge.get("contract") == contract and _edge_kind(edge) == new_kind and members_match:
        return f"连线 '{edge_label}' 已使用该契约。"

    edge["contract"] = contract
    if new_kind == "primary":
        edge.pop("kind", None)
    else:
        edge["kind"] = new_kind
    if kind and edge.get("members"):
        for m in edge["members"]:
            if new_kind == "primary":
                m.pop("kind", None)
            else:
                m["kind"] = new_kind

    graph["step"] += 1
    tag = f"arch-step-{graph['step']:02d}"
    _save_graph(graph)
    _shadow_commit(tag, f"set-edge-contract {edge_label}")
    return f"已更新连线 '{edge_label}' 的契约，快照 {tag}。\n\n" + op_focus(from_node, scope=scope)


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
    role: str = "",
    edge_kind: str = "primary",
    scope: str = "",
) -> str:
    _reject_manual_super_type(node_type)
    ekind = _validate_edge_kind(edge_kind)
    nrole = _validate_node_role(role)

    graph = _load_graph()
    cfg = _load_config()
    if ekind == "feedback" and not cfg.get("allow_feedback", True):
        raise ArchEngineError(
            "当前项目 preset 禁止回流边 (allow_feedback=false)。请先通过 arch config --allow-feedback true 放开。"
        )
    if any(n["id"] == node_id for n in _iter_all_nodes(graph["nodes"])):
        raise ArchEngineError(f"节点 '{node_id}' 已存在。")

    container, c_nodes, c_edges, _, _ = _resolve_container(graph, scope)
    for ref in (after_node, before_node):
        if ref:
            _get_node(container, ref)

    max_holes = int(cfg.get("max_active_holes", 3))
    holes = [n for n in c_nodes if n["status"] == "hole"]
    if status == "hole" and len(holes) >= max_holes:
        raise ArchEngineError(
            f"[CircuitBreakerError] 未决空洞已达上限 ({max_holes})！"
            "JIT 指令：禁止继续新增 '?' 空洞，必须先通过 collapse 收敛现有空洞，或将非核心想法设为 status='fog'。"
        )

    new_node: Dict[str, Any] = {
        "id": node_id,
        "label": label,
        "status": status,
        "type": node_type,
        "stage": stage,
        "spec": spec,
        "file_path": "",
        "last_tag": "",
    }
    if nrole:
        new_node["role"] = nrole
    c_nodes.append(new_node)

    def mk_edge(u: str, v: str) -> Dict[str, Any]:
        e: Dict[str, Any] = {"from": u, "to": v, "contract": contract}
        if ekind != "primary":
            e["kind"] = ekind
        return e

    if after_node and before_node:
        filtered = [
            e for e in c_edges
            if not (e["from"] == after_node and e["to"] == before_node)
        ]
        filtered.append(mk_edge(after_node, node_id))
        filtered.append(mk_edge(node_id, before_node))
        _replace_edges(container, filtered)
    elif after_node:
        c_edges.append(mk_edge(after_node, node_id))
    elif before_node:
        c_edges.append(mk_edge(node_id, before_node))

    graph["step"] += 1
    tag = f"arch-step-{graph['step']:02d}"
    _save_graph(graph)
    _shadow_commit(tag, f"add-node {node_id} ({status})")
    return f"已添加节点 '{node_id}' ({status})。\n\n" + op_focus(node_id if status == "hole" else None, scope=scope)


def op_commit_node(node_id: str, file_path: Union[str, List[str]], scope: str = "") -> str:
    graph = _load_graph()
    container, _, _, _, _ = _resolve_container(graph, scope)
    node = _get_node(container, node_id, allow_super=False)
    files = [file_path] if isinstance(file_path, str) else list(file_path)
    files = [f.strip() for f in files if f and f.strip()]
    if not files:
        raise ArchEngineError("commit-node 至少需要一个 --file-path。")

    graph["step"] += 1
    tag = f"node-{node_id}-s{graph['step']:02d}"
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


def op_sync_manual(node_id: str, summary: str, scope: str = "") -> str:
    graph = _load_graph()
    container, _, _, _, _ = _resolve_container(graph, scope)
    node = _get_node(container, node_id, allow_super=False)

    node["spec"] = f"{node['spec']} | [人工微调: {summary.strip()}]"
    graph["step"] += 1
    tag = f"manual-{node_id}-s{graph['step']:02d}"
    node["last_tag"] = tag
    _save_graph(graph)
    paths = [str(ARCH_DIR)] + _node_files(node)
    _shadow_commit(tag, f"sync-manual {node_id}: {summary[:40]}", paths=paths)
    return f"已同步人工修改摘要并更新快照 {tag}。"


def op_reset_node(node_id: str, reason: str, scope: str = "") -> str:
    graph = _load_graph()
    container, _, _, _, _ = _resolve_container(graph, scope)
    node = _get_node(container, node_id, allow_super=False)

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
    return f"已将节点 '{node_id}' 重置为 '?' 待定状态并记录进踩坑备忘。\n\n" + op_focus(node_id, scope=scope)


def op_fork(from_tag: str) -> str:
    _init_shadow_git()
    # Records & project preset are knowledge/policy, not graph branch state: they survive a fork.
    kept = (GRAVEYARD_FILE, GROUND_TRUTH_FILE, OPEN_ITEMS_FILE, RECORDS_FILE, CONFIG_FILE)
    backups = {p: p.read_text(encoding="utf-8") for p in kept if p.exists()}

    branch_name = f"fork-{from_tag}-{datetime.now().strftime('%H%M%S')}"
    res = _run_shadow_git(["checkout", "-b", branch_name, from_tag], check=False)
    if res.returncode != 0:
        raise ArchEngineError(f"无法从快照 '{from_tag}' 分叉：{res.stderr.strip()}")

    for path, text in backups.items():
        if text:
            path.write_text(text, encoding="utf-8")
    _shadow_commit(f"{branch_name}-start", f"fork from {from_tag}")
    return f"已成功安全回滚至 {from_tag} 并开启新分支 {branch_name}（已保留所有避坑记录与项目预设）。\n\n" + op_focus()


def op_deactivate_node(
    node_id: str,
    reason: str = "",
    bypass_contract: str = "",
    no_bridge: bool = False,
    scope: str = "",
) -> str:
    graph = _load_graph()
    cfg = _load_config()
    container, _, c_edges, _, _ = _resolve_container(graph, scope)
    node = _get_node(container, node_id, allow_super=False)

    if node["status"] == "fog":
        return f"节点 '{node_id}' 已经在迷雾区 (fog) 中。\n\n" + op_focus(scope=scope)

    # Extract incident edges (treating bidirectional edges carefully so they don't explode).
    incoming = [e for e in c_edges if e["to"] == node_id and _edge_kind(e) != "bidirectional"]
    outgoing = [e for e in c_edges if e["from"] == node_id and _edge_kind(e) != "bidirectional"]
    bidir_edges = [e for e in c_edges if _edge_kind(e) == "bidirectional" and node_id in (e["from"], e["to"])]

    bridge_pairs = [
        (inc, out)
        for inc in incoming for out in outgoing
        if inc["from"] != out["to"]
    ]
    bridge_limit = int(cfg.get("auto_bridge_limit", 4))
    if not no_bridge:
        if node.get("role") == "hub" and (bridge_pairs or len(bidir_edges) >= 2):
            raise ArchEngineError(
                f"[AutoBridgeLimitError] 节点 '{node_id}' 是星形中枢 (role=hub)，直接下线会自动缝合大量交叉边并破坏星形结构。\n"
                "JIT 指令：若要更换中枢方案请使用 arch reset-node；若确实要拆除连接下线，请显式加上 --no-bridge。"
            )
        if len(bridge_pairs) > bridge_limit:
            raise ArchEngineError(
                f"[AutoBridgeLimitError] 下线 '{node_id}' 将触发 {len(bridge_pairs)} 条旁路缝合边，"
                f"超过 preset 上限 ({bridge_limit})。\n"
                "JIT 指令：建议改用 arch reset-node，或显式传 --no-bridge 断开而不自动缝合。"
            )

    old_status = node["status"]
    node["status"] = "fog"
    node["last_tag"] = ""

    remaining_edges = [
        e for e in c_edges
        if e["from"] != node_id and e["to"] != node_id
    ]

    bridged_edges = []
    if not no_bridge:
        for inc, out in bridge_pairs:
            u, v = inc["from"], out["to"]
            ct = bypass_contract or inc.get("contract") or out.get("contract") or "Bypass-Bridged"
            # Join the original endpoints on both sides, so a bypass touching a
            # folded module still knows which child it really starts or ends at.
            bypass = [
                {"from": a["from"], "to": b["to"], "contract": ct}
                for a in _edge_members(inc) for b in _edge_members(out) if a["from"] != b["to"]
            ]
            existing = next((e for e in remaining_edges if e["from"] == u and e["to"] == v), None)
            if existing is None:
                remaining_edges.append(_edge_from_members(u, v, bypass))
                bridged_edges.append(f"{u} -> {v}")
                continue
            known = {(m["from"], m["to"]) for m in _edge_members(existing)}
            extra = [m for m in bypass if (m["from"], m["to"]) not in known]
            if extra:
                remaining_edges[remaining_edges.index(existing)] = _edge_from_members(
                    u, v, _edge_members(existing) + extra
                )

    _replace_edges(container, remaining_edges)

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
    elif no_bridge:
        info += "\n已按 --no-bridge 断开关联连线（未自动缝合旁路）。"
    return info + "\n\n" + op_focus(scope=scope)


def op_activate_node(
    node_id: str,
    after_node: str = "",
    before_node: str = "",
    contract: str = "TBD",
    edge_kind: str = "primary",
    scope: str = "",
) -> str:
    ekind = _validate_edge_kind(edge_kind)
    graph = _load_graph()
    cfg = _load_config()
    if ekind == "feedback" and not cfg.get("allow_feedback", True):
        raise ArchEngineError(
            "当前项目 preset 禁止回流边 (allow_feedback=false)。请先通过 arch config --allow-feedback true 放开。"
        )

    container, c_nodes, c_edges, _, _ = _resolve_container(graph, scope)
    node = _get_node(container, node_id, allow_super=True)

    if node["status"] != "fog":
        return f"节点 '{node_id}' 当前状态为 '{node['status']}'，并非处于迷雾区。\n\n" + op_focus(node_id, scope=scope)

    max_holes = int(cfg.get("max_active_holes", 3))
    holes = [n for n in c_nodes if n["status"] == "hole"]
    if len(holes) >= max_holes:
        raise ArchEngineError(
            f"[CircuitBreakerError] 未决空洞已达上限 ({max_holes})！"
            "JIT 指令：必须先通过 collapse 收敛现有空洞，才能激活新的迷雾节点。"
        )
    for ref in (after_node, before_node):
        if ref:
            _get_node(container, ref)

    node["status"] = "hole"
    if not node["label"].startswith("?"):
        node["label"] = f"? {node['label']}"

    def mk_edge(u: str, v: str) -> Dict[str, Any]:
        e: Dict[str, Any] = {"from": u, "to": v, "contract": contract}
        if ekind != "primary":
            e["kind"] = ekind
        return e

    if after_node and before_node:
        filtered = [
            e for e in c_edges
            if not (e["from"] == after_node and e["to"] == before_node)
        ]
        filtered.append(mk_edge(after_node, node_id))
        filtered.append(mk_edge(node_id, before_node))
        _replace_edges(container, filtered)
    elif after_node:
        c_edges.append(mk_edge(after_node, node_id))
    elif before_node:
        c_edges.append(mk_edge(node_id, before_node))

    graph["step"] += 1
    tag = f"activate-{node_id}-s{graph['step']:02d}"
    _save_graph(graph)
    _shadow_commit(tag, f"activate {node_id} (fog -> hole)")
    return f"已成功从迷雾区激活节点 '{node_id}' 并接入主干！\n\n" + op_focus(node_id, scope=scope)


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
