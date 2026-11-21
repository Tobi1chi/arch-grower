"""arch-grower engine: graph state machine, typed records, context slicing.

Module map (dependencies point downwards only):

    common      paths, limits, ArchEngineError, Shadow Git, graph.json I/O
    graph       topology, folds, walking skeleton, edge provenance, node state
    records     records.json storage, scopes, formatting, rendered md
    refs        `file.md#section` / `file.json#key` resolution
    migration   migrate-records (old md -> typed records)
    views       focus, export-task, facts, status (read-only)
    graph_ops   node and edge operations
    fold_ops    fold / unfold
    record_ops  verify, open items, rescope, legacy, config, ground
    dashboard   export-ui

The public API is the op_* functions below; each returns the text shown to
the agent and raises ArchEngineError with JIT instructions on misuse.
"""

from .common import ArchEngineError
from .dashboard import op_export_ui
from .fold_ops import op_fold, op_unfold
from .graph_ops import (
    op_activate_node, op_add_node, op_collapse, op_commit_node, op_deactivate_node, op_fork,
    op_init, op_reset_node, op_set_edge_contract, op_set_node_state, op_sync_manual,
)
from .migration import op_migrate_records
from .record_ops import (
    op_config, op_ground, op_legacy, op_open_add, op_open_list, op_open_resolve, op_rescope,
    op_verify,
)
from .views import op_export_task, op_facts, op_focus, op_status

__all__ = [
    "ArchEngineError",
    "op_activate_node", "op_add_node", "op_collapse", "op_commit_node", "op_config",
    "op_deactivate_node", "op_export_task", "op_export_ui", "op_facts", "op_focus", "op_fold",
    "op_fork", "op_ground", "op_init", "op_legacy", "op_migrate_records", "op_open_add",
    "op_open_list", "op_open_resolve", "op_rescope", "op_reset_node", "op_set_edge_contract",
    "op_set_node_state", "op_status", "op_sync_manual", "op_unfold", "op_verify",
]
