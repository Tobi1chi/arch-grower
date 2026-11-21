"""Typed records in .arch/records.json: storage, scopes, formatting and the
md files rendered from it."""

import json
import re
import textwrap
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

from .common import (
    ArchEngineError, DEFAULT_GLOBAL_FACT_LIMIT, GRAVEYARD_FILE, GROUND_TRUTH_FILE,
    OPEN_ITEMS_FILE, RECORDS_FILE,
)
from .graph import _find_any_node, _iter_all_nodes


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

# A fact line: `- [GT-xx] (已验证) text -> 架构约束：constraint`. The "(已验证)"
# tag and the constraint part are optional; group 3 is None without an arrow.
GT_LINE = re.compile(
    r"^[-*]\s+\[(GT-[^\]\s]+)\]\s*(?:[(（]已验证[)）]\s*)?(.*?)(?:\s*->\s*架构约束\s*[：:]\s*(.*))?$"
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

def _fact_head(f: Dict[str, Any]) -> str:
    """`- [GT-xx] text -> 架构约束：constraint`, without the arrow when there is no constraint."""
    head = f"- [{f['id']}] {f['text']}"
    return head + (f" -> 架构约束：{f['constraint']}" if f.get("constraint") else "")


def _fact_line(f: Dict[str, Any], show_scope: bool = True, superseded_by: str = "") -> str:
    line = _fact_head(f)
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
            lines.append(_fact_head(f))
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
            lines.append(_fact_head(f))
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
