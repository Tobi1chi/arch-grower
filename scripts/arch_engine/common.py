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
# Use a non-`.git` directory name inside `.arch/` so Git 2.34+ does not treat
# `.arch/` itself as a nested git repository when GIT_WORK_TREE is the project root.
SHADOW_GIT_DIR = ARCH_DIR / "shadow.git"
LEGACY_SHADOW_GIT_DIR = ARCH_DIR / ".git"
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


# Modes
# -----
# `basic` keeps a project on the minimal path: one pipeline, holes, fog,
# grounding and implementation. `advanced` adds star/ring topologies,
# subsystems (fold / promote / unfold), the agent/platform presets and the
# record-maintenance tools (historical graveyard entries, rescoping facts,
# migrating old-format records).
# Ops never check the mode themselves; the CLI and MCP entry points call
# `_require_command` before dispatching.
MODES = ("basic", "advanced")
BASIC_PRESETS = ("standard", "lean")
ADVANCED_COMMANDS = (
    "fold", "promote", "unfold",
    "graveyard", "rescope", "legacy", "migrate-records",
)


def _state_needs_advanced() -> bool:
    """True when the graph or records already rely on advanced-only features."""
    if GRAPH_FILE.exists():
        try:
            graph = json.loads(GRAPH_FILE.read_text(encoding="utf-8"))
        except ValueError:
            graph = {}
        if graph.get("topology", "pipeline") != "pipeline":
            return True

        def nested(nodes: List[Dict[str, Any]]) -> bool:
            return any(n.get("status") == "super_node" or n.get("children") for n in nodes)

        if nested(graph.get("nodes") or []):
            return True
        if not RECORDS_FILE.exists():
            return True   # old md records still need `migrate-records`
    if RECORDS_FILE.exists():
        try:
            rec = json.loads(RECORDS_FILE.read_text(encoding="utf-8"))
        except ValueError:
            rec = {}
        if any(lg.get("status") != "resolved" for lg in rec.get("legacy") or []):
            return True
    return False


def _uses_advanced_features() -> bool:
    """True when the on-disk project (state or config) relies on advanced-only features."""
    if _state_needs_advanced():
        return True
    if CONFIG_FILE.exists():
        try:
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except ValueError:
            raw = {}
        return _mode_for(raw.get("preset", "standard"), raw.get("default_topology", "pipeline")) == "advanced"
    return False


def _mode_for(preset: str, topology: str) -> str:
    """Mode a project needs for this preset and topology."""
    return "basic" if preset in BASIC_PRESETS and topology == "pipeline" else "advanced"


def _project_mode() -> str:
    """`mode` from config.json; projects from before modes are detected from their state."""
    if CONFIG_FILE.exists():
        try:
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except ValueError:
            raw = {}
        if raw.get("mode") in MODES:
            return raw["mode"]
    return "advanced" if _uses_advanced_features() else "basic"


def _advanced_hint(what: str) -> str:
    return (
        f"[ADVANCED_MODE_REQUIRED] {what} 属于 advanced 模式，当前项目处于 basic 模式（只走流水线最小路径）。"
        "JIT 指令：先确认最小路径确实不够用，并征得用户同意后运行 arch config --mode advanced"
        "（MCP：arch_config mode=advanced），再重试。"
    )


def _require_command(command: str) -> None:
    """Refuse an advanced-only CLI command (or its MCP tool) while the project is basic."""
    if command in ADVANCED_COMMANDS and _project_mode() == "basic":
        raise ArchEngineError(_advanced_hint(f"`{command}`"))


def _default_config(preset: str = "standard") -> Dict[str, Any]:
    if preset not in PRESETS:
        raise ArchEngineError(
            f"未知 preset '{preset}'。可选预设：{', '.join(PRESETS)}。"
        )
    return dict(PRESETS[preset])


def _load_config() -> Dict[str, Any]:
    """Load `.arch/config.json`, filling any missing keys from its preset defaults."""
    if not CONFIG_FILE.exists():
        base = _default_config("standard")
        # Projects created before config.json kept their global fact budget in
        # records.json; carry it over so the first `config` write keeps it.
        if RECORDS_FILE.exists():
            try:
                settings = json.loads(RECORDS_FILE.read_text(encoding="utf-8")).get("settings") or {}
                legacy_limit = int(settings.get("global_fact_char_limit") or 0)
            except (ValueError, TypeError, AttributeError):
                legacy_limit = 0
            if legacy_limit > 0:
                base["global_fact_char_limit"] = legacy_limit
        base["mode"] = "advanced" if _uses_advanced_features() else "basic"
        return base
    raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    preset_name = raw.get("preset", "standard")
    base = _default_config(preset_name if preset_name in PRESETS else "standard")
    base.update(raw)
    if base.get("mode") not in MODES:
        base["mode"] = "advanced" if _uses_advanced_features() else "basic"
    return base


def _save_config(cfg: Dict[str, Any]) -> None:
    ARCH_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _migrate_legacy_shadow_git() -> None:
    """Move legacy `.arch/.git` to `.arch/shadow.git` so Git 2.34+ can stage `.arch/*` files."""
    if LEGACY_SHADOW_GIT_DIR.exists() and not SHADOW_GIT_DIR.exists():
        LEGACY_SHADOW_GIT_DIR.rename(SHADOW_GIT_DIR)


def _shadow_git_exists() -> bool:
    _migrate_legacy_shadow_git()
    return SHADOW_GIT_DIR.exists()


def _run_shadow_git(args: List[str], check: bool = True) -> subprocess.CompletedProcess:
    _migrate_legacy_shadow_git()
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


def _ensure_shadow_gitignore() -> None:
    """Keep `.arch/shadow.git/` out of the host project's main Git repo."""
    gi = ARCH_DIR / ".gitignore"
    existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
    lines = [ln.strip() for ln in existing.splitlines()]
    if "shadow.git/" not in lines:
        sep = "" if (not existing or existing.endswith("\n")) else "\n"
        gi.write_text(existing + sep + "shadow.git/\n", encoding="utf-8")


def _init_shadow_git() -> None:
    _migrate_legacy_shadow_git()
    ARCH_DIR.mkdir(parents=True, exist_ok=True)
    _ensure_shadow_gitignore()
    if not SHADOW_GIT_DIR.exists():
        SHADOW_GIT_DIR.mkdir(parents=True, exist_ok=True)
        _run_shadow_git(["init"])
        _run_shadow_git(["config", "core.bare", "false"])
        _run_shadow_git(["config", "user.name", "arch-grower"])
        _run_shadow_git(["config", "user.email", "arch@local"])


def _shadow_state_targets() -> List[str]:
    return [str(p) for p in SHADOW_STATE_FILES if p.exists()]


def _tracked_arch_extras(rev: str = "") -> List[str]:
    """`.arch/` paths tracked by Shadow Git that are not state files.

    Snapshots made before the state-file whitelist staged the whole `.arch/`
    directory (e.g. `dashboard.html`). With `rev` the paths come from that
    commit, otherwise from the index.
    """
    arch = ARCH_DIR.as_posix()
    if rev:
        res = _run_shadow_git(["ls-tree", "-r", "--name-only", rev, "--", arch], check=False)
    else:
        res = _run_shadow_git(["ls-files", "--", arch], check=False)
    if res.returncode != 0:
        return []
    keep = {p.as_posix() for p in SHADOW_STATE_FILES}
    return [ln for ln in res.stdout.splitlines() if ln.strip() and ln not in keep]


def _shadow_commit(tag_name: str, message: str, paths: Optional[List[str]] = None) -> None:
    _init_shadow_git()
    # Drop legacy non-state `.arch/` files from the index so later exports of
    # them never leave a tracked-but-modified file that blocks `fork`.
    legacy = _tracked_arch_extras()
    if legacy:
        _run_shadow_git(["rm", "--cached", "-q", "--ignore-unmatch", "--", *legacy], check=False)
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
            add_res = _run_shadow_git(["add", "-f", "--", p], check=False)
            if add_res.returncode != 0:
                raise ArchEngineError(
                    f"Shadow Git 暂存 '{p}' 失败：{add_res.stderr.strip() or add_res.stdout.strip()}"
                )
    res = _run_shadow_git(["diff", "--cached", "--quiet"], check=False)
    if res.returncode != 0:
        com_res = _run_shadow_git(["commit", "-m", message], check=False)
        if com_res.returncode != 0:
            raise ArchEngineError(
                f"Shadow Git 提交快照失败：{com_res.stderr.strip() or com_res.stdout.strip()}"
            )
    head_res = _run_shadow_git(["rev-parse", "--verify", "HEAD"], check=False)
    if head_res.returncode == 0:
        tag_res = _run_shadow_git(["tag", "-f", tag_name], check=False)
        if tag_res.returncode != 0:
            raise ArchEngineError(
                f"Shadow Git 创建标签 '{tag_name}' 失败：{tag_res.stderr.strip() or tag_res.stdout.strip()}"
            )


def _load_graph() -> Dict[str, Any]:
    if not GRAPH_FILE.exists():
        raise ArchEngineError(
            "未找到 .arch/graph.json。请先调用 arch_init 初始化项目骨架。"
        )
    return json.loads(GRAPH_FILE.read_text(encoding="utf-8"))


def _write_graph(data: Dict[str, Any]) -> None:
    """Write graph.json as is. Callers must have validated it (see kernel.GraphTx)."""
    GRAPH_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _load_config_if_any() -> Optional[Dict[str, Any]]:
    try:
        return _load_config() if CONFIG_FILE.exists() else None
    except Exception:
        return None


def _save_graph(data: Dict[str, Any]) -> None:
    """Validate (per ARCH_INVARIANTS) and write graph.json."""
    from .invariants import enforce  # lazy: invariants imports this module

    enforce(data, _load_config_if_any())
    _write_graph(data)


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _graph_if_any() -> Optional[Dict[str, Any]]:
    return _load_graph() if GRAPH_FILE.exists() else None
