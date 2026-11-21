"""Paths, limits, the engine error type, Shadow Git and graph.json I/O."""

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


ARCH_DIR = Path(".arch")
GRAPH_FILE = ARCH_DIR / "graph.json"
SEED_FILE = ARCH_DIR / "SEED.md"
GROUND_TRUTH_FILE = ARCH_DIR / "ground_truth.md"
GRAVEYARD_FILE = ARCH_DIR / "graveyard.md"
OPEN_ITEMS_FILE = ARCH_DIR / "open_items.md"
RECORDS_FILE = ARCH_DIR / "records.json"
GROUNDING_FILE = ARCH_DIR / "grounding.json"
SHADOW_GIT_DIR = ARCH_DIR / ".git"
DASHBOARD_TEMPLATE = Path(__file__).resolve().parent.parent / "dashboard_template.html"

MAX_ACTIVE_HOLES = 3
MAX_UNFOLDED_SOLID = 7
DEFAULT_GLOBAL_FACT_LIMIT = 3000   # characters of active `global` facts before warning
REF_WHOLE_FILE_LIMIT = 4000        # a ref without '#section' is inlined only when this small
REF_SECTION_LIMIT = 20000          # longest inlined ref section in an export-task


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


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _graph_if_any() -> Optional[Dict[str, Any]]:
    return _load_graph() if GRAPH_FILE.exists() else None
