"""Resolve node refs (`docs/x.md#6`, `case.json#a.b`) to the text they point at."""

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .common import REF_SECTION_LIMIT, REF_WHOLE_FILE_LIMIT
from .records import MD_HEADING


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
