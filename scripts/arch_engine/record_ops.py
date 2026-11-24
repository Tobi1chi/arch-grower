"""Operations on facts, graveyard entries, open items, legacy records and record settings."""

import json
from datetime import datetime
from typing import List, Optional, Union

from .common import (
    ARCH_DIR, ArchEngineError, BASIC_PRESETS, GROUNDING_FILE, GROUND_TRUTH_FILE, MODES,
    _default_config, _graph_if_any, _load_config, _load_graph, _mode_for, _save_config,
    _shadow_commit, _state_needs_advanced, _today,
)
from .graph import _find_any_node, _validate_topology
from .records import (
    LEGACY_GUIDE, NOT_MIGRATED_HINT, OPEN_KINDS, OPEN_OWNERS, _budget_warning, _context_keys,
    _fact_line, _global_fact_budget, _grave_line, _is_global, _load_records, _matches, _next_id,
    _normalize_scopes, _open_line, _records_enabled, _save_records, _scope_text,
)


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
        out = [f"迁移原文 {len(items)} 条（arch legacy show 看下一条全文）："]
        for lg in items:
            first = next((ln.strip() for ln in lg["text"].splitlines() if ln.strip()), "")
            state = "已处理" if lg.get("status") == "resolved" else "待处理"
            where = lg["source"] + (f" · {lg['section']}" if lg.get("section") else "")
            out.append(f"- [{lg['id']}] {state} · {where} · {len(lg['text'])} 字符：{first[:60]}")
        if any(lg.get("status") != "resolved" for lg in items):
            out.append(LEGACY_GUIDE)
        return "\n".join(out)
    if action == "show" and not record_id:
        pending = [lg for lg in rec["legacy"] if lg.get("status") != "resolved"]
        if not pending:
            return "没有待处理的迁移原文，迁移已完成。"
        record_id = pending[0]["id"]
    item = next((lg for lg in rec["legacy"] if lg["id"] == record_id), None)
    if item is None:
        raise ArchEngineError(f"遗留记录 '{record_id}' 不存在。用 arch legacy list 查看编号。")
    if action == "show":
        left = sum(1 for lg in rec["legacy"] if lg.get("status") != "resolved")
        head = f"[{item['id']}] 来自 {item['source']}" + (f" · 标题：{item['section']}" if item.get("section") else "")
        if item.get("status") == "resolved":
            return head + f"（已处理：{item.get('note', '')}）\n\n" + item["text"]
        return (
            f"{head}（待处理共 {left} 条）\n\n{item['text']}\n\n"
            "处理：按类型用 verify --scope [--supersedes] / graveyard add / open add / set-node-state 记录，"
            "流程规则移到项目 AGENTS.md，无用的不记；然后 "
            f"arch legacy resolve --id {item['id']} --note \"拆为 ...\"（完整步骤：arch legacy list）。"
        )
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


def op_graveyard_add(node_id: str, scheme: str, reason: str) -> str:
    """Record an abandoned scheme without resetting the node (reset-node does both)."""
    if not scheme.strip() or not reason.strip():
        raise ArchEngineError("墓志需要写明废弃的方案和原因。")
    rec = _load_records(for_write=True)
    graph = _graph_if_any()
    if graph is not None and _find_any_node(graph, node_id) is None:
        raise ArchEngineError(f"节点 '{node_id}' 不存在。墓志必须挂在图中的节点上。")
    entry = {
        "id": _next_id(rec["graveyard"], "GY"),
        "node": node_id,
        "scheme": scheme.strip(),
        "reason": reason.strip(),
        "date": _today(),
        "detail": "",
    }
    rec["graveyard"].append(entry)
    _save_records(rec)
    _shadow_commit(f"grave-{entry['id']}", f"graveyard add {entry['id']} ({node_id})")
    return f"已记入墓志：\n{_grave_line(entry)}"


def op_config(
    global_fact_limit: Optional[int] = None,
    preset: Optional[str] = None,
    default_topology: Optional[str] = None,
    max_depth: Optional[int] = None,
    max_chain_len: Optional[int] = None,
    max_hub_degree: Optional[int] = None,
    max_cycle_len: Optional[int] = None,
    max_active_holes: Optional[int] = None,
    max_unfolded_solid: Optional[int] = None,
    auto_bridge_limit: Optional[int] = None,
    allow_feedback: Optional[bool] = None,
    mode: Optional[str] = None,
) -> str:
    """View or update project mode, preset / guardrail config (`.arch/config.json`) and global fact budget."""
    if mode is not None and mode not in MODES:
        raise ArchEngineError(f"未知 mode '{mode}'。可选：{' / '.join(MODES)}。")
    prev_mode = _load_config()["mode"]
    cfg = _default_config(preset) if preset is not None else _load_config()
    cfg["mode"] = prev_mode   # switching preset keeps the mode
    changed = preset is not None
    notes: List[str] = []
    if mode is not None and mode != prev_mode:
        if mode == "basic" and _state_needs_advanced():
            raise ArchEngineError(
                "无法切回 basic：项目已在使用 advanced 能力（星形/环形拓扑、子系统，或未处理完的迁移原文）。"
                "JIT 指令：保持 advanced 模式。"
            )
        cfg["mode"] = mode
        changed = True
        notes.append(f"[MODE] 已切换为 {mode} 模式。" + (
            "MCP 工具列表已更新；高级命令说明见 references/advanced.md。" if mode == "advanced" else ""
        ))

    if default_topology is not None:
        cfg["default_topology"] = _validate_topology(default_topology)
        changed = True

    int_updates = {
        "max_depth": max_depth,
        "max_chain_len": max_chain_len,
        "max_hub_degree": max_hub_degree,
        "max_cycle_len": max_cycle_len,
        "max_active_holes": max_active_holes,
        "max_unfolded_solid": max_unfolded_solid,
        "auto_bridge_limit": auto_bridge_limit,
        "global_fact_char_limit": global_fact_limit,
    }
    for key, val in int_updates.items():
        if val is not None:
            if int(val) <= 0:
                raise ArchEngineError(f"{key} 必须是正整数，收到：{val}。")
            cfg[key] = int(val)
            changed = True

    if allow_feedback is not None:
        cfg["allow_feedback"] = bool(allow_feedback)
        changed = True

    if cfg["mode"] == "basic" and _mode_for(cfg["preset"], cfg["default_topology"]) == "advanced":
        if mode == "basic":
            raise ArchEngineError(
                f"basic 模式只支持 pipeline 拓扑与 {' / '.join(BASIC_PRESETS)} 预设。"
                "JIT 指令：换回这些预设，或改用 --mode advanced。"
            )
        cfg["mode"] = "advanced"
        changed = True
        notes.append(
            f"[MODE] preset={cfg['preset']} / default_topology={cfg['default_topology']} 需要 advanced 模式，已自动切换。"
        )

    if changed:
        _save_config(cfg)
        # Only touch records.json when the fact budget itself was asked to change.
        if (global_fact_limit is not None or preset is not None) and _records_enabled():
            rec = _load_records(for_write=True)
            rec["settings"]["global_fact_char_limit"] = int(cfg["global_fact_char_limit"])
            _save_records(rec)
        _shadow_commit("config", f"config mode={cfg['mode']} preset={cfg['preset']}")

    used_text = ""
    if _records_enabled():
        rec = _load_records(for_write=False)
        used, limit = _global_fact_budget(rec)
        if changed:
            limit = int(cfg["global_fact_char_limit"])
        used_text = f"（当前 global 事实 {used} 字符）"
    else:
        limit = int(cfg["global_fact_char_limit"])

    lines = notes + [
        f"mode = {cfg['mode']} | preset = {cfg['preset']} | default_topology = {cfg['default_topology']}",
        f"max_depth = {cfg['max_depth']} | max_chain_len = {cfg['max_chain_len']} | "
        f"max_hub_degree = {cfg['max_hub_degree']} | max_cycle_len = {cfg['max_cycle_len']}",
        f"max_active_holes = {cfg['max_active_holes']} | max_unfolded_solid = {cfg['max_unfolded_solid']} | "
        f"auto_bridge_limit = {cfg['auto_bridge_limit']} | allow_feedback = {cfg['allow_feedback']}",
        f"global_fact_char_limit = {limit}{used_text}",
    ]
    return "\n".join(lines)
