#!/usr/bin/env python3
"""
arch_cli.py - Zero-dependency State Machine, 1-Hop Slicer & Shadow Git Guardrail
for Autoregressive Architecture Co-Creation.
"""

import argparse
import html
import json
import os
import re
import subprocess
import sys
import textwrap
from collections import Counter, deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

ARCH_DIR = Path(".arch")
GRAPH_FILE = ARCH_DIR / "graph.json"
SEED_FILE = ARCH_DIR / "SEED.md"
GROUND_TRUTH_FILE = ARCH_DIR / "ground_truth.md"
GRAVEYARD_FILE = ARCH_DIR / "graveyard.md"
OPEN_ITEMS_FILE = ARCH_DIR / "open_items.md"
RECORDS_FILE = ARCH_DIR / "records.json"
GROUNDING_FILE = ARCH_DIR / "grounding.json"
SHADOW_GIT_DIR = ARCH_DIR / ".git"
DASHBOARD_TEMPLATE = Path(__file__).resolve().parent / "dashboard_template.html"

MAX_ACTIVE_HOLES = 3
MAX_UNFOLDED_SOLID = 7
DEFAULT_GLOBAL_FACT_LIMIT = 3000   # characters of active `global` facts before warning
REF_WHOLE_FILE_LIMIT = 4000        # a ref without '#section' is inlined only when this small
REF_SECTION_LIMIT = 20000          # longest inlined ref section in an export-task

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


# =====================================================================
# Node state: implementation progress, verification depth, references
# =====================================================================
#
# `status` (hole / solid / fog / super_node) says whether a decision is made.
# The fields below say how far the decided node has got in reality. Graphs
# written before they existed have none of them; _node_state fills defaults.

IMPL_LEVELS = ("planned", "building", "built")
VERIFY_LEVELS = ("none", "unit", "api", "device")
IMPL_TEXT = {"planned": "未实现", "building": "实现中", "built": "已实现"}
VERIFY_TEXT = {"none": "未验证", "unit": "单测", "api": "接口实测", "device": "真机实测"}


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


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


# =====================================================================
# Typed records: facts, graveyard, open items, legacy (.arch/records.json)
# =====================================================================
#
# records.json is the source of truth once a project is migrated; the md files
# are rendered from it for people and Git diffs. A project without records.json
# keeps the original behaviour (md files appended to and concatenated whole).

GENERATED_MARKER = (
    "<!-- arch-grower：本文件由 .arch/records.json 生成，请勿手改；"
    "用 arch verify / open / rescope / legacy 等命令维护 -->"
)
GT_TITLE = "# 已验证现实事实与硬约束 (Ground Truth)"
GY_TITLE = "# 废弃分支与踩坑记录 (Graveyard)"
OPEN_TITLE = "# 待办项 (Open Items)"
DEFAULT_TITLES = {GT_TITLE, GY_TITLE, OPEN_TITLE, "# 踩坑记录"}
RECORD_LISTS = ("facts", "graveyard", "open_items", "legacy")

OPEN_KINDS = ("device_test", "research", "decision")
OPEN_OWNERS = ("user", "agent")
KIND_TEXT = {"device_test": "真机实测", "research": "调研", "decision": "待决策"}
OWNER_TEXT = {"user": "用户处理", "agent": "代理处理"}

NOT_MIGRATED_HINT = (
    "[RECORDS_NOT_MIGRATED] 本项目的 ground_truth.md / graveyard.md 还是旧格式，focus 与 export-task "
    "会全文拼接。运行 arch migrate-records 可转为结构化记录，之后按节点只带相关内容。"
)

GT_LINE = re.compile(
    r"^[-*]\s+\[(GT-[^\]\s]+)\]\s*(?:[(（]已验证[)）]\s*)?(.*?)\s*->\s*架构约束\s*[：:]\s*(.*)$"
)
GY_LINE = re.compile(r"^[-*]\s+❌\s*\[节点\s+([^\s/\]]+)\s*/\s*(.*)\]\s*废弃原因\s*[：:]\s*(.*)$")
MD_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def _records_enabled() -> bool:
    return RECORDS_FILE.exists()


def _new_records() -> Dict[str, Any]:
    return {
        "version": 1,
        "settings": {"global_fact_char_limit": DEFAULT_GLOBAL_FACT_LIMIT},
        "facts": [],
        "graveyard": [],
        "open_items": [],
        "legacy": [],
    }


def _read_records() -> Dict[str, Any]:
    data = json.loads(RECORDS_FILE.read_text(encoding="utf-8"))
    base = _new_records()
    data.setdefault("version", base["version"])
    settings = data.setdefault("settings", {})
    for key, value in base["settings"].items():
        settings.setdefault(key, value)
    for key in RECORD_LISTS:
        data.setdefault(key, [])
    return data


def _load_records(for_write: bool = False) -> Dict[str, Any]:
    if not _records_enabled():
        raise ArchEngineError(
            "本项目还没有结构化记录 (.arch/records.json)。\n"
            "JIT 指令：先运行 arch migrate-records（MCP: arch_migrate_records）把现有 md 记录导入，再重试本命令。"
        )
    rec = _read_records()
    if for_write:
        _guard_rendered_md(rec)
    return rec


def _save_records(rec: Dict[str, Any]) -> None:
    RECORDS_FILE.write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for path, text in _rendered_files(rec).items():
        path.write_text(text, encoding="utf-8")


def _next_id(items: List[Dict[str, Any]], prefix: str) -> str:
    pattern = re.compile(rf"^{prefix}-(\d+)$")
    nums = [int(m.group(1)) for m in (pattern.match(str(i.get("id", ""))) for i in items) if m]
    return f"{prefix}-{(max(nums) if nums else 0) + 1:02d}"


def _superseded_ids(rec: Dict[str, Any]) -> Dict[str, str]:
    """Superseded fact id -> id of the fact that replaced it."""
    out: Dict[str, str] = {}
    for f in rec["facts"]:
        for old in f.get("supersedes") or []:
            out.setdefault(old, f["id"])
    return out


def _active_facts(rec: Dict[str, Any]) -> List[Dict[str, Any]]:
    gone = _superseded_ids(rec)
    return [f for f in rec["facts"] if f["id"] not in gone]


def _is_global(record: Dict[str, Any]) -> bool:
    return "global" in (record.get("scope") or ["global"])


def _fact_chars(f: Dict[str, Any]) -> int:
    return len(f.get("text", "")) + len(f.get("constraint", "")) + len(f.get("detail", ""))


def _global_fact_budget(rec: Dict[str, Any]) -> Tuple[int, int]:
    used = sum(_fact_chars(f) for f in _active_facts(rec) if _is_global(f))
    return used, int(rec["settings"].get("global_fact_char_limit") or DEFAULT_GLOBAL_FACT_LIMIT)


def _budget_warning(rec: Dict[str, Any]) -> str:
    used, limit = _global_fact_budget(rec)
    if used <= limit:
        return ""
    return (
        f"[GLOBAL_FACTS_OVER_BUDGET] global 事实共 {used} 字符，超过上限 {limit}（每次 focus 都会带上）。"
        "把只与部分节点或阶段相关的事实改成对应作用域：arch rescope --id GT-xx --scope <节点或阶段>；"
        "已过时的用 arch verify --supersedes 取代。上限可用 arch config --global-fact-limit 调整。"
    )


def _scope_text(scopes: List[str]) -> str:
    return ", ".join(scopes or ["global"])


def _normalize_scopes(raw: Union[None, str, Iterable[str]], graph: Optional[Dict[str, Any]]) -> List[str]:
    if isinstance(raw, str):
        raw = [raw]
    scopes = list(dict.fromkeys(s.strip() for s in (raw or []) if s and s.strip())) or ["global"]
    if "global" in scopes and len(scopes) > 1:
        raise ArchEngineError("作用域 global 不能和节点 / 阶段作用域混用。")
    if graph is not None:
        all_nodes = list(_iter_all_nodes(graph["nodes"]))
        ids = {n["id"] for n in all_nodes}
        stages = {n.get("stage") for n in all_nodes if n.get("stage")}
        bad = [s for s in scopes if s != "global" and s not in ids and s not in stages]
        if bad:
            raise ArchEngineError(
                f"未知作用域：{', '.join(bad)}。作用域只能是 global、节点 ID（含折叠在模块内的子节点）"
                f"或阶段名（节点的 stage）。现有阶段：{', '.join(sorted(stages)) or '无'}。"
            )
    return scopes


def _context_keys(graph: Dict[str, Any], target_id: str, neighbor_ids: Iterable[str] = ()) -> set:
    """Scopes that count as related to a focus target.

    The target, its neighbours, every node folded inside any of them, and the
    target's stage.
    """
    keys = {target_id}
    target = _find_any_node(graph, target_id)
    if target and target.get("stage"):
        keys.add(target["stage"])
    for nid in [target_id, *neighbor_ids]:
        keys.add(nid)
        node = _find_any_node(graph, nid)
        if node:
            keys.update(c["id"] for c in _iter_all_nodes(node.get("children", [])))
    return keys


def _matches(record: Dict[str, Any], keys: set) -> bool:
    return any(s in keys for s in (record.get("scope") or ["global"]))


# ---------------------------------------------------------------- formatting

def _fact_line(f: Dict[str, Any], show_scope: bool = True, superseded_by: str = "") -> str:
    line = f"- [{f['id']}] {f['text']} -> 架构约束：{f['constraint']}"
    meta = []
    if show_scope and not _is_global(f):
        meta.append(f"作用域 {_scope_text(f['scope'])}")
    if f.get("source"):
        meta.append(f"来源 {f['source']}")
    if f.get("supersedes"):
        meta.append(f"取代 {', '.join(f['supersedes'])}")
    if superseded_by:
        meta.append(f"已被 {superseded_by} 取代")
    if meta:
        line += "（" + "；".join(meta) + "）"
    if f.get("detail"):
        line += "\n" + textwrap.indent(f["detail"], "  ")
    return line


def _grave_line(g: Dict[str, Any]) -> str:
    line = f"- ❌ [节点 {g['node']} / {g['scheme']}] 废弃原因：{g['reason']}"
    if g.get("detail"):
        line += "\n" + textwrap.indent(g["detail"], "  ")
    return line


def _open_line(o: Dict[str, Any]) -> str:
    meta = [_scope_text(o["scope"]), KIND_TEXT.get(o["kind"], o["kind"]), OWNER_TEXT.get(o["owner"], o["owner"])]
    if o.get("blocking"):
        meta.append("卡开发")
    line = f"- [{o['id']}] {o['question']}（{' · '.join(meta)}）"
    if o.get("status") == "resolved":
        line += f"\n  → 已解决 {o.get('resolved_at', '')}：{o.get('resolution', '')}".rstrip("：")
    return line


# ----------------------------------------------------------------- rendering

def _render_legacy(rec: Dict[str, Any], source: str) -> List[str]:
    items = [lg for lg in rec["legacy"] if lg.get("source") == source and lg.get("status") != "resolved"]
    if not items:
        return []
    out = ["## 未分类遗留记录（待人工处理：arch legacy list / legacy show / legacy resolve）", ""]
    for lg in items:
        out += [f"### {lg['id']}", "", lg["text"], ""]
    return out


def _render_ground_truth(rec: Dict[str, Any]) -> str:
    lines = [GENERATED_MARKER, GT_TITLE, ""]
    gone = _superseded_ids(rec)
    active = [f for f in rec["facts"] if f["id"] not in gone]
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for f in sorted(active, key=lambda f: not _is_global(f)):
        groups.setdefault(_scope_text(f.get("scope") or ["global"]), []).append(f)
    if not active:
        lines += ["暂无记录。", ""]
    for scope, facts in groups.items():
        lines += [f"## 作用域 · {scope}", ""]
        for f in facts:
            lines.append(f"- [{f['id']}] {f['text']} -> 架构约束：{f['constraint']}")
            meta = [x for x in (
                f"来源：{f['source']}" if f.get("source") else "",
                f"日期：{f['date']}" if f.get("date") else "",
                f"取代：{', '.join(f['supersedes'])}" if f.get("supersedes") else "",
            ) if x]
            if meta:
                lines.append("  - " + " · ".join(meta))
            if f.get("detail"):
                lines.append(textwrap.indent(f["detail"], "  "))
        lines.append("")
    old = [f for f in rec["facts"] if f["id"] in gone]
    if old:
        lines += ["## 已被取代（focus / export-task 默认不展示）", ""]
        for f in old:
            lines.append(f"- [{f['id']}] {f['text']} -> 架构约束：{f['constraint']}")
            lines.append(f"  - 作用域：{_scope_text(f.get('scope'))} · 由 {gone[f['id']]} 取代")
            if f.get("detail"):
                lines.append(textwrap.indent(f["detail"], "  "))
        lines.append("")
    lines += _render_legacy(rec, GROUND_TRUTH_FILE.name)
    return "\n".join(lines).rstrip() + "\n"


def _render_graveyard(rec: Dict[str, Any]) -> str:
    lines = [GENERATED_MARKER, GY_TITLE, ""]
    if not rec["graveyard"]:
        lines += ["暂无记录。", ""]
    for g in rec["graveyard"]:
        lines.append(f"- ❌ [节点 {g['node']} / {g['scheme']}] 废弃原因：{g['reason']}")
        lines.append("  - " + " · ".join(x for x in (g["id"], g.get("date", "")) if x))
        if g.get("detail"):
            lines.append(textwrap.indent(g["detail"], "  "))
    if rec["graveyard"]:
        lines.append("")
    lines += _render_legacy(rec, GRAVEYARD_FILE.name)
    return "\n".join(lines).rstrip() + "\n"


def _render_open_items(rec: Dict[str, Any]) -> str:
    lines = [GENERATED_MARKER, OPEN_TITLE, ""]
    open_items = [o for o in rec["open_items"] if o.get("status") != "resolved"]
    done = [o for o in rec["open_items"] if o.get("status") == "resolved"]
    lines += ["## 未关闭", ""]
    if not open_items:
        lines += ["暂无。", ""]
    for o in sorted(open_items, key=lambda o: not o.get("blocking")):
        lines.append(f"- [{o['id']}] {o['question']}")
        meta = [f"作用域：{_scope_text(o['scope'])}", KIND_TEXT.get(o["kind"], o["kind"]),
                OWNER_TEXT.get(o["owner"], o["owner"])]
        if o.get("blocking"):
            meta.append("卡开发")
        if o.get("created"):
            meta.append(f"登记：{o['created']}")
        lines.append("  - " + " · ".join(meta))
    if open_items:
        lines.append("")
    if done:
        lines += ["## 已解决", ""]
        for o in done:
            lines.append(f"- [{o['id']}] {o['question']}")
            lines.append(f"  - 作用域：{_scope_text(o['scope'])} · 解决：{o.get('resolved_at', '')}")
            lines.append(f"  - 结论：{o.get('resolution', '')}")
        lines.append("")
    lines += _render_legacy(rec, OPEN_ITEMS_FILE.name)
    return "\n".join(lines).rstrip() + "\n"


def _rendered_files(rec: Dict[str, Any]) -> Dict[Path, str]:
    return {
        GROUND_TRUTH_FILE: _render_ground_truth(rec),
        GRAVEYARD_FILE: _render_graveyard(rec),
        OPEN_ITEMS_FILE: _render_open_items(rec),
    }


def _md_is_boilerplate(text: str) -> bool:
    return all(ln.strip() in DEFAULT_TITLES for ln in text.splitlines() if ln.strip())


def _pending_md_edits(rec: Dict[str, Any]) -> List[Path]:
    """Rendered md files whose content is not what records.json renders to."""
    out = []
    for path, expected in _rendered_files(rec).items():
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if text != expected and not _md_is_boilerplate(text):
            out.append(path)
    return out


def _guard_rendered_md(rec: Dict[str, Any]) -> None:
    pending = _pending_md_edits(rec)
    if pending:
        raise ArchEngineError(
            f"{'、'.join(p.name for p in pending)} 里有 records.json 之外的内容（手改或旧格式）。"
            "这些 md 由工具从 .arch/records.json 生成，继续写入会覆盖它们。\n"
            "JIT 指令：先运行 arch migrate-records 导入这些内容，再重试本命令；之后用 CLI / MCP 命令维护记录，不要手改 md。"
        )


# ----------------------------------------------------------------- migration

def _strip_blank_edges(lines: List[str]) -> List[str]:
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def _parse_md_records(text: str) -> Dict[str, List[Dict[str, Any]]]:
    """Split an old-format md file into facts, graveyard entries and legacy blocks.

    Recognised entries take indented lines directly below them as `detail`.
    Everything else is kept verbatim as legacy blocks, split at headings; a
    heading followed straight by entries becomes their `section` instead.
    Only the engine's own default titles and the generated-file marker are
    dropped, so no content is lost.
    """
    facts: List[Dict[str, Any]] = []
    graves: List[Dict[str, Any]] = []
    legacy: List[Dict[str, Any]] = []
    block: List[str] = []
    section = ""
    last: Optional[Dict[str, Any]] = None

    def flush(next_is_entry: bool) -> None:
        lines = _strip_blank_edges(block)
        block.clear()
        if not lines:
            return
        if next_is_entry and len(lines) == 1 and MD_HEADING.match(lines[0]):
            return  # kept as the `section` of the entries that follow
        legacy.append({"text": "\n".join(lines), "section": section})

    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped == GENERATED_MARKER or stripped in DEFAULT_TITLES:
            continue
        if last is not None and stripped and raw[:1] in (" ", "\t"):
            last["_detail"].append(raw)
            continue
        last = None
        fm, gm = GT_LINE.match(raw), GY_LINE.match(raw)
        if fm or gm:
            flush(True)
            if fm:
                last = {"legacy_id": fm.group(1), "text": fm.group(2).strip(),
                        "constraint": fm.group(3).strip(), "section": section, "_detail": []}
                facts.append(last)
            else:
                last = {"node": gm.group(1), "scheme": gm.group(2).strip(),
                        "reason": gm.group(3).strip(), "section": section, "_detail": []}
                graves.append(last)
            continue
        heading = MD_HEADING.match(raw)
        if heading:
            flush(False)
            section = heading.group(2)
        block.append(raw)
    flush(False)

    for entry in facts + graves:
        entry["detail"] = textwrap.dedent("\n".join(entry.pop("_detail"))).strip("\n")
    return {"facts": facts, "graveyard": graves, "legacy": legacy}


def _added_lines(text: str, expected: str) -> str:
    """Lines of `text` that the rendered `expected` does not account for, in order."""
    budget = Counter(expected.splitlines())
    kept = []
    for line in text.splitlines():
        if budget[line] > 0:
            budget[line] -= 1
        else:
            kept.append(line)
    return "\n".join(kept)


def _merge_parsed(rec: Dict[str, Any], parsed: Dict[str, List[Dict[str, Any]]],
                  source: str, stats: Counter) -> None:
    norm = lambda s: " ".join(str(s).split())
    fact_keys = {(norm(f["text"]), norm(f["constraint"]), norm(f.get("detail", ""))) for f in rec["facts"]}
    for f in parsed["facts"]:
        key = (norm(f["text"]), norm(f["constraint"]), norm(f["detail"]))
        if key in fact_keys:
            stats["skipped"] += 1
            continue
        fact_keys.add(key)
        used = {x["id"] for x in rec["facts"]}
        fid = f["legacy_id"] if f["legacy_id"] not in used else _next_id(rec["facts"], "GT")
        entry = {"id": fid, "scope": ["global"], "text": f["text"], "constraint": f["constraint"],
                 "source": "", "date": "", "supersedes": [], "detail": f["detail"], "imported_from": source}
        if fid != f["legacy_id"]:
            entry["legacy_id"] = f["legacy_id"]
        if f["section"]:
            entry["section"] = f["section"]
        rec["facts"].append(entry)
        stats["facts"] += 1

    grave_keys = {(norm(g["node"]), norm(g["scheme"]), norm(g["reason"]), norm(g.get("detail", "")))
                  for g in rec["graveyard"]}
    for g in parsed["graveyard"]:
        key = (norm(g["node"]), norm(g["scheme"]), norm(g["reason"]), norm(g["detail"]))
        if key in grave_keys:
            stats["skipped"] += 1
            continue
        grave_keys.add(key)
        entry = {"id": _next_id(rec["graveyard"], "GY"), "node": g["node"], "scheme": g["scheme"],
                 "reason": g["reason"], "date": "", "detail": g["detail"], "imported_from": source}
        if g["section"]:
            entry["section"] = g["section"]
        rec["graveyard"].append(entry)
        stats["graveyard"] += 1

    legacy_keys = {(lg.get("source"), lg["text"]) for lg in rec["legacy"]}
    for lg in parsed["legacy"]:
        if (source, lg["text"]) in legacy_keys:
            stats["skipped"] += 1
            continue
        legacy_keys.add((source, lg["text"]))
        rec["legacy"].append({"id": _next_id(rec["legacy"], "LG"), "source": source,
                              "section": lg["section"], "text": lg["text"], "status": "open",
                              "note": "", "imported": _today()})
        stats["legacy"] += 1


# ------------------------------------------------------------ ref extraction

def _md_headings(lines: List[str]) -> List[Tuple[int, int, str]]:
    """(line index, level, title) of every heading outside fenced code blocks."""
    out, fence = [], ""
    for i, line in enumerate(lines):
        mark = re.match(r"^\s*(```|~~~)", line)
        if mark:
            fence = "" if fence == mark.group(1) else (fence or mark.group(1))
            continue
        if fence:
            continue
        m = MD_HEADING.match(line)
        if m:
            out.append((i, len(m.group(1)), m.group(2).strip()))
    return out


def _slug(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text.strip().lower())
    return re.sub(r"\s+", "-", text)


def _extract_md_section(text: str, anchor: str) -> Optional[str]:
    """The section a `file.md#anchor` ref points at, heading line included.

    The anchor may be a heading title, a section number (`6`, `6.2`, `§6`)
    or a GitHub-style slug; the first heading matching, in that order, wins.
    """
    lines = text.splitlines()
    heads = _md_headings(lines)
    want = anchor.strip()
    number = re.sub(r"^§\s*", "", want)

    def strip_prefix(title: str) -> str:
        return re.sub(r"^(?:§|第)\s*", "", title)

    tests = [
        lambda t: t.lower() == want.lower(),
        lambda t: bool(re.fullmatch(r"\d+(?:\.\d+)*", number))
        and bool(re.match(rf"{re.escape(number)}(?!\d)(?!\.\d)", strip_prefix(t))),
        lambda t: _slug(t) == _slug(want),
        lambda t: want.lower() in t.lower(),
    ]
    for test in tests:
        for idx, (line_no, level, title) in enumerate(heads):
            if test(title):
                end = next((ln for ln, lv, _ in heads[idx + 1:] if lv <= level), len(lines))
                return "\n".join(lines[line_no:end]).rstrip()
    return None


def _json_pointer(data: Any, anchor: str) -> Any:
    if isinstance(data, dict) and anchor in data:
        return data[anchor]
    cur = data
    for part in anchor.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            raise KeyError(anchor)
    return cur


def _resolve_ref(ref: str) -> Dict[str, Any]:
    path_text, _, anchor = ref.partition("#")
    path = Path(path_text)
    item: Dict[str, Any] = {"ref": ref}
    if not path_text or not path.is_file():
        item["error"] = "文件不存在（路径相对于项目根目录）"
        return item
    suffix = path.suffix.lower()
    try:
        if suffix in (".md", ".markdown"):
            text = path.read_text(encoding="utf-8")
            if not anchor:
                if len(text) > REF_WHOLE_FILE_LIMIT:
                    item["note"] = f"整篇 {len(text)} 字符，未内联；请在引用里加 #章节 或让子代理自行查阅"
                    return item
                content = text.strip()
            else:
                content = _extract_md_section(text, anchor)
                if content is None:
                    titles = [t for _, _, t in _md_headings(text.splitlines())][:30]
                    item["error"] = f"未找到章节 '{anchor}'。可用标题：{' | '.join(titles) or '无'}"
                    return item
        elif suffix == ".json" and anchor:
            value = _json_pointer(json.loads(path.read_text(encoding="utf-8")), anchor)
            content = json.dumps(value, indent=2, ensure_ascii=False)
        else:
            item["note"] = "非 Markdown 章节引用，内容未内联；请子代理按路径自行读取"
            return item
    except (KeyError, ValueError, UnicodeDecodeError) as exc:
        item["error"] = f"无法读取引用：{type(exc).__name__} {exc}"
        return item
    if len(content) > REF_SECTION_LIMIT:
        content = content[:REF_SECTION_LIMIT] + f"\n…（已截断，原文 {len(content)} 字符）"
    item["content"] = content
    return item


# =====================================================================
# Core Operations (Shared by CLI and MCP Server)
# =====================================================================

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


def op_focus(node_id: Optional[str] = None) -> str:
    graph = _load_graph()
    nodes = {n["id"]: n for n in graph["nodes"]}

    holes = [n for n in graph["nodes"] if n["status"] == "hole"]
    solids = [n for n in graph["nodes"] if n["status"] == "solid"]
    settled = [n for n in graph["nodes"] if n["status"] in SOLID_STATUSES]

    target = None
    if node_id:
        target = _get_node(graph, node_id)
    elif holes:
        target = nodes[_next_focus_hole(graph)]
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

    if len(holes) > MAX_ACTIVE_HOLES:
        out.append(
            f"[CIRCUIT_BREAKER_WARNING] 当前未决空洞数 ({len(holes)}) 已超过上限 ({MAX_ACTIVE_HOLES})！"
            "请先收敛现有主干空洞，禁止继续发散新分支。"
        )
    if len(solids) > MAX_UNFOLDED_SOLID:
        out.append(
            f"[CIRCUIT_BREAKER_WARNING] 当前活跃实心节点 ({len(solids)}) 超过 {MAX_UNFOLDED_SOLID} 个，"
            "建议调用 arch_fold 将已稳定的子图折叠为超级节点，以降低认知负荷。"
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
        "focus_node": _node_view(target),
        "upstream_edges": [_public_edge(e) for e in in_edges],
        "downstream_edges": [_public_edge(e) for e in out_edges],
        "neighbors": [_node_view(nodes[nid]) for nid in neighbor_ids if nid in nodes],
        "fog_backlog": [{"id": f["id"], "label": f["label"]} for f in fog_nodes],
    }
    out.append("=== 当前焦点 1-Hop 切片 ===")
    out.append(json.dumps(slice_payload, indent=2, ensure_ascii=False))
    if rec is not None:
        keys = _context_keys(graph, tid, neighbor_ids)
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
        out.append(f"另有 {legacy_open} 条未分类遗留记录待人工处理（arch legacy list）。")
    return out


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


def op_verify(
    fact: str,
    constraint: str,
    scope: Union[None, str, List[str]] = None,
    source: str = "",
    supersedes: Union[None, str, List[str]] = None,
) -> str:
    if isinstance(supersedes, str):
        supersedes = [supersedes]
    supersedes = [s.strip() for s in (supersedes or []) if s and s.strip()]
    if _records_enabled():
        return _verify_record(fact, constraint, scope, source, supersedes)
    if scope or source or supersedes:
        raise ArchEngineError(
            "作用域 / 来源 / 取代关系需要结构化记录。\n"
            "JIT 指令：先运行 arch migrate-records，再带这些参数重试。"
        )

    ARCH_DIR.mkdir(parents=True, exist_ok=True)
    if not GROUND_TRUTH_FILE.exists():
        GROUND_TRUTH_FILE.write_text("# 已验证现实事实与硬约束 (Ground Truth)\n\n", encoding="utf-8")

    existing = GROUND_TRUTH_FILE.read_text(encoding="utf-8")
    idx = existing.count("- [GT-") + 1
    entry = f"- [GT-{idx:02d}] (已验证) {fact.strip()} -> 架构约束：{constraint.strip()}\n"
    GROUND_TRUTH_FILE.write_text(existing + entry, encoding="utf-8")
    _shadow_commit(f"gt-{idx:02d}", f"verify GT-{idx:02d}")
    return f"已永久沉淀现实事实：\n{entry}\n{NOT_MIGRATED_HINT}"


def _graph_if_any() -> Optional[Dict[str, Any]]:
    return _load_graph() if GRAPH_FILE.exists() else None


def _verify_record(fact: str, constraint: str, scope: Union[None, str, List[str]],
                   source: str, supersedes: List[str]) -> str:
    rec = _load_records(for_write=True)
    by_id = {f["id"]: f for f in rec["facts"]}
    missing = [s for s in supersedes if s not in by_id]
    if missing:
        raise ArchEngineError(f"要取代的事实不存在：{', '.join(missing)}。用 arch facts --all 查看现有编号。")
    if not scope and supersedes:
        # A replacement keeps the scope of what it replaces unless told otherwise.
        scope = list(dict.fromkeys(s for old in supersedes for s in by_id[old].get("scope") or ["global"]))
        if "global" in scope:
            scope = ["global"]
    scopes = _normalize_scopes(scope, _graph_if_any())

    fid = _next_id(rec["facts"], "GT")
    entry = {
        "id": fid,
        "scope": scopes,
        "text": fact.strip(),
        "constraint": constraint.strip(),
        "source": source.strip(),
        "date": _today(),
        "supersedes": supersedes,
        "detail": "",
    }
    rec["facts"].append(entry)
    _save_records(rec)
    _shadow_commit(f"gt-{fid[3:]}", f"verify {fid}")
    out = f"已永久沉淀现实事实：\n{_fact_line(entry)}"
    warning = _budget_warning(rec) if "global" in scopes else ""
    return out + (f"\n{warning}" if warning else "")


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
    _get_node(graph, node_id, allow_super=False)

    node = nodes[node_id]
    if node["status"] == "hole":
        raise ArchEngineError(f"节点 '{node_id}' 仍处于 '?' 待定状态，请先通过 collapse 确定节点规范后再导出编码任务。")

    _check_node_drift(node)

    in_edges = [e for e in graph["edges"] if e["to"] == node_id]
    out_edges = [e for e in graph["edges"] if e["from"] == node_id]

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
    if _records_enabled():
        rec = _read_records()
        keys = _context_keys(graph, node_id)
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


def op_open_add(
    question: str,
    kind: str,
    owner: str,
    scope: Union[None, str, List[str]] = None,
    blocking: bool = False,
) -> str:
    if kind not in OPEN_KINDS:
        raise ArchEngineError(f"类型只能是 {' / '.join(OPEN_KINDS)}，收到：{kind}")
    if owner not in OPEN_OWNERS:
        raise ArchEngineError(f"处理方只能是 {' / '.join(OPEN_OWNERS)}，收到：{owner}")
    if not question.strip():
        raise ArchEngineError("待办项需要写明问题。")
    rec = _load_records(for_write=True)
    item = {
        "id": _next_id(rec["open_items"], "OI"),
        "scope": _normalize_scopes(scope, _graph_if_any()),
        "question": question.strip(),
        "kind": kind,
        "owner": owner,
        "blocking": bool(blocking),
        "status": "open",
        "resolution": "",
        "created": _today(),
        "resolved_at": "",
    }
    rec["open_items"].append(item)
    _save_records(rec)
    _shadow_commit(f"open-{item['id']}", f"open add {item['id']}")
    return f"已登记待办项：\n{_open_line(item)}"


def op_open_resolve(item_id: str, resolution: str, constraint: str = "") -> str:
    """Close an open item; with `constraint`, its conclusion also becomes a fact."""
    rec = _load_records(for_write=True)
    item = next((o for o in rec["open_items"] if o["id"] == item_id), None)
    if item is None:
        raise ArchEngineError(f"待办项 '{item_id}' 不存在。用 arch open list --all 查看编号。")
    if item.get("status") == "resolved":
        raise ArchEngineError(f"待办项 '{item_id}' 已在 {item.get('resolved_at', '')} 解决：{item.get('resolution', '')}")
    if not resolution.strip():
        raise ArchEngineError("关闭待办项需要写明结论。")
    item["status"] = "resolved"
    item["resolution"] = resolution.strip()
    item["resolved_at"] = _today()
    out = [f"已关闭待办项：\n{_open_line(item)}"]
    if constraint.strip():
        fact = {
            "id": _next_id(rec["facts"], "GT"),
            "scope": list(item["scope"]),
            "text": resolution.strip(),
            "constraint": constraint.strip(),
            "source": f"待办项 {item_id}",
            "date": _today(),
            "supersedes": [],
            "detail": "",
        }
        rec["facts"].append(fact)
        out.append(f"并沉淀为事实：\n{_fact_line(fact)}")
        if _is_global(fact) and _budget_warning(rec):
            out.append(_budget_warning(rec))
    _save_records(rec)
    _shadow_commit(f"resolve-{item_id}", f"open resolve {item_id}")
    return "\n".join(out)


def op_open_list(
    node_id: str = "",
    scope: str = "",
    include_resolved: bool = False,
    owner: str = "",
    blocking_only: bool = False,
) -> str:
    rec = _load_records()
    items = rec["open_items"]
    if not include_resolved:
        items = [o for o in items if o.get("status") != "resolved"]
    if node_id:
        graph = _load_graph()
        if _find_any_node(graph, node_id) is None:
            raise ArchEngineError(f"节点 '{node_id}' 不存在。")
        keys = _context_keys(graph, node_id)
        items = [o for o in items if _matches(o, keys)]
    if scope:
        items = [o for o in items if scope in (o.get("scope") or ["global"])]
    if owner:
        items = [o for o in items if o.get("owner") == owner]
    if blocking_only:
        items = [o for o in items if o.get("blocking")]
    if not items:
        return "没有符合条件的待办项。"
    items = sorted(items, key=lambda o: (o.get("status") == "resolved", not o.get("blocking")))
    return f"待办项 {len(items)} 条：\n" + "\n".join(_open_line(o) for o in items)


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


def op_rescope(record_id: str, scope: Union[str, List[str]]) -> str:
    """Change the scope of a fact (GT-xx) or open item (OI-xx); the content stays as written."""
    rec = _load_records(for_write=True)
    pool = rec["facts"] if record_id.startswith("GT-") else rec["open_items"] if record_id.startswith("OI-") else None
    if pool is None:
        raise ArchEngineError("rescope 只适用于事实 (GT-xx) 和待办项 (OI-xx)。墓志的作用域就是它记录的节点。")
    item = next((x for x in pool if x["id"] == record_id), None)
    if item is None:
        raise ArchEngineError(f"记录 '{record_id}' 不存在。")
    scopes = _normalize_scopes(scope, _graph_if_any())
    if scopes == item.get("scope"):
        return f"{record_id} 的作用域已经是 {_scope_text(scopes)}。"
    old = _scope_text(item.get("scope"))
    item["scope"] = scopes
    _save_records(rec)
    _shadow_commit(f"rescope-{record_id}", f"rescope {record_id}: {old} -> {_scope_text(scopes)}")
    out = f"已把 {record_id} 的作用域从 {old} 改为 {_scope_text(scopes)}。"
    used, limit = _global_fact_budget(rec)
    return out + f"\nglobal 事实现为 {used} / {limit} 字符。"


def op_legacy(action: str = "list", record_id: str = "", note: str = "", include_resolved: bool = False) -> str:
    """List, show or resolve unclassified blocks kept verbatim by migrate-records."""
    rec = _load_records(for_write=(action == "resolve"))
    if action == "list":
        items = [lg for lg in rec["legacy"] if include_resolved or lg.get("status") != "resolved"]
        if not items:
            return "没有待处理的未分类遗留记录。"
        out = [f"未分类遗留记录 {len(items)} 条（arch legacy show --id LG-xx 看全文）："]
        for lg in items:
            first = next((ln.strip() for ln in lg["text"].splitlines() if ln.strip()), "")
            state = "已处理" if lg.get("status") == "resolved" else "待处理"
            where = lg["source"] + (f" · {lg['section']}" if lg.get("section") else "")
            out.append(f"- [{lg['id']}] {state} · {where} · {len(lg['text'])} 字符：{first[:60]}")
        out.append(
            "处理方式：把其中仍有用的内容拆成 verify --scope（决策与约束）、open add（待验证项）、"
            "set-node-state（实现状态 / refs），调查过程只留结论和证据路径；然后 legacy resolve --id 关闭。"
        )
        return "\n".join(out)
    item = next((lg for lg in rec["legacy"] if lg["id"] == record_id), None)
    if item is None:
        raise ArchEngineError(f"遗留记录 '{record_id}' 不存在。用 arch legacy list 查看编号。")
    if action == "show":
        head = f"[{item['id']}] 来自 {item['source']}" + (f" · {item['section']}" if item.get("section") else "")
        if item.get("status") == "resolved":
            head += f"（已处理：{item.get('note', '')}）"
        return head + "\n\n" + item["text"]
    if action == "resolve":
        if item.get("status") == "resolved":
            return f"{record_id} 已处理过。"
        item["status"] = "resolved"
        item["note"] = note.strip()
        item["resolved_at"] = _today()
        _save_records(rec)
        _shadow_commit(f"legacy-{record_id}", f"legacy resolve {record_id}")
        left = sum(1 for lg in rec["legacy"] if lg.get("status") != "resolved")
        return (f"已将 {record_id} 标记为已处理（原文仍保存在 records.json）。剩余待处理 {left} 条。")
    raise ArchEngineError(f"legacy 的操作只能是 list / show / resolve，收到：{action}")


def op_config(global_fact_limit: Optional[int] = None) -> str:
    rec = _load_records(for_write=global_fact_limit is not None)
    if global_fact_limit is not None:
        if global_fact_limit <= 0:
            raise ArchEngineError("global 事实字数上限必须是正整数。")
        rec["settings"]["global_fact_char_limit"] = int(global_fact_limit)
        _save_records(rec)
        _shadow_commit("config", f"config global_fact_char_limit={global_fact_limit}")
    used, limit = _global_fact_budget(rec)
    return f"global_fact_char_limit = {limit}（当前 global 事实 {used} 字符）"


def op_status() -> str:
    """Whole-project progress: node states, open items, record counts, migration state."""
    graph = _load_graph()
    all_nodes = list(_iter_all_nodes(graph["nodes"]))
    leaves = [n for n in all_nodes if n["status"] != "super_node"]
    count = Counter(n["status"] for n in all_nodes)
    solid = [n for n in leaves if n["status"] == "solid"]
    impl = Counter(_node_state(n)["impl"] for n in solid)
    verify = Counter(_node_state(n)["verify"]["level"] for n in solid)

    out = [f"=== 项目状态 · step {graph.get('step', 0)} ==="]
    out.append(
        f"节点：已确定 {count['solid']} · 待定 {count['hole']} · 储备 {count['fog']} · 折叠模块 {count['super_node']}"
    )
    out.append(
        "实现进度（已确定节点，含折叠子节点）："
        + " · ".join(f"{IMPL_TEXT[k]} {impl[k]}" for k in reversed(IMPL_LEVELS))
    )
    out.append("验证程度：" + " · ".join(f"{VERIFY_TEXT[k]} {verify[k]}" for k in reversed(VERIFY_LEVELS)))

    out.append("--- 节点明细 ---")

    def describe(n: Dict[str, Any], depth: int) -> None:
        pad = "  " * depth + ("└ " if depth else "")
        if n["status"] == "super_node":
            inner = [c for c in _iter_all_nodes(n["children"]) if c["status"] != "super_node"]
            built = sum(1 for c in inner if _node_state(c)["impl"] == "built")
            out.append(f"{pad}{n['id']} [模块] {built}/{len(inner)} 已实现")
            for c in n["children"]:
                describe(c, depth + 1)
            return
        st = _node_state(n)
        extra = []
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
        out.append(
            f"[LEGACY_RECORDS] {len(legacy)} 条未分类遗留记录待人工处理（{', '.join(lg['id'] for lg in legacy)}）："
            "arch legacy list / legacy show --id / legacy resolve --id"
        )
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


def op_migrate_records() -> str:
    """Convert md records into .arch/records.json, then render the md from it.

    Idempotent: entries already present (same content) are skipped, and a
    generated md that nobody edited contributes nothing. Lines added by hand
    to a generated md are imported too.
    """
    if not ARCH_DIR.exists():
        raise ArchEngineError("未找到 .arch/ 目录。请在持有 .arch/ 的项目根目录执行。")
    first_time = not _records_enabled()
    rec = _new_records() if first_time else _read_records()
    expected = _rendered_files(rec)
    stats: Counter = Counter()
    sources = []
    for path in (GROUND_TRUTH_FILE, GRAVEYARD_FILE, OPEN_ITEMS_FILE):
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if text == expected[path] or _md_is_boilerplate(text):
            continue
        if GENERATED_MARKER in text:
            text = _added_lines(text, expected[path])
        before = sum(stats[k] for k in ("facts", "graveyard", "legacy"))
        _merge_parsed(rec, _parse_md_records(text), path.name, stats)
        if sum(stats[k] for k in ("facts", "graveyard", "legacy")) > before:
            sources.append(path.name)

    if not first_time and not sources:
        out = "记录已是最新：没有需要导入的新内容。" + (f"（跳过重复 {stats['skipped']} 条）" if stats["skipped"] else "")
        if _pending_md_edits(rec):
            # Only duplicates or deletions were made by hand: records.json wins.
            _save_records(rec)
            out += "\nmd 里的删改不会生效，已按 records.json 重新生成；改动记录请用 verify --supersedes、rescope 等命令。"
        return out

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    _shadow_commit(f"pre-migrate-records-{stamp}", "snapshot before migrate-records")
    _save_records(rec)
    _shadow_commit(f"migrate-records-{stamp}", "migrate-records")
    out = [
        f"已{'迁移' if first_time else '导入'}结构化记录到 {RECORDS_FILE}（迁移前快照：pre-migrate-records-{stamp}）：",
        f"事实 {stats['facts']} 条（默认作用域 global）、墓志 {stats['graveyard']} 条、"
        f"未分类遗留 {stats['legacy']} 条；跳过重复 {stats['skipped']} 条。",
        "ground_truth.md / graveyard.md / open_items.md 现由工具生成，不要再手改。",
    ]
    if stats["legacy"]:
        out.append(
            f"[LEGACY_RECORDS] 有 {stats['legacy']} 段内容不是标准条目，已原样保存为未分类遗留记录。"
            "JIT 指令：和用户逐条处理（arch legacy list / legacy show --id），拆成带作用域的事实、待办项或节点状态后 legacy resolve。"
        )
    if stats["facts"]:
        out.append("导入的事实都是 global 作用域；只与部分节点相关的，用 arch rescope --id GT-xx --scope <节点> 收窄。")
    warning = _budget_warning(rec)
    if warning:
        out.append(warning)
    return "\n".join(out)


def _md_bullets(path: Path) -> List[str]:
    if not path.exists():
        return []
    return [ln[2:].strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.startswith("- ")]


def op_export_ui(out_path: str = "") -> str:
    """Render the stage-swimlane dashboard from dashboard_template.html.

    The page is fully self-contained (no network). Graph data is embedded as
    JSON inside <script type="application/json">, with <, > and & escaped so
    node text can never close the script element.
    """
    graph = _load_graph()
    target = Path(out_path).resolve() if out_path else (ARCH_DIR / "dashboard.html")
    target.parent.mkdir(parents=True, exist_ok=True)

    seed_raw = SEED_FILE.read_text(encoding="utf-8") if SEED_FILE.exists() else ""
    seed = "\n".join(
        ln for ln in seed_raw.splitlines() if ln.strip() and not ln.lstrip().startswith("#")
    ).strip() or "架构图"

    payload = {
        "seed": seed,
        "step": graph.get("step", 1),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "graph": graph,
        "topo_order": _topological_order(graph),
        "skeleton": _find_walking_skeleton(graph),
        "next_focus": _next_focus_hole(graph),
        "limits": {"holes": MAX_ACTIVE_HOLES, "solids": MAX_UNFOLDED_SOLID},
        "node_state": {
            n["id"]: _node_state(n) for n in _iter_all_nodes(graph["nodes"]) if n["status"] != "super_node"
        },
        "records": {"enabled": False},
    }
    if _records_enabled():
        rec = _read_records()
        active = _active_facts(rec)
        payload["ground_truth"] = [_fact_line(f, show_scope=False) for f in active if _is_global(f)]
        payload["graveyard"] = [_grave_line(g) for g in rec["graveyard"]]
        payload["records"] = {
            "enabled": True,
            "facts": [{"id": f["id"], "scope": f.get("scope") or ["global"], "line": _fact_line(f, show_scope=False)}
                      for f in active],
            "graveyard": [{"id": g["id"], "node": g["node"], "line": _grave_line(g)} for g in rec["graveyard"]],
            "open_items": [o for o in rec["open_items"] if o.get("status") != "resolved"],
            "legacy_open": sum(1 for lg in rec["legacy"] if lg.get("status") != "resolved"),
        }
    else:
        payload["ground_truth"] = _md_bullets(GROUND_TRUTH_FILE)
        payload["graveyard"] = _md_bullets(GRAVEYARD_FILE)
    data = (
        json.dumps(payload, ensure_ascii=False)
        .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    )
    template = DASHBOARD_TEMPLATE.read_text(encoding="utf-8")
    tokens = {"__ARCH_TITLE__": html.escape(seed[:40]), "__ARCH_DATA__": data}
    html_content = re.sub(r"__ARCH_(?:TITLE|DATA)__", lambda m: tokens[m.group(0)], template)
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

    p_edge = sub.add_parser("set-edge-contract", help="更新指定连线的契约")
    p_edge.add_argument("--from", dest="from_node", required=True)
    p_edge.add_argument("--to", dest="to_node", required=True)
    p_edge.add_argument("--contract", required=True)

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

    p_fold = sub.add_parser("fold", help="将已确定、相互连通的子图折叠为一个超级节点")
    p_fold.add_argument("--nodes", required=True, help="逗号分隔的待折叠节点 ID，如 a,b,c")
    p_fold.add_argument("--id", required=True, help="超级节点 ID")
    p_fold.add_argument("--label", required=True)
    p_fold.add_argument("--spec", required=True)
    p_fold.add_argument("--stage", default="")

    p_unfold = sub.add_parser("unfold", help="展开超级节点，恢复其子节点与连线")
    p_unfold.add_argument("--node", required=True)
    p_unfold.add_argument(
        "--attach", action="append", default=[], metavar="NEIGHBOUR=CHILD",
        help="折叠期间新增连线的归位：外部节点ID=子节点ID（可重复）",
    )

    p_ver = sub.add_parser("verify", help="沉淀已验证事实与架构约束")
    p_ver.add_argument("--fact", required=True)
    p_ver.add_argument("--constraint", required=True)
    p_ver.add_argument("--scope", action="append", default=[],
                       help="作用域：global（默认）、节点 ID 或阶段名，可重复；需已迁移为结构化记录")
    p_ver.add_argument("--source", default="", help="证据来源，如 analysis/x/case.json#key 或实测记录路径")
    p_ver.add_argument("--supersedes", action="append", default=[], metavar="GT-xx",
                       help="本条取代的旧事实编号，可重复；被取代的条目默认不再展示")

    p_state = sub.add_parser("set-node-state", help="记录节点的实现进度、验证程度、引用与实现文件")
    p_state.add_argument("--node", required=True)
    p_state.add_argument("--impl", default="", choices=["", *IMPL_LEVELS])
    p_state.add_argument("--verify", default="", choices=["", *VERIFY_LEVELS])
    p_state.add_argument("--verify-date", default="", help="YYYY-MM-DD，缺省为今天")
    p_state.add_argument("--ref", action="append", default=None,
                         help="替换整个引用列表，可重复，如 docs/protocol.md#6")
    p_state.add_argument("--add-ref", action="append", default=[])
    p_state.add_argument("--drop-ref", action="append", default=[])
    p_state.add_argument("--file-path", action="append", default=None, help="替换实现文件列表，可重复")

    p_open = sub.add_parser("open", help="待办项登记：add / resolve / list")
    open_sub = p_open.add_subparsers(dest="open_cmd", required=True)
    p_oadd = open_sub.add_parser("add")
    p_oadd.add_argument("--question", required=True)
    p_oadd.add_argument("--kind", required=True, choices=list(OPEN_KINDS))
    p_oadd.add_argument("--owner", required=True, choices=list(OPEN_OWNERS))
    p_oadd.add_argument("--scope", action="append", default=[], help="global（默认）、节点 ID 或阶段名，可重复")
    p_oadd.add_argument("--blocking", action="store_true", help="卡住开发")
    p_ores = open_sub.add_parser("resolve")
    p_ores.add_argument("--id", required=True)
    p_ores.add_argument("--resolution", required=True)
    p_ores.add_argument("--constraint", default="", help="给出时把结论同时沉淀为同作用域的事实")
    p_olist = open_sub.add_parser("list")
    p_olist.add_argument("--node", default="", help="与该节点相关（含阶段、折叠子节点）")
    p_olist.add_argument("--scope", default="", help="作用域精确匹配，如 global")
    p_olist.add_argument("--owner", default="", choices=["", *OPEN_OWNERS])
    p_olist.add_argument("--blocking", action="store_true")
    p_olist.add_argument("--all", action="store_true", help="包括已解决的")

    p_facts = sub.add_parser("facts", help="查询事实：缺省列 global，--node 列节点相关，--all 列全部")
    p_facts.add_argument("--node", default="")
    p_facts.add_argument("--all", action="store_true")
    p_facts.add_argument("--with-superseded", action="store_true")

    sub.add_parser("status", help="项目进度总览：节点实现/验证、待办项、记录统计")
    sub.add_parser("migrate-records", help="把旧格式 ground_truth.md / graveyard.md 导入 .arch/records.json（幂等）")

    p_rescope = sub.add_parser("rescope", help="修改事实 (GT-xx) 或待办项 (OI-xx) 的作用域")
    p_rescope.add_argument("--id", required=True)
    p_rescope.add_argument("--scope", action="append", required=True)

    p_legacy = sub.add_parser("legacy", help="未分类遗留记录：list / show / resolve")
    legacy_sub = p_legacy.add_subparsers(dest="legacy_cmd", required=True)
    p_llist = legacy_sub.add_parser("list")
    p_llist.add_argument("--all", action="store_true", help="包括已处理的")
    p_lshow = legacy_sub.add_parser("show")
    p_lshow.add_argument("--id", required=True)
    p_lres = legacy_sub.add_parser("resolve")
    p_lres.add_argument("--id", required=True)
    p_lres.add_argument("--note", default="", help="处理去向，如“拆为 GT-12、OI-03”")

    p_cfg = sub.add_parser("config", help="查看或设置记录配置")
    p_cfg.add_argument("--global-fact-limit", type=int, default=None, help="global 事实字数上限（默认 3000）")

    p_gnd = sub.add_parser("ground")
    p_gnd.add_argument("--type", required=True, choices=["human_verify", "doc_ref", "probe_cmd", "code_ref"])
    p_gnd.add_argument("--source", required=True)
    p_gnd.add_argument("--note", default="")

    p_exp = sub.add_parser("export-task")
    p_exp.add_argument("--node", required=True)

    p_com = sub.add_parser("commit-node")
    p_com.add_argument("--node", required=True)
    p_com.add_argument("--file-path", required=True, action="append", help="实现文件，可重复绑定多个")

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
        elif args.cmd == "set-edge-contract":
            print(op_set_edge_contract(args.from_node, args.to_node, args.contract))
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
        elif args.cmd == "fold":
            print(op_fold(args.nodes.split(","), args.id, args.label, args.spec, args.stage))
        elif args.cmd == "unfold":
            attach = {}
            for pair in args.attach:
                neighbour, sep, child = pair.partition("=")
                if not sep or not neighbour.strip() or not child.strip():
                    raise ArchEngineError(f"--attach 格式应为 外部节点ID=子节点ID，收到：{pair}")
                attach[neighbour.strip()] = child.strip()
            print(op_unfold(args.node, attach))
        elif args.cmd == "verify":
            print(op_verify(args.fact, args.constraint, args.scope, args.source, args.supersedes))
        elif args.cmd == "set-node-state":
            print(op_set_node_state(
                args.node, args.impl, args.verify, args.verify_date,
                args.ref, args.add_ref, args.drop_ref, args.file_path,
            ))
        elif args.cmd == "open":
            if args.open_cmd == "add":
                print(op_open_add(args.question, args.kind, args.owner, args.scope, args.blocking))
            elif args.open_cmd == "resolve":
                print(op_open_resolve(args.id, args.resolution, args.constraint))
            else:
                print(op_open_list(args.node, args.scope, args.all, args.owner, args.blocking))
        elif args.cmd == "facts":
            print(op_facts(args.node, args.all, args.with_superseded))
        elif args.cmd == "status":
            print(op_status())
        elif args.cmd == "migrate-records":
            print(op_migrate_records())
        elif args.cmd == "rescope":
            print(op_rescope(args.id, args.scope))
        elif args.cmd == "legacy":
            print(op_legacy(args.legacy_cmd, getattr(args, "id", ""), getattr(args, "note", ""),
                            getattr(args, "all", False)))
        elif args.cmd == "config":
            print(op_config(args.global_fact_limit))
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
