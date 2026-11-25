"""Operations that change nodes, edges, project initialization and local CLI bootstrap."""

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .common import (
    ARCH_DIR, ArchEngineError, BASIC_PRESETS, CONFIG_FILE, ENGINE_VERSION, MODES, _mode_for, GRAVEYARD_FILE, GROUNDING_FILE,
    GROUND_TRUTH_FILE, OPEN_ITEMS_FILE, RECORDS_FILE, SEED_FILE, _default_config, _init_shadow_git,
    _run_shadow_git, _save_config, _shadow_commit, _today, _tracked_arch_extras,
)
from .graph import (
    IMPL_LEVELS, VERIFY_LEVELS, VERIFY_TEXT, _container_topology, _edge_kind,
    _edge_members, _is_feedback, _node_files, _node_state, _reject_manual_super_type, _star_hub_id,
    _state_text, _store_files, _validate_edge_kind, _validate_node_role, _validate_topology,
)
from .records import (
    GT_TITLE, GY_TITLE, _load_records, _md_is_boilerplate, _new_records, _next_id,
    _records_enabled, _save_records,
)
from .kernel import GraphTx
from .views import op_focus


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


def op_init(
    seed: str, topology: str = "", preset: str = "standard", embed: bool = False, mode: str = "",
) -> str:
    cfg = _default_config(preset)
    topo = _validate_topology(topology or cfg["default_topology"])
    if topology:
        cfg["default_topology"] = topo
    needed = _mode_for(preset, topo)
    if mode and mode not in MODES:
        raise ArchEngineError(f"未知 mode '{mode}'。可选：{' / '.join(MODES)}。")
    if mode == "basic" and needed == "advanced":
        raise ArchEngineError(
            f"basic 模式只支持 pipeline 拓扑与 {' / '.join(BASIC_PRESETS)} 预设，"
            f"收到 topology={topo}、preset={preset}。JIT 指令：改用 pipeline，或去掉 --mode basic。"
        )
    cfg["mode"] = mode or needed
    mode_note = ""
    if cfg["mode"] == "advanced" and not mode:
        mode_note = f"[MODE] topology={topo} / preset={preset} 需要 advanced 模式，项目已按 advanced 初始化。\n\n"

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
    GraphTx(graph).commit("arch-step-01", f"init ({topo}/{preset}): {seed[:40]}")
    extra = f"{op_bootstrap()}\n\n" if embed else ""
    return extra + mode_note + op_focus()


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
    tx = GraphTx()
    c = tx.container(scope)
    node = c.get(node_id, allow_super=False)

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
        for e in c.edges:
            if e["to"] == node_id:
                e["contract"] = in_contract
    if out_contract:
        for e in c.edges:
            if e["from"] == node_id:
                e["contract"] = out_contract

    tag = f"arch-step-{tx.bump():02d}"
    tx.commit(tag, f"collapse {node_id}: {label}")
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

    tx = GraphTx()
    if ekind == "feedback" and not tx.cfg.get("allow_feedback", True):
        raise ArchEngineError(
            "当前项目 preset 禁止回流边 (allow_feedback=false)。\n"
            "JIT 指令：若该架构确实需要闭环反馈，向用户确认后运行 arch config --allow-feedback true。"
        )

    c = tx.container(scope)
    c.get(from_node)
    c.get(to_node)

    between = c.edges_between(from_node, to_node)
    if any(_edge_kind(e) == "bidirectional" for e in between):
        raise ArchEngineError(
            f"'{from_node}' 与 '{to_node}' 之间已有双向连线，它已覆盖两个方向；如需修改契约请使用 set-edge-contract。"
        )
    if ekind != "bidirectional" and any(e["from"] == from_node and e["to"] == to_node for e in between):
        raise ArchEngineError(f"连线 '{from_node} -> {to_node}' 已存在；如需修改契约请使用 set-edge-contract。")

    edge: Dict[str, Any] = {"from": from_node, "to": to_node, "contract": contract}
    if ekind != "primary":
        edge["kind"] = ekind
    # A bidirectional link absorbs any one-way edges already between the pair.
    c.add_edge(edge)
    merged = ekind == "bidirectional" and bool(between)

    tag = f"arch-step-{tx.bump():02d}"
    arrow = "<->" if ekind == "bidirectional" else "->"
    tx.commit(tag, f"connect {from_node} {arrow} {to_node} ({ekind})")
    note = ""
    if merged:
        absorbed = ", ".join(f"{e['from']} -> {e['to']}" for e in between)
        note = f"\n已将原有单向连线（{absorbed}）并入这条双向连线，原连线保留为溯源成员。"
    return (
        f"已建立连线 '{from_node} {arrow} {to_node}'（类型：{ekind}，契约：{contract}），快照 {tag}。{note}\n\n"
        + op_focus(from_node, scope=scope)
    )


def op_disconnect(from_node: str, to_node: str, scope: str = "") -> str:
    """Remove an edge between two nodes in the same container."""
    tx = GraphTx()
    c = tx.container(scope)
    found = c.find_link(from_node, to_node)
    if not found:
        raise ArchEngineError(f"连线 '{from_node} -> {to_node}' 不存在。")
    removed = c.remove_edge(found[0]["from"], found[0]["to"])

    tag = f"arch-step-{tx.bump():02d}"
    tx.commit(tag, f"disconnect {removed['from']} -> {removed['to']}")
    return f"已移除连线 '{removed['from']} -> {removed['to']}'，快照 {tag}。\n\n" + op_focus(scope=scope)


def op_set_edge_contract(
    from_node: str,
    to_node: str,
    contract: str,
    kind: str = "",
    scope: str = "",
) -> str:
    tx = GraphTx()
    c = tx.container(scope)
    matches = c.find_link(from_node, to_node)
    edge_label = f"{from_node} -> {to_node}"
    if len(matches) != 1:
        raise ArchEngineError(f"需要唯一的连线 '{edge_label}'，找到 {len(matches)} 条。")

    edge = matches[0]
    new_kind = _validate_edge_kind(kind) if kind else _edge_kind(edge)
    if new_kind == "feedback" and not tx.cfg.get("allow_feedback", True):
        raise ArchEngineError(
            "当前项目 preset 禁止回流边 (allow_feedback=false)。请先通过 arch config --allow-feedback true 放开。"
        )

    members_match = not kind or all(_edge_kind(m) == new_kind for m in (edge.get("members") or []))
    if edge.get("contract") == contract and _edge_kind(edge) == new_kind and members_match:
        return f"连线 '{edge_label}' 已使用该契约。"

    u, v = edge["from"], edge["to"]
    outcome = c.retype_link(edge, contract, kind)
    note = {
        "merged": "\n已将同一对节点间的其他连线并入这条双向连线，原连线保留为溯源成员。",
        "split": f"\n原双向连线含两个方向的流，已按方向拆为 {u} -> {v} 与 {v} -> {u}。",
    }.get(outcome, "")

    tag = f"arch-step-{tx.bump():02d}"
    tx.commit(tag, f"set-edge-contract {edge_label}")
    return f"已更新连线 '{edge_label}' 的契约，快照 {tag}。{note}\n\n" + op_focus(from_node, scope=scope)


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
    edge_kind: str = "",
    scope: str = "",
) -> str:
    _reject_manual_super_type(node_type)
    ekind = _validate_edge_kind(edge_kind) if edge_kind else ""
    nrole = _validate_node_role(role)

    tx = GraphTx()
    cfg = tx.cfg
    if ekind == "feedback" and not cfg.get("allow_feedback", True):
        raise ArchEngineError(
            "当前项目 preset 禁止回流边 (allow_feedback=false)。请先通过 arch config --allow-feedback true 放开。"
        )
    if node_id in tx.all_ids():
        raise ArchEngineError(f"节点 '{node_id}' 已存在。")

    c = tx.container(scope)
    for ref in (after_node, before_node):
        if ref:
            c.get(ref)

    max_holes = int(cfg.get("max_active_holes", 3))
    holes = [n for n in c.nodes if n["status"] == "hole"]
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
    c.insert_node(new_node)
    c.wire_inserted(node_id, after_node, before_node, contract, ekind)

    tag = f"arch-step-{tx.bump():02d}"
    tx.commit(tag, f"add-node {node_id} ({status})")
    return f"已添加节点 '{node_id}' ({status})。\n\n" + op_focus(node_id if status == "hole" else None, scope=scope)


def op_commit_node(node_id: str, file_path: Union[str, List[str]], scope: str = "") -> str:
    tx = GraphTx()
    node = tx.container(scope).get(node_id, allow_super=False)
    files = [file_path] if isinstance(file_path, str) else list(file_path)
    files = [f.strip() for f in files if f and f.strip()]
    if not files:
        raise ArchEngineError("commit-node 至少需要一个 --file-path。")

    tag = f"node-{node_id}-s{tx.bump():02d}"
    _store_files(node, files)
    node["last_tag"] = tag
    node["impl"] = "built"
    shown = ", ".join(files)
    tx.commit(tag, f"implement {node_id} -> {shown}", paths=[str(ARCH_DIR)] + files)
    return (
        f"节点 '{node_id}' 代码已锚定至 {shown}，影子快照 {tag} 已生成，实现进度记为 built。\n"
        f"验证程度仍为 {VERIFY_TEXT[_node_state(node)['verify']['level']]}；"
        "测过后用 arch set-node-state --verify unit|api|device 记录。"
    )


def op_sync_manual(node_id: str, summary: str, scope: str = "") -> str:
    tx = GraphTx()
    node = tx.container(scope).get(node_id, allow_super=False)

    node["spec"] = f"{node['spec']} | [人工微调: {summary.strip()}]"
    tag = f"manual-{node_id}-s{tx.bump():02d}"
    node["last_tag"] = tag
    paths = [str(ARCH_DIR)] + _node_files(node)
    tx.commit(tag, f"sync-manual {node_id}: {summary[:40]}", paths=paths)
    return f"已同步人工修改摘要并更新快照 {tag}。"


def op_reset_node(node_id: str, reason: str, scope: str = "") -> str:
    tx = GraphTx()
    node = tx.container(scope).get(node_id, allow_super=False)

    old_label = node["label"]
    rec = _load_records(for_write=True) if _records_enabled() else None
    node["status"] = "hole"
    node["label"] = f"? 待替换: {old_label}"
    node["last_tag"] = ""
    # The replacement scheme starts from scratch; references and files stay.
    node["impl"] = "planned"
    node["verify"] = {"level": "none", "date": ""}

    def write_graveyard() -> None:
        # Runs only after the graph change passed validation.
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

    tag = f"reset-{node_id}-s{tx.bump():02d}"
    tx.commit(tag, f"reset {node_id}: {reason[:40]}", before_write=write_graveyard)
    return f"已将节点 '{node_id}' 重置为 '?' 待定状态并记录进踩坑备忘。\n\n" + op_focus(node_id, scope=scope)


def op_fork(from_tag: str) -> str:
    _init_shadow_git()
    # Records & project preset are knowledge/policy, not graph branch state: they survive a fork.
    kept = (GRAVEYARD_FILE, GROUND_TRUTH_FILE, OPEN_ITEMS_FILE, RECORDS_FILE, CONFIG_FILE)
    backups = {p: p.read_text(encoding="utf-8") for p in kept if p.exists()}

    # Non-state `.arch/` files (e.g. dashboard.html) tracked by older snapshots
    # are not graph state: keep the current copies, and clear them out of the
    # way first so a newer export never blocks the checkout.
    head_extras = set(_tracked_arch_extras("HEAD"))
    extras = head_extras | set(_tracked_arch_extras(from_tag))
    extra_backups = {p: Path(p).read_bytes() for p in extras if Path(p).is_file()}

    def restore_extras() -> None:
        for p in extras:
            if p in extra_backups:
                Path(p).parent.mkdir(parents=True, exist_ok=True)
                Path(p).write_bytes(extra_backups[p])
            elif Path(p).is_file():
                Path(p).unlink()

    for p in extras:
        if p in head_extras:
            _run_shadow_git(["checkout", "HEAD", "--", p], check=False)
        elif Path(p).is_file():
            Path(p).unlink()

    branch_name = f"fork-{from_tag}-{datetime.now().strftime('%H%M%S')}"
    res = _run_shadow_git(["checkout", "-b", branch_name, from_tag], check=False)
    if res.returncode != 0:
        restore_extras()
        raise ArchEngineError(f"无法从快照 '{from_tag}' 分叉：{res.stderr.strip()}")

    restore_extras()
    for path, text in backups.items():
        if text:
            path.write_text(text, encoding="utf-8")
    _shadow_commit(f"{branch_name}-start", f"fork from {from_tag}")
    return f"已成功安全回滚至 {from_tag} 并开启新分支 {branch_name}（已保留所有避坑记录与项目预设）。\n\n" + op_focus()


def op_deactivate_node(
    node_id: str,
    reason: str = "",
    bypass_contract: str = "",
    scope: str = "",
) -> str:
    """Move a node to fog.

    Simple chains are bridged (upstream joined straight to downstream). When a
    bridge would distort the structure (a star hub, bidirectional links, or more
    bypass edges than `auto_bridge_limit`), a blank hole takes the node's place
    and keeps all of its edges instead, so the gap stays visible.
    """
    tx = GraphTx()
    cfg = tx.cfg
    c = tx.container(scope)
    node = c.get(node_id, allow_super=False)
    c_edges = c.edges

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
    # The hub may be implicit (no role, picked by degree), so ask the star itself.
    is_hub = node.get("role") == "hub" or (
        _container_topology(c.raw) == "star" and _star_hub_id(c.raw) == node_id
    )
    slot_reasons: List[str] = []
    if is_hub and (bridge_pairs or len(bidir_edges) >= 2):
        slot_reasons.append("它是星形中枢，缝合会把外围节点两两连成网状")
    if len(bidir_edges) >= 2:
        slot_reasons.append(f"它有 {len(bidir_edges)} 条双向连线，双向边无法自动缝合")
    if len(bridge_pairs) > bridge_limit:
        slot_reasons.append(f"需要 {len(bridge_pairs)} 条旁路缝合边，超过 preset 上限 ({bridge_limit})")

    old_status = node["status"]
    node["status"] = "fog"
    node["last_tag"] = ""

    bridged_edges: List[str] = []
    slot: Optional[Dict[str, Any]] = None
    hole_warning = ""
    if slot_reasons:
        existing_ids = tx.all_ids()
        slot_id, i = f"{node_id}_slot", 2
        while slot_id in existing_ids:
            slot_id, i = f"{node_id}_slot{i}", i + 1
        base_label = node["label"].lstrip("? ").strip() or node_id
        slot = {
            "id": slot_id,
            "label": f"? 待定：{base_label}",
            "status": "hole",
            "type": node.get("type", "port"),
            "spec": f"占位：原方案 '{node_id}' 已移入储备区，此位置待重新设计。原职责：{node.get('spec', '')}",
            "file_path": "",
            "last_tag": "",
        }
        for key in ("stage", "role"):
            if node.get(key):
                slot[key] = node[key]
        max_holes = int(cfg.get("max_active_holes", 3))
        if sum(1 for n in c.nodes if n["status"] == "hole") >= max_holes:
            hole_warning = (
                f"\n[CIRCUIT_BREAKER_WARNING] 占位后未决空洞已超过上限 ({max_holes})，请优先收敛现有空洞。"
            )
        c.insert_node(slot, c.index(node_id) + 1)
        tx.replace_node_refs(node_id, slot_id)
    else:
        c.set_edges([e for e in c_edges if e["from"] != node_id and e["to"] != node_id])
        for inc, out in bridge_pairs:
            u, v = inc["from"], out["to"]
            ct = bypass_contract or inc.get("contract") or out.get("contract") or "Bypass-Bridged"
            inc_fb = _is_feedback(inc)
            out_fb = _is_feedback(out)
            # Join the original endpoints on both sides, so a bypass touching a
            # folded module still knows which child it really starts or ends at.
            # Preserve feedback semantics whenever either incident edge is feedback.
            bypass = []
            for a in _edge_members(inc):
                for b in _edge_members(out):
                    if a["from"] == b["to"]:
                        continue
                    item: Dict[str, Any] = {"from": a["from"], "to": b["to"], "contract": ct}
                    if inc_fb or out_fb or _is_feedback(a) or _is_feedback(b):
                        item["kind"] = "feedback"
                    bypass.append(item)
            # A bypass next to a bidirectional link joins it (set_edges merges).
            if bypass and c.add_flows(u, v, bypass):
                bridged_edges.append(f"{u} -> {v}")

    tag = f"deactivate-{node_id}-s{tx.bump():02d}"
    msg = f"deactivate {node_id} ({old_status} -> fog)"
    if slot:
        msg += f", slot {slot['id']}"
    if reason:
        msg += f": {reason[:30]}"
    tx.commit(tag, msg)

    info = f"已将节点 '{node_id}' 移入战争迷雾区 (fog)。"
    if slot:
        info += (
            f"\n不适合自动缝合旁路（{'；'.join(slot_reasons)}），"
            f"已在原位置留下空白节点 '{slot['id']}' 占位，原有连线全部转到它身上。"
            f"\nJIT 指令：与用户一起重新设计这个位置，确定后对 '{slot['id']}' 执行 collapse。"
            + hole_warning
        )
        return info + "\n\n" + op_focus(slot["id"], scope=scope)
    if bridged_edges:
        info += f"\n已自动缝合主干旁路：{', '.join(bridged_edges)}。"
    if bidir_edges:
        dropped = [f"{e['from']} <-> {e['to']}" for e in bidir_edges]
        info += f"\n已移除双向连线：{', '.join(dropped)}。"
    return info + "\n\n" + op_focus(scope=scope)


def op_activate_node(
    node_id: str,
    after_node: str = "",
    before_node: str = "",
    contract: str = "TBD",
    edge_kind: str = "",
    scope: str = "",
) -> str:
    ekind = _validate_edge_kind(edge_kind) if edge_kind else ""
    tx = GraphTx()
    cfg = tx.cfg
    if ekind == "feedback" and not cfg.get("allow_feedback", True):
        raise ArchEngineError(
            "当前项目 preset 禁止回流边 (allow_feedback=false)。请先通过 arch config --allow-feedback true 放开。"
        )

    c = tx.container(scope)
    node = c.get(node_id, allow_super=True)

    if node["status"] != "fog":
        return f"节点 '{node_id}' 当前状态为 '{node['status']}'，并非处于迷雾区。\n\n" + op_focus(node_id, scope=scope)

    max_holes = int(cfg.get("max_active_holes", 3))
    holes = [n for n in c.nodes if n["status"] == "hole"]
    if len(holes) >= max_holes:
        raise ArchEngineError(
            f"[CircuitBreakerError] 未决空洞已达上限 ({max_holes})！"
            "JIT 指令：必须先通过 collapse 收敛现有空洞，才能激活新的迷雾节点。"
        )
    for ref in (after_node, before_node):
        if ref:
            c.get(ref)

    node["status"] = "hole"
    if not node["label"].startswith("?"):
        node["label"] = f"? {node['label']}"

    c.wire_inserted(node_id, after_node, before_node, contract, ekind)

    tag = f"activate-{node_id}-s{tx.bump():02d}"
    tx.commit(tag, f"activate {node_id} (fog -> hole)")
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
    tx = GraphTx()
    node = tx.find_node(node_id)
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
    tag = f"state-{node_id}-s{tx.bump():02d}"
    tx.commit(tag, f"set-node-state {node_id}")
    st = _node_state(node)
    lines = [f"已更新节点 '{node_id}' 的状态，快照 {tag}：{_state_text(node)}"]
    if st["files"]:
        lines.append(f"实现文件：{', '.join(st['files'])}")
    if st["refs"]:
        lines.append(f"引用：{', '.join(st['refs'])}")
    return "\n".join(lines)
