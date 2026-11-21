"""migrate-records: store old-format md records verbatim as legacy chunks.

The engine does not interpret old ground_truth.md / graveyard.md. Only the
agent, having read a chunk, can tell its scope and type, so every chunk goes
into `legacy` and the agent classifies it with the record commands
(see LEGACY_GUIDE and SKILL.md "迁移旧项目").
"""

import re
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List, Tuple

from .common import (
    ARCH_DIR, ArchEngineError, GRAVEYARD_FILE, GROUND_TRUTH_FILE, OPEN_ITEMS_FILE, RECORDS_FILE,
    _shadow_commit, _today,
)
from .records import (
    DEFAULT_TITLES, GENERATED_MARKER, LEGACY_GUIDE, MD_HEADING, _md_is_boilerplate, _new_records,
    _next_id, _pending_md_edits, _read_records, _records_enabled, _rendered_files, _save_records,
)

ITEM_LINE = re.compile(r"^(?:[-*+]|\d+[.)])\s+\S")
FENCE = re.compile(r"^\s*(```|~~~)")


def _strip_blank_edges(lines: List[str]) -> List[str]:
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def _chunk_md(text: str) -> List[Dict[str, str]]:
    """Cut an md file into chunks small enough to classify one at a time.

    Each top-level list item (with its indented continuation lines) is one
    chunk; other content (tables, narrative, code) is one chunk per heading.
    A chunk's `section` is the path of headings above it, kept verbatim
    (e.g. "## 实现状态 / ### 硬件"); a heading with no content under it becomes
    a chunk of its own. Only the engine's default titles and the generated
    marker are dropped, so every other character ends up in a chunk.
    """
    chunks: List[Dict[str, str]] = []
    stack: List[List[Any]] = []          # open headings: [level, raw line, used]
    block: List[str] = []
    kind = ""                            # "", "item" or "para"
    fence = ""

    def emit(lines: List[str]) -> None:
        chunks.append({"text": "\n".join(lines), "section": " / ".join(h[1] for h in stack)})
        for h in stack:
            h[2] = True

    def flush() -> None:
        nonlocal kind
        lines = _strip_blank_edges(block)
        block.clear()
        kind = ""
        if lines:
            emit(lines)

    def close_headings(level: int) -> None:
        while stack and stack[-1][0] >= level:
            level_, raw, used = stack.pop()
            if not used:
                emit([raw])

    for raw in text.splitlines():
        stripped = raw.strip()
        if fence:
            block.append(raw)
            if stripped.startswith(fence):
                fence = ""
            continue
        if stripped == GENERATED_MARKER or stripped in DEFAULT_TITLES:
            continue
        mark = FENCE.match(raw)
        if mark and not (kind == "item" and raw[:1] in (" ", "\t")):
            if kind != "para":
                flush()
                kind = "para"
            block.append(raw)
            fence = mark.group(1)
            continue
        if mark:  # fence indented under a list item belongs to the item
            block.append(raw)
            fence = mark.group(1)
            continue
        heading = MD_HEADING.match(raw)
        if heading:
            flush()
            close_headings(len(heading.group(1)))
            stack.append([len(heading.group(1)), stripped, False])
            continue
        if ITEM_LINE.match(raw):
            flush()
            kind = "item"
            block.append(raw)
            continue
        if kind == "item" and (not stripped or raw[:1] in (" ", "\t")):
            block.append(raw)
            continue
        if kind != "para":
            if not stripped:
                continue
            flush()
            kind = "para"
        block.append(raw)
    flush()
    close_headings(0)
    return chunks


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


def _import_chunks(rec: Dict[str, Any], chunks: List[Dict[str, str]], source: str, stats: Counter) -> None:
    """Append chunks as open legacy records; a chunk already stored (same file,
    section and text, handled or not) is skipped, which keeps migration idempotent."""
    known = {(lg.get("source"), lg.get("section", ""), lg["text"]) for lg in rec["legacy"]}
    for chunk in chunks:
        key: Tuple[str, str, str] = (source, chunk["section"], chunk["text"])
        if key in known:
            stats["skipped"] += 1
            continue
        known.add(key)
        rec["legacy"].append({"id": _next_id(rec["legacy"], "LG"), "source": source,
                              "section": chunk["section"], "text": chunk["text"], "status": "open",
                              "note": "", "imported": _today()})
        stats["legacy"] += 1


def op_migrate_records() -> str:
    """Store old md records in .arch/records.json as legacy chunks, then render the md.

    Idempotent: chunks already stored are skipped, and a generated md that
    nobody edited contributes nothing. Lines added by hand to a generated md
    are chunked and stored too.
    """
    if not ARCH_DIR.exists():
        raise ArchEngineError("未找到 .arch/ 目录。请在持有 .arch/ 的项目根目录执行。")
    first_time = not _records_enabled()
    rec = _new_records() if first_time else _read_records()
    expected = _rendered_files(rec)
    stats: Counter = Counter()
    for path in (GROUND_TRUTH_FILE, GRAVEYARD_FILE, OPEN_ITEMS_FILE):
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if text == expected[path] or _md_is_boilerplate(text):
            continue
        if GENERATED_MARKER in text:
            text = _added_lines(text, expected[path])
        _import_chunks(rec, _chunk_md(text), path.name, stats)

    if not first_time and not stats["legacy"]:
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
    pending = sum(1 for lg in rec["legacy"] if lg.get("status") != "resolved")
    out = [
        f"已{'迁移' if first_time else '导入'}到 {RECORDS_FILE}（迁移前快照：pre-migrate-records-{stamp}）：",
        f"原文切为 {stats['legacy']} 条待处理记录（跳过重复 {stats['skipped']} 条），现共 {pending} 条待处理。"
        "原文一字不改地保存在 records.json，处理完前仍显示在生成的 md 里。",
        "ground_truth.md / graveyard.md / open_items.md 现由工具生成，不要再手改。",
    ]
    if pending:
        out.append(f"[LEGACY_RECORDS] JIT 指令：现在逐条处理这 {pending} 条记录。\n{LEGACY_GUIDE}")
    return "\n".join(out)
