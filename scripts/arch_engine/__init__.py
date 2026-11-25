"""arch-grower engine: graph state machine, multi-topology subsystems, project
presets, typed records, context slicing.

Module map (dependencies point downwards only):

    common      paths, presets, config.json, ArchEngineError, Shadow Git, graph.json I/O
    invariants  whole-graph structural checks run before graph.json is written (ARCH_INVARIANTS)
    graph       multi-topology containers, folds, walking skeleton, edge provenance, node state
    records     records.json storage, scopes, formatting, rendered md
    refs        `file.md#section` / `file.json#key` resolution
    migration   migrate-records (old md -> verbatim legacy chunks)
    kernel      GraphTx (one command = one validated write + snapshot) and Container primitives;
                the only code that changes node / edge lists
    views       focus, export-task, facts, status (read-only)
    graph_ops   init, bootstrap, node and edge operations (connect / disconnect / ...)
    fold_ops    fold / unfold / promote
    record_ops  verify, graveyard add, open items, rescope, legacy, config, ground
    dashboard   export-ui

The public API is the op_* functions below; each returns the text shown to
the agent and raises ArchEngineError with JIT instructions on misuse.
"""

from .common import ArchEngineError, ENGINE_VERSION, PRESETS, TOPOLOGIES, EDGE_KINDS, NODE_ROLES
from .dashboard import op_export_ui
from .fold_ops import op_fold, op_promote, op_unfold
from .graph_ops import (
    op_activate_node, op_add_node, op_bootstrap, op_collapse, op_commit_node, op_connect,
    op_deactivate_node, op_disconnect, op_fork, op_init, op_reset_node, op_set_edge_contract,
    op_set_node_state, op_sync_manual,
)
from .migration import op_migrate_records
from .record_ops import (
    op_config, op_graveyard_add, op_ground, op_legacy, op_open_add, op_open_list,
    op_open_resolve, op_rescope, op_verify,
)
from .views import op_export_task, op_facts, op_focus, op_status

__all__ = [
    "ArchEngineError", "ENGINE_VERSION", "PRESETS", "TOPOLOGIES", "EDGE_KINDS", "NODE_ROLES",
    "op_activate_node", "op_add_node", "op_bootstrap", "op_collapse", "op_commit_node", "op_config",
    "op_connect", "op_deactivate_node", "op_disconnect", "op_export_task", "op_export_ui",
    "op_facts", "op_focus", "op_fold", "op_fork", "op_graveyard_add", "op_ground", "op_init",
    "op_legacy", "op_migrate_records", "op_open_add", "op_open_list", "op_open_resolve",
    "op_promote", "op_rescope", "op_reset_node", "op_set_edge_contract", "op_set_node_state",
    "op_status", "op_sync_manual", "op_unfold", "op_verify",
]
