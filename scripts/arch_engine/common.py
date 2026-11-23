"""Paths, limits, project presets, the engine error type, Shadow Git and graph.json I/O."""

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


ENGINE_VERSION = "0.2.0"

ARCH_DIR = Path(".arch")
GRAPH_FILE = ARCH_DIR / "graph.json"
CONFIG_FILE = ARCH_DIR / "config.json"
SEED_FILE = ARCH_DIR / "SEED.md"
GROUND_TRUTH_FILE = ARCH_DIR / "ground_truth.md"
GRAVEYARD_FILE = ARCH_DIR / "graveyard.md"
OPEN_ITEMS_FILE = ARCH_DIR / "open_items.md"
RECORDS_FILE = ARCH_DIR / "records.json"
GROUNDING_FILE = ARCH_DIR / "grounding.json"
SHADOW_GIT_DIR = ARCH_DIR / ".git"
DASHBOARD_TEMPLATE = Path(__file__).resolve().parent.parent / "dashboard_template.html"

# Only state files belong in Shadow Git snapshots; vendored `.arch/scripts/`
# and generated `.arch/dashboard.html` stay outside so `fork` never rolls back
# the engine code itself.
SHADOW_STATE_FILES = (
    SEED_FILE,
    GRAPH_FILE,
    CONFIG_FILE,
    RECORDS_FILE,
    GROUND_TRUTH_FILE,
    GRAVEYARD_FILE,
    OPEN_ITEMS_FILE,
    GROUNDING_FILE,
)

MAX_ACTIVE_HOLES = 3
MAX_UNFOLDED_SOLID = 7
DEFAULT_GLOBAL_FACT_LIMIT = 3000   # characters of active `global` facts before warning
REF_WHOLE_FILE_LIMIT = 4000        # a ref without '#section' is inlined only when this small
REF_SECTION_LIMIT = 20000          # longest inlined ref section in an export-task

TOPOLOGIES = ("pipeline", "star", "ring")
EDGE_KINDS = ("primary", "feedback", "bidirectional")
NODE_ROLES = ("", "entry", "exit", "hub", "spoke")

PRESETS: Dict[str, Dict[str, Any]] = {
    "standard": {
        "preset": "standard",
        "default_topology": "pipeline",
        "max_depth": 2,
        "max_chain_len": 6,
        "max_hub_degree": 6,
        "max_cycle_len": 5,
        "max_active_holes": MAX_ACTIVE_HOLES,
        "max_unfolded_solid": MAX_UNFOLDED_SOLID,
        "auto_bridge_limit": 4,
        "allow_feedback": True,
        "global_fact_char_limit": DEFAULT_GLOBAL_FACT_LIMIT,
    },
    "lean": {
        "preset": "lean",
        "default_topology": "pipeline",
        "max_depth": 1,
        "max_chain_len": 5,
        "max_hub_degree": 4,
        "max_cycle_len": 4,
        "max_active_holes": 2,
        "max_unfolded_solid": 5,
        "auto_bridge_limit": 2,
        "allow_feedback": False,
        "global_fact_char_limit": 2000,
    },
    "agent": {
        "preset": "agent",
        "default_topology": "ring",
        "max_depth": 2,
        "max_chain_len": 6,
        "max_hub_degree": 6,
        "max_cycle_len": 6,
        "max_active_holes": 3,
        "max_unfolded_solid": 7,
        "auto_bridge_limit": 4,
        "allow_feedback": True,
        "global_fact_char_limit": DEFAULT_GLOBAL_FACT_LIMIT,
    },
    "platform": {
        "preset": "platform",
        "default_topology": "star",
        "max_depth": 3,
        "max_chain_len": 8,
        "max_hub_degree": 8,
        "max_cycle_len": 6,
        "max_active_holes": 4,
        "max_unfolded_solid": 9,
        "auto_bridge_limit": 4,
        "allow_feedback": True,
        "global_fact_char_limit": 4000,
    },
}


class ArchEngineError(Exception):
    """Domain error carrying JIT instructions for the Agent."""
    pass


def _default_config(preset: str = "standard") -> Dict[str, Any]:
    if preset not in PRESETS:
        raise ArchEngineError(
            f"未知 preset '{preset}'。可选预设：{', '.join(PRESETS)}。"
        )
    return dict(PRESETS[preset])


def _load_config() -> Dict[str, Any]:
    """Load `.arch/config.json`, filling any missing keys from its preset defaults."""
    if not CONFIG_FILE.exists():
        return _default_config("standard")
    raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    preset_name = raw.get("preset", "standard")
    base = _default_config(preset_name if preset_name in PRESETS else "standard")
    base.update(raw)
    return base


def _save_config(cfg: Dict[str, Any]) -> None:
    ARCH_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


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


def _shadow_state_targets() -> List[str]:
    return [str(p) for p in SHADOW_STATE_FILES if p.exists()]


def _shadow_commit(tag_name: str, message: str, paths: Optional[List[str]] = None) -> None:
    _init_shadow_git()
    if not paths:
        targets = _shadow_state_targets()
    else:
        targets = []
        for p in paths:
            if Path(p) == ARCH_DIR:
                targets.extend(_shadow_state_targets())
            elif Path(p).exists():
                targets.append(p)
    for p in dict.fromkeys(targets):
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
