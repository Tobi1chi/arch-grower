"""migrate-records: turn old-format md records into typed records, losslessly."""

import textwrap
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List, Optional

from .common import (
    ARCH_DIR, ArchEngineError, GRAVEYARD_FILE, GROUND_TRUTH_FILE, OPEN_ITEMS_FILE, RECORDS_FILE,
    _shadow_commit, _today,
)
from .records import (
    DEFAULT_TITLES, GENERATED_MARKER, GT_LINE, GY_LINE, MD_HEADING, _budget_warning,
    _md_is_boilerplate, _new_records, _next_id, _pending_md_edits, _read_records,
    _records_enabled, _rendered_files, _save_records,
)


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
                        "constraint": (fm.group(3) or "").strip(), "section": section, "_detail": []}
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


def _reparse_legacy(rec: Dict[str, Any], stats: Counter) -> List[str]:
    """Split open legacy records that the current parser now understands.

    A legacy block holding entries (e.g. GT lines without "-> 架构约束", which
    older versions did not recognise) becomes those entries plus whatever is
    left as new legacy blocks; the original block is marked resolved and kept
    verbatim. Blocks with no entries are left untouched, so this is idempotent.
    """
    notes = []
    for lg in [x for x in rec["legacy"] if x.get("status") != "resolved"]:
        parsed = _parse_md_records(lg["text"])
        if not parsed["facts"] and not parsed["graveyard"]:
            continue
        for item in parsed["facts"] + parsed["graveyard"] + parsed["legacy"]:
            item["section"] = item["section"] or lg.get("section", "")
        known = {x["id"] for key in ("facts", "graveyard", "legacy") for x in rec[key]}
        _merge_parsed(rec, parsed, lg["source"], stats)
        new_ids = [x["id"] for key in ("facts", "graveyard", "legacy") for x in rec[key] if x["id"] not in known]
        for key in ("facts", "graveyard", "legacy"):
            for x in rec[key]:
                if x["id"] in new_ids:
                    x["from_legacy"] = lg["id"]
        lg["status"] = "resolved"
        lg["resolved_at"] = _today()
        lg["note"] = "migrate-records 重新解析为 " + ("、".join(new_ids) or "已有条目（内容重复）")
        notes.append(f"{lg['id']} → {'、'.join(new_ids) or '已有条目'}")
    return notes


def op_migrate_records() -> str:
    """Convert md records into .arch/records.json, then render the md from it.

    Idempotent: entries already present (same content) are skipped, and a
    generated md that nobody edited contributes nothing. Lines added by hand
    to a generated md are imported too. Open legacy records that contain
    recognisable entries are split into them (see _reparse_legacy).
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
    reparsed = _reparse_legacy(rec, stats)

    if not first_time and not sources and not reparsed:
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
    if reparsed:
        out.append("已把含标准条目的未分类遗留记录拆开（原文保留为已处理）：" + "；".join(reparsed))
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
