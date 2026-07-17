"""Memoized semantic tree — ``SemTree``.

The Python counterpart of ``lazily-spec/cell-model.md`` § "Memoized semantic
tree". The syntactic tree holds input cells; a **semantic** tree (unresolved
prompts, drainable heads, summaries) is a layer of **memoized computeds**
derived from it — one memo slot per node folding ``(node value, child derived
values)``.

It conforms when the derivation is incremental and glitch-free:

- editing one node recomputes only its **ancestor chain** — a sibling subtree's
  derived slot stays cached;
- a node edit that does not change the folded result MUST NOT re-run a downstream
  consumer (the memo equality guard); and
- cost is proportional to the diff, not the document.

Built on :mod:`lazily.cell` / :mod:`lazily.tree` plus the memo guard that a
memoized fold carries. The executable reference behind the
``lazily-spec/conformance/collections/semtree_incremental.json`` fixture.
"""

from __future__ import annotations


__all__ = [
    "Fold",
    "SemNode",
    "SemTree",
    "count_positive_fold",
    "sum_fold",
]

from collections.abc import Callable
from typing import Any, Protocol

from .cell import Cell


# A fold takes (node_value, [child derived values]) and produces the node's
# derived value. Pure: identical inputs produce identical outputs so the memo
# guard can suppress equal recomputes.
Fold = Callable[[Any, list[Any]], Any]


def sum_fold(value: Any, child_values: list[Any]) -> Any:
    """Sum numeric node values with their children's derived values."""
    total: Any = value
    for cv in child_values:
        total = total + cv
    return total


def count_positive_fold(value: Any, child_values: list[Any]) -> Any:
    """Count the positive values in a subtree (``value > 0`` plus children)."""
    count = 1 if value > 0 else 0
    for cv in child_values:
        count += cv
    return count


_FOLDS: dict[str, Fold] = {
    "sum": sum_fold,
    "count_positive": count_positive_fold,
}


def _fold_named(name: str) -> Fold:
    fold = _FOLDS.get(name)
    if fold is None:
        raise ValueError(f"unknown fold {name!r}")
    return fold


class _IdLike(Protocol):
    def __hash__(self) -> int: ...
    def __eq__(self, self_other: object) -> bool: ...


class SemNode[IdLike, V]:
    """One semantic-tree node: a value ``Cell`` plus a memoized derived value.

    The derived value folds ``(node value, child derived values)``. Recomputation
    counters (``compute_count`` / ``downstream_count``) make the
    ancestor-chain-only and memo-guard invariants observable to the conformance
    fixture:

    - ``compute_count`` ticks once per real recompute of this node;
    - ``downstream_count`` ticks only when the folded value actually changed
      (the memo-guarded propagation).
    """

    __slots__ = (
        "_dirty",
        "children",
        "compute_count",
        "derived",
        "downstream_count",
        "id",
        "value_cell",
    )

    def __init__(self, node_id: _IdLike, value: V) -> None:
        self.id = node_id
        self.value_cell: Cell[V] = Cell({}, value)
        self.children: list[_IdLike] = []
        self.compute_count = 0
        self.downstream_count = 0
        # Cached derived value + dirty flag. ``dirty=False`` means "cached."
        self.derived: Any = None
        self._dirty = True

    @property
    def value(self) -> V:
        return self.value_cell.value

    def _mark_dirty_chain(self, parents: dict, visited: set) -> None:
        """Mark this node and its ancestors dirty (the ancestor chain).

        Walks the per-child parent index (``#lzpysemtreeparents``) — O(depth),
        not O(depth x N) over the whole node table."""
        if self.id in visited:
            return
        visited.add(self.id)
        self._dirty = True
        for parent in parents.get(self.id, ()):
            parent._mark_dirty_chain(parents, visited)


class SemTree[IdLike, V]:
    """A memoized semantic tree: nodes keyed by stable id.

    Each node carries an input value cell and a memoized derived value folding
    ``(node value, child derived values)``. The derivation is incremental and
    glitch-free: editing one node recomputes only its ancestor chain, and a
    node edit that does not change the folded result MUST NOT re-run a
    downstream consumer.

    Mirrors the runtime substrate described in
    ``lazily-spec/cell-model.md § Memoized semantic tree``.
    """

    __slots__ = ("_fold", "_nodes", "_parents")

    def __init__(self, fold: Fold | str = "sum") -> None:
        self._fold: Fold = _fold_named(fold) if isinstance(fold, str) else fold
        self._nodes: dict[_IdLike, SemNode[_IdLike, V]] = {}
        # Per-child parent index (``#lzpysemtreeparents``) so a dirty-chain walk
        # is O(depth) instead of O(depth x N).
        self._parents: dict[_IdLike, list[SemNode[_IdLike, V]]] = {}

    # -- reads ---------------------------------------------------------- #

    def __contains__(self, node_id: object) -> bool:
        return node_id in self._nodes

    @property
    def fold(self) -> Fold:
        return self._fold

    def node(self, node_id: _IdLike) -> SemNode[_IdLike, V] | None:
        return self._nodes.get(node_id)

    def value_of(self, node_id: _IdLike) -> V | None:
        n = self._nodes.get(node_id)
        return n.value_cell.value if n is not None else None

    def derived(self, node_id: _IdLike) -> Any:
        """Read (and cache) the derived value at ``node_id``.

        Recomputes only when this node is dirty. Because a value edit marks only
        the ancestor chain dirty, a sibling subtree's derived value stays cached
        — the "proportional to the diff, not the document" invariant.
        """
        n = self._nodes.get(node_id)
        if n is None:
            raise KeyError(node_id)
        if n._dirty:
            child_values = [self.derived(cid) for cid in n.children]
            folded = self._fold(n.value_cell.value, child_values)
            prev = n.derived
            n.compute_count += 1
            if prev != folded:
                n.downstream_count += 1
            n.derived = folded
            n._dirty = False
        return n.derived

    def children_of(self, node_id: _IdLike) -> list[_IdLike]:
        n = self._nodes.get(node_id)
        return list(n.children) if n is not None else []

    # -- mutators ------------------------------------------------------- #

    def add(self, node_id: _IdLike, value: V) -> SemNode[_IdLike, V]:
        """Add a node (minting its value cell). Idempotent."""
        existing = self._nodes.get(node_id)
        if existing is not None:
            return existing
        node: SemNode[_IdLike, V] = SemNode(node_id, value)
        self._nodes[node_id] = node
        return node

    def set_node_value(self, node_id: _IdLike, value: V) -> None:
        """Edit one node's value. Only its ancestor chain recomputes."""
        n = self._nodes.get(node_id)
        if n is None:
            return
        if n.value_cell.value == value:
            return
        n.value_cell.set(value)
        n._mark_dirty_chain(self._parents, set())

    def insert_child(self, parent: _IdLike, child: _IdLike, value: V) -> None:
        p = self._nodes.get(parent)
        if p is None:
            return
        if child not in self._nodes:
            self.add(child, value)
        if child in p.children:
            return
        p.children.append(child)
        # Maintain the parent index (``#lzpysemtreeparents``).
        self._parents.setdefault(child, []).append(p)
        # A structural change dirties the parent's chain (its fold input set).
        p._mark_dirty_chain(self._parents, set())

    def remove_child(self, parent: _IdLike, child: _IdLike) -> None:
        """Detach ``child`` from ``parent``'s child collection.

        The child's derived value drops out of the parent's fold on the next
        read, which recomputes only the ancestor chain.
        """
        p = self._nodes.get(parent)
        if p is None:
            return
        if child not in p.children:
            return
        p.children = [c for c in p.children if c != child]
        # Maintain the parent index (``#lzpysemtreeparents``) — drop this parent.
        self._parents[child] = [
            pp for pp in self._parents.get(child, []) if pp is not p
        ]
        p._mark_dirty_chain(self._parents, set())

    # -- JSON builder (conformance fixture shape) ----------------------- #

    @classmethod
    def from_json(cls, tree: dict, fold: Fold | str = "sum") -> SemTree[Any, Any]:
        """Build a tree from the conformance-fixture JSON shape.

        Fixture shape::

            {
                "id": "root",
                "value": 0,
                "children": {
                    "order": ["a", "b"],
                    "values": {"a": {"id": "a", "value": 1, "children": {...}}},
                },
            }
        """
        obj = cls(fold=fold)

        def visit(node_json: dict) -> Any:
            node_id = node_json["id"]
            value = node_json.get("value")
            obj.add(node_id, value)
            children = node_json.get("children")
            if children:
                for cid in children.get("order", []):
                    child_json = children["values"][cid]
                    visit(child_json)
                    obj.insert_child(node_id, cid, child_json["value"])
            return node_id

        visit(tree)
        return obj
