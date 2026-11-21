"""export-ui: the self-contained HTML dashboard."""

import html
import json
import re
from datetime import datetime
from pathlib import Path
from typing import List

from .common import (
    ARCH_DIR, DASHBOARD_TEMPLATE, GRAVEYARD_FILE, GROUND_TRUTH_FILE, MAX_ACTIVE_HOLES,
    MAX_UNFOLDED_SOLID, SEED_FILE, _load_graph,
)
from .graph import (
    _find_walking_skeleton, _iter_all_nodes, _next_focus_hole, _node_state, _topological_order,
)
from .records import (
    _active_facts, _fact_line, _grave_line, _is_global, _read_records, _records_enabled,
)


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
