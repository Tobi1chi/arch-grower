"""Graph kernel: the one place that changes graph structure.

Semantic ops (collapse / fold / deactivate / ...) decide *what* should change
and keep their guardrails and JIT messages; the kernel does the change itself
and keeps the structural rules in one implementation:

    GraphTx     one command = one transaction: load, bump step, validate,
                write side files, write graph.json, Shadow Git snapshot.
    Container   root graph or a super node's body, with the primitives that
                touch node / edge lists (insert, remove, link, split, ...).

Every edge list written through `Container.set_edges` is normalized by
`_merge_bidirectional_pairs`, so "a bidirectional link absorbs every other
edge between its pair" holds no matter which op wrote the edges.

Depends on `common` and `graph` only; ops depend on it.
"""

from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple

from .common import (
    ArchEngineError, _load_config, _load_graph, _shadow_commit, _write_graph,
)
from .graph import (
    _edge_from_members, _edge_kind, _edge_members, _get_node, _iter_all_nodes,
    _merge_bidirectional_pairs, _resolve_container,
)
from .invariants import enforce

Edge = Dict[str, Any]
Node = Dict[str, Any]


def _plain_edge(u: str, v: str, contract: str = "TBD", kind: str = "primary") -> Edge:
    e: Edge = {"from": u, "to": v, "contract": contract}
    if kind and kind != "primary":
        e["kind"] = kind
    return e


def _covers(edges: List[Edge], u: str, v: str) -> bool:
    """An edge `u -> v` already exists, or a bidirectional link covers it."""
    return any(
        (e["from"] == u and e["to"] == v)
        or (e["from"] == v and e["to"] == u and _edge_kind(e) == "bidirectional")
        for e in edges
    )


class Container:
    """Root graph or super node body. Always reads the live lists from `raw`."""

    def __init__(self, tx: "GraphTx", raw: Dict[str, Any], depth: int, path: List[str]) -> None:
        self.tx = tx
        self.raw = raw
        self.depth = depth
        self.path = path            # super node ids from root down to this container

    # -- views ---------------------------------------------------------------
    @property
    def is_root(self) -> bool:
        return self.raw is self.tx.graph

    @property
    def id(self) -> str:
        return "" if self.is_root else self.raw["id"]

    @property
    def nodes(self) -> List[Node]:
        return self.raw["nodes"] if self.is_root else self.raw["children"]

    @property
    def edges(self) -> List[Edge]:
        return self.raw["edges"] if self.is_root else self.raw["inner_edges"]

    def ancestors(self) -> List["Container"]:
        """Containers above this one, nearest first (ends with root)."""
        out = [self.tx.container(pid) for pid in reversed(self.path[:-1])] if self.path else []
        return out + ([] if self.is_root else [self.tx.root])

    def get(self, node_id: str, allow_super: bool = True) -> Node:
        return _get_node(self.raw, node_id, allow_super=allow_super)

    def has(self, node_id: str) -> bool:
        return any(n["id"] == node_id for n in self.nodes)

    def index(self, node_id: str) -> int:
        return next(i for i, n in enumerate(self.nodes) if n["id"] == node_id)

    def subtree_ids(self, node_id: str) -> Set[str]:
        return {n["id"] for n in _iter_all_nodes([self.get(node_id)])}

    def edges_between(self, a: str, b: str) -> List[Edge]:
        return [e for e in self.edges if {e["from"], e["to"]} == {a, b}]

    def covers(self, u: str, v: str) -> bool:
        return _covers(self.edges, u, v)

    def find_link(self, u: str, v: str) -> List[Edge]:
        """Edges stored as `u -> v`, else a bidirectional link stored `v -> u`."""
        exact = [e for e in self.edges if e["from"] == u and e["to"] == v]
        return exact or [
            e for e in self.edges
            if _edge_kind(e) == "bidirectional" and e["from"] == v and e["to"] == u
        ]

    def replace_edge(self, edge: Edge, new_edges: List[Edge]) -> None:
        """Swap one edge (by identity) for `new_edges`, keeping list position."""
        idx = next(i for i, e in enumerate(self.edges) if e is edge)
        self.set_edges(self.edges[:idx] + new_edges + self.edges[idx + 1:])

    def split_by_direction(self, edge: Edge) -> bool:
        """Split a merged link holding flows both ways into one edge per direction.

        Used when a merged bidirectional link is turned one-way. True when split.
        """
        if not edge.get("members"):
            return False
        u, v = edge["from"], edge["to"]
        u_ids = self.subtree_ids(u)
        fwd = [m for m in _edge_members(edge) if m["from"] in u_ids]
        rev = [m for m in _edge_members(edge) if m["from"] not in u_ids]
        if not rev:
            return False
        split = ([_edge_from_members(u, v, fwd)] if fwd else []) + [_edge_from_members(v, u, rev)]
        self.replace_edge(edge, split)
        return True

    # -- node list -----------------------------------------------------------
    def set_nodes(self, nodes: List[Node]) -> None:
        self.raw["nodes" if self.is_root else "children"] = nodes

    def insert_node(self, node: Node, index: Optional[int] = None) -> None:
        """Add a node; its id must be new across the whole graph."""
        if node["id"] in self.tx.all_ids():
            raise ArchEngineError(f"节点 '{node['id']}' 已存在。")
        nodes = self.nodes
        nodes.insert(len(nodes) if index is None else index, node)

    def remove_nodes(self, ids: Set[str]) -> List[Node]:
        """Take nodes out (edges untouched); returns them in their list order."""
        taken = [n for n in self.nodes if n["id"] in ids]
        self.set_nodes([n for n in self.nodes if n["id"] not in ids])
        return taken

    # -- edge list -----------------------------------------------------------
    def set_edges(self, edges: List[Edge]) -> None:
        self.raw["edges" if self.is_root else "inner_edges"] = _merge_bidirectional_pairs(list(edges))

    def add_edge(self, edge: Edge) -> None:
        self.set_edges(self.edges + [edge])

    def remove_edge(self, u: str, v: str) -> Edge:
        idx = next((i for i, e in enumerate(self.edges) if e["from"] == u and e["to"] == v), None)
        if idx is None:
            raise ArchEngineError(f"连线 '{u} -> {v}' 不存在。")
        edges = list(self.edges)
        removed = edges.pop(idx)
        self.set_edges(edges)
        return removed

    def link(self, u: str, v: str, contract: str = "TBD", kind: str = "primary") -> bool:
        """Add `u -> v` unless the pair already covers it; True when added."""
        if self.covers(u, v):
            return False
        self.add_edge(_plain_edge(u, v, contract, kind))
        return True

    def wire_inserted(
        self, node_id: str, after_node: str, before_node: str, contract: str, edge_kind: str,
    ) -> None:
        """Connect a node placed after and/or before existing nodes.

        With both ends given, an existing `after -> before` link is split in
        two. Each half keeps the original members (direction, contract,
        folded provenance); kinds are kept unless `edge_kind` overrides them.
        A feedback member stays feedback only on the half that returns into its
        original target, so the new node sits on the forward path. Edges the
        node already has (a fog branch keeps its edge) are not duplicated.
        """
        if node_id in (after_node, before_node):
            raise ArchEngineError(
                f"节点 '{node_id}' 不能接在自己前后（会形成自环）。\n"
                "JIT 指令：--after / --before 应指向其他已有节点。"
            )
        if after_node and after_node == before_node:
            raise ArchEngineError(
                f"--after 与 --before 不能是同一个节点 '{after_node}'（会形成 {after_node} -> {node_id} -> {after_node} 的往返）。\n"
                f"JIT 指令：挂成旁支只给 --after；需要双向交互时先只给 --after，再用 connect --kind bidirectional。"
            )
        for ref in (after_node, before_node):
            if ref:
                self.get(ref)

        if not (after_node and before_node):
            if after_node:
                self.link(after_node, node_id, contract, edge_kind or "primary")
            elif before_node:
                self.link(node_id, before_node, contract, edge_kind or "primary")
            return

        def is_split(e: Edge) -> bool:
            if e["from"] == after_node and e["to"] == before_node:
                return True
            # A bidirectional link may be stored either way round.
            return e["from"] == before_node and e["to"] == after_node and _edge_kind(e) == "bidirectional"

        old = [e for e in self.edges if is_split(e)]
        kept = [e for e in self.edges if not is_split(e)]
        if not old:
            kind = edge_kind or "primary"
            for u, v in ((after_node, node_id), (node_id, before_node)):
                if not _covers(kept, u, v):
                    kept.append(_plain_edge(u, v, contract, kind))
            self.set_edges(kept)
            return

        # Which side each original endpoint sits on (members may name nodes
        # folded inside `after` / `before`).
        after_ids = self.subtree_ids(after_node)
        inherit_contract = (contract or "TBD").strip() == "TBD"
        halves: Dict[Tuple[str, str], Dict[Tuple[str, str, str], Edge]] = {}
        for oe in old:
            for m in _edge_members(oe):
                mkind = _edge_kind(m)
                forward = m["from"] in after_ids
                if not forward and mkind == "bidirectional":
                    m, forward = {**m, "from": m["to"], "to": m["from"]}, True
                k1 = edge_kind or ("primary" if mkind == "feedback" else mkind)
                k2 = edge_kind or mkind
                c = (m.get("contract", "") if inherit_contract else contract) or "TBD"
                if forward:   # after-side -> X -> before-side
                    parts = (((after_node, node_id), m["from"], node_id, k1),
                             ((node_id, before_node), node_id, m["to"], k2))
                else:         # a merged link's reverse flow: before-side -> X -> after-side
                    parts = (((before_node, node_id), m["from"], node_id, k1),
                             ((node_id, after_node), node_id, m["to"], k2))
                for pair, u, v, k in parts:
                    halves.setdefault(pair, {}).setdefault((u, v, k), _plain_edge(u, v, c, k))
        for (u, v), bucket in halves.items():
            if not _covers(kept, u, v):
                kept.append(_edge_from_members(u, v, list(bucket.values())))
        self.set_edges(kept)


class GraphTx:
    """One command's change to graph.json.

    Nothing is written until `commit`; raising before it leaves every file as
    it was (each command loads a fresh graph, so no copy is needed).
    """

    def __init__(self, graph: Optional[Dict[str, Any]] = None) -> None:
        self.graph: Dict[str, Any] = graph if graph is not None else _load_graph()
        self._cfg: Optional[Dict[str, Any]] = None

    @property
    def cfg(self) -> Dict[str, Any]:
        if self._cfg is None:
            self._cfg = _load_config()
        return self._cfg

    @property
    def step(self) -> int:
        return int(self.graph["step"])

    def bump(self) -> int:
        """Advance the step counter (tags embed it) and return the new value."""
        self.graph["step"] = self.step + 1
        return self.step

    @property
    def root(self) -> Container:
        return Container(self, self.graph, 0, [])

    def container(self, scope: str = "") -> Container:
        if not scope:
            return self.root
        raw, _, _, depth, path = _resolve_container(self.graph, scope)
        return Container(self, raw, depth, path)

    def containers(self) -> Iterator[Container]:
        """Root and every super node body, depth-first."""
        stack: List[Tuple[Dict[str, Any], int, List[str]]] = [(self.graph, 0, [])]
        while stack:
            raw, depth, path = stack.pop()
            c = Container(self, raw, depth, path)
            yield c
            for n in c.nodes:
                if n.get("status") == "super_node":
                    stack.append((n, depth + 1, path + [n["id"]]))

    def all_ids(self) -> Set[str]:
        return {n["id"] for n in _iter_all_nodes(self.graph["nodes"])}

    def find_node(self, node_id: str) -> Optional[Node]:
        return next((n for n in _iter_all_nodes(self.graph["nodes"]) if n["id"] == node_id), None)

    def replace_node_refs(self, old_id: str, new_id: str) -> None:
        """Point every edge endpoint, provenance member and port at `new_id`."""
        def swap(x: str) -> str:
            return new_id if x == old_id else x

        for c in self.containers():
            for e in c.edges:
                e["from"], e["to"] = swap(e["from"]), swap(e["to"])
                if e.get("members"):
                    e["members"] = [{**m, "from": swap(m["from"]), "to": swap(m["to"])} for m in e["members"]]
            raw = c.raw
            for key in ("entry_children", "exit_children"):
                if key in raw:
                    raw[key] = [swap(x) for x in raw[key]]
            for key, ports in (raw.get("parent_ports") or {}).items():
                raw["parent_ports"][key] = [swap(x) for x in ports]

    def commit(
        self,
        tag: str,
        message: str,
        *,
        paths: Optional[List[str]] = None,
        before_write: Optional[Callable[[], None]] = None,
        strict: bool = False,
    ) -> None:
        """Validate, write side files, write graph.json, snapshot.

        `before_write` runs only after the graph passed validation, so a
        rejected change never leaves records and graph out of step.
        `strict` enforces hard invariants whatever ARCH_INVARIANTS says.
        """
        try:
            cfg: Optional[Dict[str, Any]] = self.cfg
        except Exception:
            cfg = None
        enforce(self.graph, cfg, mode="strict" if strict else "")
        if before_write is not None:
            before_write()
        _write_graph(self.graph)
        _shadow_commit(tag, message, paths=paths)
