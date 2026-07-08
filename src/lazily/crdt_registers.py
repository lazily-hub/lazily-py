"""CRDT register types — the value shapes available within ``merge: crdt``.

The Python counterpart of ``lazily-spec/protocol.md`` § "Distributed: CRDT Cell
Plane / Cell register types". These are the CvRDT (state-based, convergent)
register types a multi-write ``merge: crdt`` cell may carry. Each is
commutative, associative, and idempotent under :meth:`merge`, so out-of-order,
duplicated, or batched delivery all converge.

Register kinds:

- :class:`LwwRegister` — last-write-wins by HLC stamp; "current value" semantics;
- :class:`MvRegister` — multi-value register; surfaces concurrent writes as a set;
- :class:`PnCounter` — positive-negative counter (additive);
- :class:`CellCrdt` — a CRDT cell wrapping one of the above and integrating with
  the reactive :class:`lazily.cell.Cell` plane.

A :class:`WireStamp` (``lazily.ipc``) is the decisive stamp for every register.
"""

from __future__ import annotations


__all__ = [
    "CellCrdt",
    "LwwRegister",
    "MvRegister",
    "PnCounter",
]

from dataclasses import dataclass, field
from typing import Any, Protocol

from .cell import Cell
from .ipc import IpcValue, IpcValue_Inline, WireStamp


class _StampOrder(Protocol):
    """Total order on stamps used by CRDT registers (``WireStamp``)."""

    def __lt__(self, other: Any) -> bool: ...
    def __eq__(self, other: Any) -> bool: ...
    def __le__(self, other: Any) -> bool: ...


def _stamp_key(stamp: WireStamp) -> tuple[int, int, int]:
    """The lexicographic ``(wall_time, logical, peer)`` total order on a stamp."""
    return (stamp.wall_time, stamp.logical, stamp.peer)


@dataclass
class LwwRegister[V]:
    """Last-write-wins register — the default "current value" semantics.

    Carries one ``(value, stamp)`` pair. :meth:`merge` keeps the entry with the
    greater stamp under the lexicographic ``(wall_time, logical, peer)`` total
    order (peer id is the final tiebreak, matching the
    ``anti_entropy_converge`` conformance fixture). :meth:`assign` produces a new
    local write at ``stamp``.
    """

    value: V | None = None
    stamp: WireStamp | None = None

    def assign(self, value: V, stamp: WireStamp) -> bool:
        """Local write at ``stamp``. Returns whether it superseded the current."""
        return self._set(value, stamp)

    def _set(self, value: V, stamp: WireStamp) -> bool:
        if self.stamp is None or _stamp_key(stamp) >= _stamp_key(self.stamp):
            self.value = value
            self.stamp = stamp
            return True
        return False

    def merge(self, other: LwwRegister[V]) -> bool:
        """Merge ``other`` into ``self`` (LWW). Returns whether ``self`` changed.

        Commutative, associative, idempotent: merging two registers yields the
        entry with the greater stamp regardless of order.
        """
        if other.stamp is None:
            return False
        if self.stamp is None or _stamp_key(other.stamp) > _stamp_key(self.stamp):
            self.value = other.value
            self.stamp = other.stamp
            return True
        return False

    @staticmethod
    def from_state(state: IpcValue, stamp: WireStamp) -> LwwRegister[bytes]:
        """Reconstruct from an :class:`IpcValue` state + decisive stamp."""
        value: bytes | None = state.data if isinstance(state, IpcValue_Inline) else None
        return LwwRegister(value=value, stamp=stamp)


@dataclass
class MvRegister[V]:
    """Multi-value register — surfaces concurrent writes as a set of values.

    Carries a set of ``(value, stamp)`` entries plus the set of stamps this
    register has **observed** (its causal context). :meth:`write` records the
    currently observed entries into the causal context and replaces the entry
    set with the single new ``(value, stamp)`` (which causally dominates every
    observed entry). :meth:`merge` unions the entry sets and causal contexts,
    then drops any entry whose stamp is in the merged causal context (it was
    observed — causally dominated). The survivors are the causal maxima — the
    concurrent-writes set.
    """

    entries: list[tuple[V, WireStamp]] = field(default_factory=list)
    observed: set[tuple[int, int, int]] = field(default_factory=set)

    def write(self, value: V, stamp: WireStamp) -> bool:
        """A local write at ``stamp`` that supersedes every currently observed
        entry (they go into the causal context)."""
        self.observed = {_stamp_key(s) for _, s in self.entries}
        self.entries = [(value, stamp)]
        return True

    def values(self) -> list[V]:
        """The causal maxima — the concurrent-writes set."""
        return [v for v, _ in self.entries]

    def merge(self, other: MvRegister[V]) -> bool:
        """Merge ``other`` into ``self``. Returns whether ``self`` changed."""
        before = list(self.entries)
        merged_observed = self.observed | other.observed
        candidates: dict[tuple[int, int, int], tuple[V, WireStamp]] = {}
        for value, stamp in [*self.entries, *other.entries]:
            candidates[_stamp_key(stamp)] = (value, stamp)
        kept = [(v, s) for k, (v, s) in candidates.items() if k not in merged_observed]
        kept.sort(key=lambda vs: _stamp_key(vs[1]))
        self.entries = kept
        self.observed = merged_observed
        return self.entries != before


@dataclass
class PnCounter:
    """Positive-negative counter (additive CvRDT).

    A per-peer pair of monotone counters ``(p, n)``; the value is
    ``sum(p) - sum(n)``. :meth:`merge` takes the per-peer ``max`` of each
    counter, so out-of-order or duplicated delivery converges. :meth:`increment`
    / :meth:`decrement` are local writes charged to ``peer``.
    """

    p: dict[int, int] = field(default_factory=dict)
    n: dict[int, int] = field(default_factory=dict)

    def increment(self, peer: int, by: int = 1) -> None:
        if by < 0:
            raise ValueError("increment must be non-negative; use decrement")
        self.p[peer] = self.p.get(peer, 0) + by

    def decrement(self, peer: int, by: int = 1) -> None:
        if by < 0:
            raise ValueError("decrement must be non-negative; use increment")
        self.n[peer] = self.n.get(peer, 0) + by

    def value(self) -> int:
        return sum(self.p.values()) - sum(self.n.values())

    def merge(self, other: PnCounter) -> bool:
        """Per-peer ``max`` merge. Returns whether ``self`` changed."""
        before_p = dict(self.p)
        before_n = dict(self.n)
        for peer, count in other.p.items():
            self.p[peer] = max(self.p.get(peer, 0), count)
        for peer, count in other.n.items():
            self.n[peer] = max(self.n.get(peer, 0), count)
        return self.p != before_p or self.n != before_n


class CellCrdt[V]:
    """A CRDT cell: a register cell that integrates with the reactive plane.

    Wraps one :class:`LwwRegister` (the default register kind; ``MvRegister`` and
    :class:`PnCounter` are addressable by their own classes) and exposes its
    converged value through a reactive :class:`lazily.cell.Cell`. :meth:`merge`
    folds a remote op into the register and, only if the converged value
    changed, propagates the new value into the reactive cell (the local
    PartialEq invalidation guard applies *after* merge, exactly as the spec
    requires).
    """

    __slots__ = ("_cell", "_register")

    def __init__(self, ctx: dict, register: LwwRegister[V] | None = None) -> None:
        self._register: LwwRegister[V] = (
            register if register is not None else LwwRegister()
        )
        seed = self._register.value
        self._cell: Cell[V | None] = Cell(ctx, seed)

    @property
    def cell(self) -> Cell[V | None]:
        """The reactive cell carrying the converged CRDT value."""
        return self._cell

    @property
    def register(self) -> LwwRegister[V]:
        return self._register

    @property
    def value(self) -> V | None:
        return self._cell.value

    def assign(self, value: V, stamp: WireStamp) -> bool:
        """Local write at ``stamp``; propagates into the reactive cell on change."""
        changed = self._register.assign(value, stamp)
        if changed:
            self._cell.set(self._register.value)
        return changed

    def merge(self, other: LwwRegister[V]) -> bool:
        """Merge a remote register into this cell; returns whether it changed."""
        changed = self._register.merge(other)
        if changed:
            self._cell.set(self._register.value)
        return changed

    def merge_op(self, state: IpcValue, stamp: WireStamp) -> bool:
        """Merge one state-based :class:`CrdtOp` payload (inline bytes)."""
        if not isinstance(state, IpcValue_Inline):
            return False
        other = LwwRegister[V](value=state.data, stamp=stamp)  # type: ignore[arg-type]
        return self.merge(other)
