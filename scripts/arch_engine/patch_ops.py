"""`arch patch`: a low-level escape hatch onto the graph kernel.

Applies a list of node / edge operations to one container in a single
transaction. It exists for edits the semantic commands cannot express; it
never creates or reshapes subsystems (use fold / promote / unfold for that)
and always commits in strict mode, so it cannot write a structurally broken
graph whatever ARCH_INVARIANTS says.

    {"op": "add_node",    "id": ..., "label": ..., "spec"?, "status"?, "type"?, "stage"?, "role"?}
    {"op": "update_node", "id": ..., "set": {label | spec | status | type | stage | role: ...}}
    {"op": "remove_node", "id": ...}                      # drops its edges in this container
    {"op": "add_edge",    "from": ..., "to": ..., "kind"?, "contract"?}
    {"op": "update_edge", "from": ..., "to": ..., "contract"?, "kind"?}
    {"op": "remove_edge", "from": ..., "to": ...}
"""

import json
from typing import Any, Dict, List, Union

from .common import ArchEngineError
from .graph import (
    _edge_kind, _reject_manual_super_type, _validate_edge_kind, _validate_node_role,
)
from .kernel import Container, GraphTx
from .views import op_focus

PATCH_OPS = ("add_node", "update_node", "remove_node", "add_edge", "update_edge", "remove_edge")
PATCH_NODE_STATUSES = ("hole", "solid", "fog")
NODE_FIELDS = ("label", "spec", "status", "type", "stage", "role")


def _parse_ops(ops: Union[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    if isinstance(ops, str):
        try:
            ops = json.loads(ops)
        except ValueError as exc:
            raise ArchEngineError(f"patch 操作不是合法 JSON：{exc}") from None
    if isinstance(ops, dict):
        ops = ops.get("ops", [ops]) if "ops" in ops else [ops]
    if not isinstance(ops, list) or not ops:
        raise ArchEngineError("patch 需要非空的操作列表。")
    for i, op in enumerate(ops):
        if not isinstance(op, dict) or op.get("op") not in PATCH_OPS:
            raise ArchEngineError(
                f"第 {i + 1} 个操作无效：{json.dumps(op, ensure_ascii=False)[:120]}。"
                f"op 只能是 {' / '.join(PATCH_OPS)}。"
            )
    return ops


def _node_fields(fields: Dict[str, Any], where: str) -> Dict[str, Any]:
    unknown = sorted(set(fields) - set(NODE_FIELDS))
    if unknown:
        raise ArchEngineError(
            f"{where}：不允许通过 patch 修改字段 {', '.join(unknown)}。可改字段：{' / '.join(NODE_FIELDS)}；"
            "子系统结构请用 fold / promote / unfold，实现文件用 commit-node / set-node-state。"
        )
    if "status" in fields and fields["status"] not in PATCH_NODE_STATUSES:
        raise ArchEngineError(f"{where}：status 只能是 {' / '.join(PATCH_NODE_STATUSES)}。")
    if "type" in fields:
        _reject_manual_super_type(fields["type"])
    if "role" in fields:
        _validate_node_role(fields["role"])
    return fields


def _apply(c: Container, op: Dict[str, Any], where: str) -> str:
    kind = op["op"]
    if kind == "add_node":
        nid = str(op.get("id", "")).strip()
        if not nid or not op.get("label"):
            raise ArchEngineError(f"{where}：add_node 需要 id 与 label。")
        fields = _node_fields({k: v for k, v in op.items() if k not in ("op", "id")}, where)
        node: Dict[str, Any] = {
            "id": nid, "label": fields["label"], "status": fields.get("status", "hole"),
            "type": fields.get("type", "port"), "stage": fields.get("stage", ""),
            "spec": fields.get("spec", ""), "file_path": "", "last_tag": "",
        }
        if fields.get("role"):
            node["role"] = fields["role"]
        c.insert_node(node)
        return f"+ 节点 {nid} ({node['status']})"

    if kind == "update_node":
        node = c.get(str(op.get("id", "")))
        if node["status"] == "super_node":
            raise ArchEngineError(f"{where}：'{node['id']}' 是子系统，patch 不修改子系统节点。")
        fields = _node_fields(dict(op.get("set") or {}), where)
        if not fields:
            raise ArchEngineError(f"{where}：update_node 需要非空的 set。")
        for key, value in fields.items():
            if key == "role" and not value:
                node.pop("role", None)
            else:
                node[key] = value
        return f"~ 节点 {node['id']}：{', '.join(sorted(fields))}"

    if kind == "remove_node":
        nid = str(op.get("id", ""))
        node = c.get(nid)
        if node["status"] == "super_node":
            raise ArchEngineError(f"{where}：'{nid}' 是子系统，请先 unfold 再删除其中的节点。")
        dropped = [e for e in c.edges if nid in (e["from"], e["to"])]
        c.set_edges([e for e in c.edges if nid not in (e["from"], e["to"])])
        c.remove_nodes({nid})
        return f"- 节点 {nid}（连带 {len(dropped)} 条连线）"

    u, v = str(op.get("from", "")), str(op.get("to", ""))
    if kind == "add_edge":
        ekind = _validate_edge_kind(op.get("kind") or "primary")
        if u == v:
            raise ArchEngineError(f"{where}：不支持自环。")
        c.get(u)
        c.get(v)
        between = c.edges_between(u, v)
        if any(_edge_kind(e) == "bidirectional" for e in between):
            raise ArchEngineError(f"{where}：'{u}' 与 '{v}' 之间已有双向连线。")
        if ekind != "bidirectional" and any(e["from"] == u and e["to"] == v for e in between):
            raise ArchEngineError(f"{where}：连线 '{u} -> {v}' 已存在。")
        edge: Dict[str, Any] = {"from": u, "to": v, "contract": op.get("contract") or "TBD"}
        if ekind != "primary":
            edge["kind"] = ekind
        c.add_edge(edge)
        return f"+ 连线 {u} -> {v} ({ekind})"

    found = c.find_link(u, v)
    if not found:
        raise ArchEngineError(f"{where}：连线 '{u} -> {v}' 不存在。")
    edge = found[0]
    if kind == "update_edge":
        new_kind = _validate_edge_kind(op["kind"]) if op.get("kind") else ""
        if "contract" not in op and not new_kind:
            raise ArchEngineError(f"{where}：update_edge 需要 contract 或 kind。")
        outcome = c.retype_link(edge, op.get("contract", edge.get("contract", "TBD")), new_kind)
        suffix = {"merged": "，已合并为一条双向连线", "split": "，已按方向拆分"}.get(outcome, "")
        return f"~ 连线 {u} -> {v}{suffix}"

    c.remove_edge(edge["from"], edge["to"])
    return f"- 连线 {edge['from']} -> {edge['to']}"


def op_patch(ops: Union[str, List[Dict[str, Any]]], scope: str = "") -> str:
    """Apply low-level node / edge operations to one container, all or nothing."""
    parsed = _parse_ops(ops)
    tx = GraphTx()
    c = tx.container(scope)
    feedback_ok = tx.cfg.get("allow_feedback", True)
    lines: List[str] = []
    for i, op in enumerate(parsed):
        where = f"第 {i + 1} 个操作 ({op['op']})"
        if op.get("kind") == "feedback" and not feedback_ok:
            raise ArchEngineError(f"{where}：当前项目 preset 禁止回流边 (allow_feedback=false)。")
        lines.append(_apply(c, op, where))

    tag = f"patch-s{tx.bump():02d}"
    tx.commit(tag, f"patch {len(parsed)} ops" + (f" in {scope}" if scope else ""), strict=True)
    return (
        f"已在{('子系统 ' + scope) if scope else '顶层'}应用 {len(parsed)} 个底层操作，快照 {tag}：\n  "
        + "\n  ".join(lines)
        + "\n\n" + op_focus(scope=scope)
    )
