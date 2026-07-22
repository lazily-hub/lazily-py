"""Phase 1 of the RelayCell backpressure plan (#relaycell) — the merge algebra.

See ``lazily-spec/docs/reactive-graph.md`` § "MergeCell and the merge algebra"
and ``relaycell-backpressure-analysis.md`` §4.0/§4.3. A merge policy is an
*associative* fold ``⊕: T*T->T``; the properties it satisfies (associativity
always; commutativity = reordering tax; idempotency = durability tax) select
which overflow behaviour is sound. ``MergeCell`` generalizes a plain ``Cell`` —
``Cell ≡ MergeCell(KeepLatest)`` — a source whose write is a merge. Backed by an
ordinary cell, so it inherits the Phase-0 ``!=`` store-guard + store-without-cascade.
"""

from __future__ import annotations


__all__ = [
    "KeepLatest",
    "Max",
    "MergeCell",
    "MergePolicy",
    "RawFifo",
    "SetUnion",
    "Sum",
    "merge_cell",
]

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .cell import Cell


if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True)
class MergePolicy[T]:
    """An associative merge ``⊕`` with its transport-selected property flags.

    Associativity (``(a⊕b)⊕c == a⊕(b⊕c)``) is a law, verified by the law-tests,
    not a flag. ``commutative`` is the reordering tax; ``idempotent`` the
    durability tax; ``conflates`` gates the ``Conflate`` overflow (Phase 2 — only
    ``RawFifo`` cannot bound).
    """

    name: str
    merge: Callable[[T, T], T]
    commutative: bool
    idempotent: bool
    conflates: bool = True


# -- Canonical policies ------------------------------------------------------

#: Keep-latest band (``old ⊕ op = op``) — the policy behind a plain ``Cell``.
KeepLatest: MergePolicy = MergePolicy(
    "KeepLatest", lambda _old, op: op, commutative=False, idempotent=True
)

#: Additive commutative monoid (``old + op``). Not idempotent.
Sum: MergePolicy = MergePolicy(
    "Sum", lambda old, op: old + op, commutative=True, idempotent=False
)

#: Max semilattice (``max(old, op)``). Associative, commutative, idempotent.
Max: MergePolicy = MergePolicy(
    "Max", lambda old, op: op if op > old else old, commutative=True, idempotent=True
)

#: Grow-only set-union semilattice over ``set``/``frozenset``.
SetUnion: MergePolicy = MergePolicy(
    "SetUnion", lambda old, op: old | op, commutative=True, idempotent=True
)

#: Raw FIFO append over ``list`` (``old ++ op``). Order + multiplicity are
#: meaning — associative only; cannot conflate.
RawFifo: MergePolicy = MergePolicy(
    "RawFifo",
    lambda old, op: old + op,
    commutative=False,
    idempotent=False,
    conflates=False,
)


class MergeCell[T]:
    """A cell whose write is a *merge* under ``policy`` rather than a replace.

    ``Cell ≡ MergeCell(KeepLatest)``. Reads track like any cell; ``merge`` routes
    through the cell's ``!=``-guarded setter, so an idempotent policy's no-op
    merge fires no cascade (free dedup) and store-without-cascade still applies.
    """

    __slots__ = ("_cell", "_policy")

    def __init__(self, ctx: dict, initial: T, policy: MergePolicy[T]) -> None:
        self._cell: Cell[T] = Cell(ctx, initial)
        self._policy = policy

    @property
    def cell(self) -> Cell[T]:
        """The underlying reactive cell (for wiring derived readers)."""
        return self._cell

    @property
    def policy(self) -> MergePolicy[T]:
        return self._policy

    def get(self, ctx: Any = None) -> T:
        """Read the current converged value.

        Pass the caller's :class:`~lazily.compute.Compute` view (``ctx``) to
        value-thread the dependency edge when reading inside a reactive body;
        omit it for an untracked top-level read (``#lzcellkernel`` bare-read
        removal)."""
        if ctx is None:
            return self._cell.get()
        return ctx.read(self._cell)

    def set(self, value: T) -> None:
        """Replace the value outright (the keep-latest write), bypassing the policy."""
        self._cell.set(value)

    def merge(self, op: T) -> None:
        """Fold ``op`` into the current value under the policy."""
        self._cell.set(self._policy.merge(self._cell.get(), op))


def merge_cell[T](ctx: dict, initial: T, policy: MergePolicy[T]) -> MergeCell[T]:
    """Create a :class:`MergeCell` over ``ctx``."""
    return MergeCell(ctx, initial, policy)
