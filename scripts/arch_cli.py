#!/usr/bin/env python3
"""
arch_cli.py - Zero-dependency State Machine, 1-Hop Slicer & Shadow Git Guardrail
for Autoregressive Architecture Co-Creation.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ARCH_DIR = Path(".arch")
GRAPH_FILE = ARCH_DIR / "graph.json"
SEED_FILE = ARCH_DIR / "SEED.md"
GROUND_TRUTH_FILE = ARCH_DIR / "ground_truth.md"
GRAVEYARD_FILE = ARCH_DIR / "graveyard.md"
GROUNDING_FILE = ARCH_DIR / "grounding.json"
SHADOW_GIT_DIR = ARCH_DIR / ".git"

MAX_ACTIVE_HOLES = 3
MAX_UNFOLDED_SOLID = 7

# A changed diff line that declares a function / type, across common languages
# (Python, JS/TS, Go, Rust, C#/Java-style modifiers). Anchored at the start of the
# line so keywords inside strings or comments do not count.
SIGNATURE_LINE = re.compile(
    r"^[+-]\s*"
    r"(?:(?:export|default|pub(?:\([^)]*\))?|public|private|protected|internal|static|abstract|async|unsafe)\s+)*"
    r"(?:def|class|function|interface|fn|func|struct|trait|enum|type|impl)\b"
)


class ArchEngineError(Exception):
    """Domain error carrying JIT instructions for the Agent."""
    pass


def _run_shadow_git(args: List[str], check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GIT_DIR"] = str(SHADOW_GIT_DIR.resolve())
    env["GIT_WORK_TREE"] = str(Path(".").resolve())
    return subprocess.run(
        ["git"] + args,
        env=env,
        capture_output=True,
        text=True,
        check=check,
    )


def _init_shadow_git() -> None:
    if not SHADOW_GIT_DIR.exists():
        SHADOW_GIT_DIR.mkdir(parents=True, exist_ok=True)
        _run_shadow_git(["init"])
        _run_shadow_git(["config", "user.name", "arch-grower"])
        _run_shadow_git(["config", "user.email", "arch@local"])


def _shadow_commit(tag_name: str, message: str, paths: Optional[List[str]] = None) -> None:
    _init_shadow_git()
    targets = paths if paths else [str(ARCH_DIR)]
    for p in targets:
        if Path(p).exists():
            _run_shadow_git(["add", "-f", p], check=False)
    res = _run_shadow_git(["diff", "--cached", "--quiet"], check=False)
    if res.returncode != 0:
        _run_shadow_git(["commit", "-m", message], check=False)
    _run_shadow_git(["tag", "-f", tag_name], check=False)


def _load_graph() -> Dict[str, Any]:
    if not GRAPH_FILE.exists():
        raise ArchEngineError(
            "未找到 .arch/graph.json。请先调用 arch_init 初始化项目骨架。"
        )
    return json.loads(GRAPH_FILE.read_text(encoding="utf-8"))


def _save_graph(data: Dict[str, Any]) -> None:
    GRAPH_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
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


def _find_walking_skeleton(graph: Dict[str, Any]) -> Optional[List[str]]:
    """BFS for a path from a real entry to a real exit made only of solid nodes.

    Entries and exits are judged on the whole active graph (fog excluded), not on
    the solid sub-graph alone; otherwise a solid node whose successor is still a
    hole would wrongly count as an exit.
    """
    nodes = {n["id"]: n for n in graph["nodes"]}
    adj, in_deg, out_deg = _active_topology(graph)
    solid_ids = {nid for nid in adj if nodes[nid]["status"] in SOLID_STATUSES}
    if not solid_ids:
        return None

    starts = [nid for nid in adj if in_deg[nid] == 0 and nid in solid_ids]
    ends = {nid for nid in adj if out_deg[nid] == 0 and nid in solid_ids}
    if not starts or not ends:
        return None

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
    file_path = node.get("file_path")
    last_tag = node.get("last_tag")
    if not file_path or not last_tag or not Path(file_path).exists() or not SHADOW_GIT_DIR.exists():
        return

    diff_res = _run_shadow_git(["diff", last_tag, "--", file_path], check=False)
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


# =====================================================================
# Core Operations (Shared by CLI and MCP Server)
# =====================================================================

def op_init(seed: str) -> str:
    ARCH_DIR.mkdir(parents=True, exist_ok=True)
    _init_shadow_git()

    SEED_FILE.write_text(f"# 项目初始意图 (SEED)\n\n{seed.strip()}\n", encoding="utf-8")
    if not GROUND_TRUTH_FILE.exists():
        GROUND_TRUTH_FILE.write_text("# 已验证现实事实与硬约束 (Ground Truth)\n\n", encoding="utf-8")
    if not GRAVEYARD_FILE.exists():
        GRAVEYARD_FILE.write_text("# 废弃分支与踩坑记录 (Graveyard)\n\n", encoding="utf-8")
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


def op_focus(node_id: Optional[str] = None) -> str:
    graph = _load_graph()
    nodes = {n["id"]: n for n in graph["nodes"]}

    holes = [n for n in graph["nodes"] if n["status"] == "hole"]
    solids = [n for n in graph["nodes"] if n["status"] == "solid"]

    target = None
    if node_id and node_id in nodes:
        target = nodes[node_id]
    elif holes:
        # Cognitive serialization: settle the most upstream hole first.
        hole_ids = {n["id"] for n in holes}
        target = next(
            (nodes[nid] for nid in _topological_order(graph) if nid in hole_ids),
            holes[0],
        )
    elif solids:
        target = solids[-1]

    seed_txt = SEED_FILE.read_text(encoding="utf-8").strip() if SEED_FILE.exists() else ""
    gt_txt = GROUND_TRUTH_FILE.read_text(encoding="utf-8").strip() if GROUND_TRUTH_FILE.exists() else ""
    gy_txt = GRAVEYARD_FILE.read_text(encoding="utf-8").strip() if GRAVEYARD_FILE.exists() else ""

    out: List[str] = [
        "=== 北极星锚点与硬约束 ===",
        seed_txt,
        gt_txt,
        gy_txt,
        "========================",
    ]

    if len(holes) > MAX_ACTIVE_HOLES:
        out.append(
            f"[CIRCUIT_BREAKER_WARNING] 当前未决空洞数 ({len(holes)}) 已超过上限 ({MAX_ACTIVE_HOLES})！"
            "请先收敛现有主干空洞，禁止继续发散新分支。"
        )
    if len(solids) > MAX_UNFOLDED_SOLID:
        out.append(
            f"[CIRCUIT_BREAKER_WARNING] 当前活跃实心节点 ({len(solids)}) 超过 {MAX_UNFOLDED_SOLID} 个，"
            "建议先将已稳定的子图模块折叠以降低认知负荷。"
        )

    skeleton = _find_walking_skeleton(graph)
    if skeleton:
        out.append(f"[WALKING_SKELETON_READY] 检测到无空洞可运行主干: {' -> '.join(skeleton)}")

    if not target:
        out.append("当前图为空。")
        return "\n".join(out)

    tid = target["id"]
    in_edges = [e for e in graph["edges"] if e["to"] == tid]
    out_edges = [e for e in graph["edges"] if e["from"] == tid]
    neighbor_ids = {e["from"] for e in in_edges} | {e["to"] for e in out_edges}
    fog_nodes = [n for n in graph["nodes"] if n["status"] == "fog"]

    slice_payload = {
        "current_step": graph["step"],
        "focus_node": target,
        "upstream_edges": in_edges,
        "downstream_edges": out_edges,
        "neighbors": [nodes[nid] for nid in neighbor_ids if nid in nodes],
        "fog_backlog": [{"id": f["id"], "label": f["label"]} for f in fog_nodes],
    }
    out.append("=== 当前焦点 1-Hop 切片 ===")
    out.append(json.dumps(slice_payload, indent=2, ensure_ascii=False))
    return "\n".join(out)


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
    graph = _load_graph()
    nodes = {n["id"]: n for n in graph["nodes"]}
    if node_id not in nodes:
        raise ArchEngineError(f"节点 '{node_id}' 不存在。")

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
    graph = _load_graph()
    if any(n["id"] == node_id for n in graph["nodes"]):
        raise ArchEngineError(f"节点 '{node_id}' 已存在。")

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


def op_verify(fact: str, constraint: str) -> str:
    ARCH_DIR.mkdir(parents=True, exist_ok=True)
    if not GROUND_TRUTH_FILE.exists():
        GROUND_TRUTH_FILE.write_text("# 已验证现实事实与硬约束 (Ground Truth)\n\n", encoding="utf-8")

    existing = GROUND_TRUTH_FILE.read_text(encoding="utf-8")
    idx = existing.count("- [GT-") + 1
    entry = f"- [GT-{idx:02d}] (已验证) {fact.strip()} -> 架构约束：{constraint.strip()}\n"
    GROUND_TRUTH_FILE.write_text(existing + entry, encoding="utf-8")
    _shadow_commit(f"gt-{idx:02d}", f"verify GT-{idx:02d}")
    return f"已永久沉淀现实事实：\n{entry}"


def op_ground(source_type: str, source: str, note: str = "") -> str:
    ARCH_DIR.mkdir(parents=True, exist_ok=True)
    data = json.loads(GROUNDING_FILE.read_text(encoding="utf-8")) if GROUNDING_FILE.exists() else {"sources": []}
    data["sources"].append({
        "type": source_type,
        "source": source,
        "note": note,
        "added_at": datetime.now().isoformat(timespec="seconds"),
    })
    GROUNDING_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return f"已挂载 Grounding 数据源 ({source_type}): {source}"


def op_export_task(node_id: str) -> str:
    graph = _load_graph()
    nodes = {n["id"]: n for n in graph["nodes"]}
    if node_id not in nodes:
        raise ArchEngineError(f"节点 '{node_id}' 不存在。")

    node = nodes[node_id]
    if node["status"] == "hole":
        raise ArchEngineError(f"节点 '{node_id}' 仍处于 '?' 待定状态，请先通过 collapse 确定节点规范后再导出编码任务。")

    _check_node_drift(node)

    in_edges = [e for e in graph["edges"] if e["to"] == node_id]
    out_edges = [e for e in graph["edges"] if e["from"] == node_id]
    gt_txt = GROUND_TRUTH_FILE.read_text(encoding="utf-8").strip() if GROUND_TRUTH_FILE.exists() else ""
    gy_txt = GRAVEYARD_FILE.read_text(encoding="utf-8").strip() if GRAVEYARD_FILE.exists() else ""

    task = {
        "worker_instruction": (
            "你是独立的实现子代理。严格仅实现以下单节点契约："
            "1) 采用 Port/Adapter 分离易变外部接口；"
            "2) 编写上下游契约校验与单元测试；"
            "3) 若 type=='reality_gate'，生成终端交互式测试桩；"
            "4) 完成后调用 commit-node 提交影子快照，并仅返回 <=3 行结果摘要。"
        ),
        "target_node": node,
        "input_contracts": in_edges,
        "output_contracts": out_edges,
        "global_ground_truth": gt_txt,
        "global_graveyard": gy_txt,
    }
    return json.dumps(task, indent=2, ensure_ascii=False)


def op_commit_node(node_id: str, file_path: str) -> str:
    graph = _load_graph()
    nodes = {n["id"]: n for n in graph["nodes"]}
    if node_id not in nodes:
        raise ArchEngineError(f"节点 '{node_id}' 不存在。")

    graph["step"] += 1
    tag = f"node-{node_id}-s{graph['step']:02d}"
    nodes[node_id]["file_path"] = file_path
    nodes[node_id]["last_tag"] = tag
    _save_graph(graph)
    _shadow_commit(tag, f"implement {node_id} -> {file_path}", paths=[str(ARCH_DIR), file_path])
    return f"节点 '{node_id}' 代码已锚定至 {file_path}，影子快照 {tag} 已生成。"


def op_sync_manual(node_id: str, summary: str) -> str:
    graph = _load_graph()
    nodes = {n["id"]: n for n in graph["nodes"]}
    if node_id not in nodes:
        raise ArchEngineError(f"节点 '{node_id}' 不存在。")

    node = nodes[node_id]
    file_path = node.get("file_path", "")
    node["spec"] = f"{node['spec']} | [人工微调: {summary.strip()}]"
    graph["step"] += 1
    tag = f"manual-{node_id}-s{graph['step']:02d}"
    node["last_tag"] = tag
    _save_graph(graph)
    paths = [str(ARCH_DIR)] + ([file_path] if file_path else [])
    _shadow_commit(tag, f"sync-manual {node_id}: {summary[:40]}", paths=paths)
    return f"已同步人工修改摘要并更新快照 {tag}。"


def op_reset_node(node_id: str, reason: str) -> str:
    graph = _load_graph()
    nodes = {n["id"]: n for n in graph["nodes"]}
    if node_id not in nodes:
        raise ArchEngineError(f"节点 '{node_id}' 不存在。")

    node = nodes[node_id]
    old_label = node["label"]
    node["status"] = "hole"
    node["label"] = f"? 待替换: {old_label}"
    node["last_tag"] = ""

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
    gy_backup = GRAVEYARD_FILE.read_text(encoding="utf-8") if GRAVEYARD_FILE.exists() else ""
    gt_backup = GROUND_TRUTH_FILE.read_text(encoding="utf-8") if GROUND_TRUTH_FILE.exists() else ""

    branch_name = f"fork-{from_tag}-{datetime.now().strftime('%H%M%S')}"
    res = _run_shadow_git(["checkout", "-b", branch_name, from_tag], check=False)
    if res.returncode != 0:
        raise ArchEngineError(f"无法从快照 '{from_tag}' 分叉：{res.stderr.strip()}")

    if gy_backup:
        GRAVEYARD_FILE.write_text(gy_backup, encoding="utf-8")
    if gt_backup:
        GROUND_TRUTH_FILE.write_text(gt_backup, encoding="utf-8")
    _shadow_commit(f"{branch_name}-start", f"fork from {from_tag}")
    return f"已成功安全回滚至 {from_tag} 并开启新分支 {branch_name}（已保留所有避坑记录）。\n\n" + op_focus()


def op_deactivate_node(node_id: str, reason: str = "", bypass_contract: str = "") -> str:
    graph = _load_graph()
    nodes = {n["id"]: n for n in graph["nodes"]}
    if node_id not in nodes:
        raise ArchEngineError(f"节点 '{node_id}' 不存在。")

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
            if u != v and not any(e["from"] == u and e["to"] == v for e in graph["edges"]):
                ct = bypass_contract or inc.get("contract") or out.get("contract") or "Bypass-Bridged"
                graph["edges"].append({"from": u, "to": v, "contract": ct})
                bridged_edges.append(f"{u} -> {v}")

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
    if node_id not in nodes:
        raise ArchEngineError(f"节点 '{node_id}' 不存在。")

    node = nodes[node_id]
    if node["status"] != "fog":
        return f"节点 '{node_id}' 当前状态为 '{node['status']}'，并非处于迷雾区。\n\n" + op_focus(node_id)

    holes = [n for n in graph["nodes"] if n["status"] == "hole"]
    if len(holes) >= MAX_ACTIVE_HOLES:
        raise ArchEngineError(
            f"[CircuitBreakerError] 未决空洞已达上限 ({MAX_ACTIVE_HOLES})！"
            "JIT 指令：必须先通过 collapse 收敛现有空洞，才能激活新的迷雾节点。"
        )

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


def op_export_ui(out_path: str = "") -> str:
    graph = _load_graph()
    target = Path(out_path).resolve() if out_path else (ARCH_DIR / "dashboard.html")
    target.parent.mkdir(parents=True, exist_ok=True)
    seed = SEED_FILE.read_text(encoding="utf-8").strip() if SEED_FILE.exists() else "架构状态机"

    graph_json_str = json.dumps(graph, ensure_ascii=False)
    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{seed[:30]} - 架构阶段泳道看板</title>
  <script src="https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js"></script>
  <style>
    /* Fallback theme for opening the file directly in a browser. :where() keeps
       specificity at zero so a host that injects these variables still wins. */
    :where(:root) {{
      --background: #f8fafc; --foreground: #0f172a; --card: #ffffff;
      --border: #e2e8f0; --muted-foreground: #64748b;
    }}
    @media (prefers-color-scheme: dark) {{
      :where(:root) {{
        --background: #0b1120; --foreground: #e2e8f0; --card: #111827;
        --border: #1f2937; --muted-foreground: #94a3b8;
      }}
    }}
    ::-webkit-scrollbar {{ width: 6px; height: 6px; }}
    ::-webkit-scrollbar-track {{ background: transparent; }}
    ::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 3px; }}
    .stage-glow {{ box-shadow: 0 0 25px -5px rgba(16, 185, 129, 0.08); }}
  </style>
</head>
<body class="bg-transparent text-[var(--foreground)] antialiased select-none p-2 h-screen flex flex-col box-border">
  <div class="flex-1 bg-[var(--card)] border border-[var(--border)] rounded-2xl shadow-2xl flex flex-col overflow-hidden relative">
    <header class="h-14 border-b border-[var(--border)] px-5 flex items-center justify-between bg-[var(--card)]/95 backdrop-blur z-20">
      <div class="flex items-center gap-3">
        <div class="w-8 h-8 rounded-lg bg-emerald-500/10 border border-emerald-500/30 flex items-center justify-center text-emerald-500 font-bold text-sm">AG</div>
        <div>
          <div class="flex items-center gap-2">
            <h1 class="text-sm font-semibold text-[var(--foreground)] truncate max-w-md">{seed[:40]}</h1>
            <span class="px-2 py-0.5 text-xs rounded-full bg-emerald-500/15 text-emerald-500 font-medium border border-emerald-500/30">Step {graph.get('step', 1)}</span>
          </div>
          <p class="text-xs text-[var(--muted-foreground)]">阶段泳道看板 (Stage Swimlane View) · 主干实心 + 阶段迷雾储备</p>
        </div>
      </div>
      <div class="flex items-center gap-4 text-xs">
        <div class="flex items-center gap-3 bg-[var(--background)] px-3 py-1.5 rounded-lg border border-[var(--border)]">
          <span class="flex items-center gap-1.5 text-emerald-500 font-semibold"><span class="w-2.5 h-2.5 rounded-sm bg-emerald-500"></span> 主干 (Trunk)</span>
          <span class="flex items-center gap-1.5 text-amber-500 font-semibold"><span class="w-2.5 h-2.5 rounded-sm bg-amber-500"></span> 空洞 (Hole)</span>
          <span class="flex items-center gap-1.5 text-[var(--muted-foreground)] font-medium"><span class="w-2.5 h-2.5 rounded-sm border border-dashed border-[var(--border)]"></span> 迷雾 (Fog)</span>
        </div>
      </div>
    </header>

    <div class="flex-1 flex overflow-hidden relative">
      <main id="swimlaneContainer" class="flex-1 overflow-x-auto overflow-y-hidden p-6 flex gap-6 items-stretch bg-[var(--background)]/40"></main>
      <aside id="inspector" class="w-84 border-l border-[var(--border)] bg-[var(--card)] flex flex-col z-20 shadow-2xl p-4 overflow-y-auto">
        <h2 class="text-xs font-bold uppercase tracking-wider text-[var(--muted-foreground)] mb-3 flex items-center gap-1.5">契约与规范详情</h2>
        <div id="inspectorContent" class="space-y-4 text-xs"></div>
      </aside>
    </div>
  </div>

  <script>
    const rawGraph = {graph_json_str};
    const stageMap = {{}};
    const defaultStages = ["感知与接入", "安全与网关", "会话与中枢", "传输与分帧", "终端与呈现"];

    const solidTrunk = rawGraph.nodes.filter(n => n.status !== "fog");
    const fogNodes = rawGraph.nodes.filter(n => n.status === "fog");

    solidTrunk.forEach((n, idx) => {{
      const stName = n.stage || (idx < defaultStages.length ? defaultStages[idx] : `阶段 ${{idx + 1}}`);
      if (!stageMap[stName]) {{
        stageMap[stName] = {{ name: stName, trunk: [], fog: [] }};
      }}
      stageMap[stName].trunk.push(n);
    }});

    fogNodes.forEach((f, idx) => {{
      const stName = f.stage || Object.keys(stageMap)[idx % Math.max(1, Object.keys(stageMap).length)] || "扩展储备";
      if (!stageMap[stName]) {{
        stageMap[stName] = {{ name: stName, trunk: [], fog: [] }};
      }}
      stageMap[stName].fog.push(f);
    }});

    const container = document.getElementById("swimlaneContainer");
    const inspectorContent = document.getElementById("inspectorContent");
    let selectedId = solidTrunk.length ? solidTrunk[0].id : (rawGraph.nodes.length ? rawGraph.nodes[0].id : "");

    function render() {{
      container.innerHTML = "";
      let sNum = 1;
      for (const [stName, stData] of Object.entries(stageMap)) {{
        const col = document.createElement("div");
        col.className = "w-80 flex-shrink-0 flex flex-col rounded-2xl border border-[var(--border)] bg-[var(--card)]/80 stage-glow overflow-hidden";
        
        let trunkHtml = stData.trunk.map(t => `
          <div onclick="selectNode('${{t.id}}')" class="p-3 rounded-xl border cursor-pointer transition-all ${{selectedId === t.id ? 'border-emerald-500 ring-2 ring-emerald-500/20 bg-emerald-500/5' : 'border-emerald-500/30 bg-[var(--card)] hover:border-emerald-500'}}">
            <div class="font-bold text-xs text-[var(--foreground)] mb-1">${{t.label}}</div>
            <p class="text-[11px] text-[var(--muted-foreground)] line-clamp-2 leading-relaxed mb-2">${{t.spec || ''}}</p>
            <div class="pt-2 border-t border-[var(--border)]/60 flex items-center justify-between text-[11px] font-mono text-[var(--muted-foreground)]">
              <span>${{t.id}}</span>
              <span class="text-emerald-500 font-semibold">${{t.status.toUpperCase()}}</span>
            </div>
          </div>
        `).join('');

        let fogHtml = stData.fog.map(f => `
          <div onclick="selectNode('${{f.id}}')" class="p-2.5 rounded-xl border border-dashed border-[var(--border)] bg-[var(--background)]/40 hover:bg-[var(--background)] cursor-pointer transition-all ${{selectedId === f.id ? 'border-amber-500 ring-2 ring-amber-500/20' : ''}}">
            <div class="flex items-center justify-between mb-1">
              <span class="text-[10px] text-[var(--muted-foreground)] font-mono">⚪ FOG</span>
            </div>
            <div class="font-semibold text-xs text-[var(--foreground)] mb-1">${{f.label}}</div>
            <p class="text-[11px] text-[var(--muted-foreground)] line-clamp-2 leading-relaxed">${{f.spec || ''}}</p>
          </div>
        `).join('');

        col.innerHTML = `
          <div class="p-3.5 border-b border-[var(--border)] bg-[var(--background)]/30">
            <span class="text-[11px] font-mono font-bold text-emerald-500">STAGE 0${{sNum++}}</span>
            <h3 class="text-sm font-bold text-[var(--foreground)] truncate">${{stName}}</h3>
          </div>
          <div class="flex-1 p-3 space-y-4 overflow-y-auto">
            <div>
              <div class="text-[11px] font-semibold text-emerald-500 mb-2">主干选定 (Trunk)</div>
              <div class="space-y-2">${{trunkHtml || '<div class="text-[11px] text-[var(--muted-foreground)]">待决空洞</div>'}}</div>
            </div>
            ${{stData.fog.length ? `
            <div class="pt-2 border-t border-dashed border-[var(--border)]">
              <div class="text-[11px] font-semibold text-[var(--muted-foreground)] mb-2">同阶段迷雾储备 (Fog)</div>
              <div class="space-y-2">${{fogHtml}}</div>
            </div>` : ''}}
          </div>
        `;
        container.appendChild(col);
      }}
      showInspector();
    }}

    window.selectNode = function(nid) {{
      selectedId = nid;
      render();
    }};

    function showInspector() {{
      const n = rawGraph.nodes.find(x => x.id === selectedId);
      if (!n) return;
      const inEdges = rawGraph.edges.filter(e => e.to === n.id);
      const outEdges = rawGraph.edges.filter(e => e.from === n.id);
      
      inspectorContent.innerHTML = `
        <div class="space-y-4">
          <div>
            <span class="text-xs font-mono text-[var(--muted-foreground)]">ID: ${{n.id}}</span>
            <h3 class="text-sm font-bold text-[var(--foreground)] mt-0.5">${{n.label}}</h3>
          </div>
          <div class="p-3 bg-[var(--background)] border border-[var(--border)] rounded-xl space-y-1">
            <div class="font-semibold text-xs text-[var(--foreground)] flex justify-between">
              <span>状态属性</span>
              <span class="text-emerald-500 font-mono">${{n.status.toUpperCase()}}</span>
            </div>
            <p class="text-xs text-[var(--muted-foreground)] leading-relaxed">${{n.spec || '无描述'}}</p>
          </div>
          <div class="space-y-1">
            <span class="text-xs font-semibold text-[var(--muted-foreground)]">📥 输入契约 (In)</span>
            <div class="p-2 bg-[var(--background)] border border-[var(--border)] rounded-lg font-mono text-xs text-emerald-400 break-all">
              ${{inEdges.map(e => e.contract).join('; ') || '无 / 起始边界'}}
            </div>
          </div>
          <div class="space-y-1">
            <span class="text-xs font-semibold text-[var(--muted-foreground)]">📤 输出契约 (Out)</span>
            <div class="p-2 bg-[var(--background)] border border-[var(--border)] rounded-lg font-mono text-xs text-amber-400 break-all">
              ${{outEdges.map(e => e.contract).join('; ') || '无 / 最终交付'}}
            </div>
          </div>
          ${{n.file_path ? `
          <div class="space-y-1">
            <span class="text-xs font-semibold text-[var(--muted-foreground)]">📁 映射源码</span>
            <div class="p-2 bg-[var(--background)] border border-[var(--border)] rounded-lg font-mono text-xs break-all">${{n.file_path}}</div>
          </div>` : ''}}
        </div>
      `;
    }}

    render();
  </script>
</body>
</html>"""
    target.write_text(html_content, encoding="utf-8")
    return f"已成功导出阶段泳道架构看板 HTML 至：{target}"


# =====================================================================
# CLI Entrypoint
# =====================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="Autoregressive Architecture State Machine CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init")
    p_init.add_argument("--seed", required=True)

    p_focus = sub.add_parser("focus")
    p_focus.add_argument("--node", default=None)

    p_col = sub.add_parser("collapse")
    p_col.add_argument("--node", required=True)
    p_col.add_argument("--label", required=True)
    p_col.add_argument("--spec", required=True)
    p_col.add_argument("--file-path", default="")
    p_col.add_argument("--in-contract", default="")
    p_col.add_argument("--out-contract", default="")
    p_col.add_argument("--type", default="port")
    p_col.add_argument("--stage", default="")

    p_add = sub.add_parser("add-node")
    p_add.add_argument("--node", required=True)
    p_add.add_argument("--label", required=True)
    p_add.add_argument("--spec", required=True)
    p_add.add_argument("--status", default="hole", choices=["hole", "solid", "fog"])
    p_add.add_argument("--type", default="port")
    p_add.add_argument("--stage", default="")
    p_add.add_argument("--after", default="")
    p_add.add_argument("--before", default="")
    p_add.add_argument("--contract", default="TBD")

    p_exp_ui = sub.add_parser("export-ui", help="导出 Stage 泳道交互式架构看板 HTML")
    p_exp_ui.add_argument("--out", default="")

    p_deact = sub.add_parser("deactivate", help="将活跃节点下线并移入战争迷雾区，自动缝合上下游边")
    p_deact.add_argument("--node", required=True)
    p_deact.add_argument("--reason", default="")
    p_deact.add_argument("--bypass-contract", default="")

    p_act = sub.add_parser("activate", help="将迷雾区节点激活为待定空洞并接入主干")
    p_act.add_argument("--node", required=True)
    p_act.add_argument("--after", default="")
    p_act.add_argument("--before", default="")
    p_act.add_argument("--contract", default="TBD")

    p_ver = sub.add_parser("verify")
    p_ver.add_argument("--fact", required=True)
    p_ver.add_argument("--constraint", required=True)

    p_gnd = sub.add_parser("ground")
    p_gnd.add_argument("--type", required=True, choices=["human_verify", "doc_ref", "probe_cmd", "code_ref"])
    p_gnd.add_argument("--source", required=True)
    p_gnd.add_argument("--note", default="")

    p_exp = sub.add_parser("export-task")
    p_exp.add_argument("--node", required=True)

    p_com = sub.add_parser("commit-node")
    p_com.add_argument("--node", required=True)
    p_com.add_argument("--file-path", required=True)

    p_syn = sub.add_parser("sync-manual")
    p_syn.add_argument("--node", required=True)
    p_syn.add_argument("--summary", required=True)

    p_rst = sub.add_parser("reset-node")
    p_rst.add_argument("--node", required=True)
    p_rst.add_argument("--reason", required=True)

    p_frk = sub.add_parser("fork")
    p_frk.add_argument("--from-tag", required=True)

    args = parser.parse_args()
    try:
        if args.cmd == "init":
            print(op_init(args.seed))
        elif args.cmd == "focus":
            print(op_focus(args.node))
        elif args.cmd == "collapse":
            print(op_collapse(
                args.node, args.label, args.spec,
                args.file_path, args.in_contract, args.out_contract, args.type, args.stage
            ))
        elif args.cmd == "add-node":
            print(op_add_node(
                args.node, args.label, args.spec,
                args.status, args.type, args.stage, args.after, args.before, args.contract
            ))
        elif args.cmd == "export-ui":
            print(op_export_ui(args.out))
        elif args.cmd == "deactivate":
            print(op_deactivate_node(args.node, args.reason, args.bypass_contract))
        elif args.cmd == "activate":
            print(op_activate_node(args.node, args.after, args.before, args.contract))
        elif args.cmd == "verify":
            print(op_verify(args.fact, args.constraint))
        elif args.cmd == "ground":
            print(op_ground(args.type, args.source, args.note))
        elif args.cmd == "export-task":
            print(op_export_task(args.node))
        elif args.cmd == "commit-node":
            print(op_commit_node(args.node, args.file_path))
        elif args.cmd == "sync-manual":
            print(op_sync_manual(args.node, args.summary))
        elif args.cmd == "reset-node":
            print(op_reset_node(args.node, args.reason))
        elif args.cmd == "fork":
            print(op_fork(args.from_tag))
    except ArchEngineError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
