"""Keyed reactive collections — ``CellMap`` and ``CellFamily``.

The Python counterpart of the Lean ``LazilyFormal.Collection`` formal model in
``lazily-formal`` and ``lazily-spec/cell-model.md`` § "Keyed cell collections".
A ``CellMap`` is a hash collection whose **membership is itself reactive**, with
one independently-tracked value cell per entry; ``CellFamily`` layers a value
factory on top that lazily mints and caches one cell per key.

Three independent reactive signals are exposed, mirroring ``lazily-rs``'s
``membership`` and ``order_signal`` cells:

- **per-entry value** — one ``Cell[V]`` per key (read via :meth:`CellMap.value_cell`);
- **set-membership** — :meth:`CellMap.membership_signal` (a ``Cell[int]`` bumped
  on add/remove only; ``len``/``contains`` readers subscribe here);
- **order** — :meth:`CellMap.order_signal` (a ``Cell[int]`` bumped on add/remove
  *and* on move; ``keys`` readers subscribe here).

The independence laws fixed by the formal model are observable here: a pure
reorder (:meth:`CellMap.move_to`) bumps the order signal only — membership and
every entry's value cell are untouched, so a ``len``/``contains`` reader is not
invalidated (the wire-level "a pure reorder MUST NOT invalidate
set-membership readers" invariant). An atomic move keeps each entry's cell
identity (not remove + re-mint).
"""

from __future__ import annotations


__all__ = ["CellFamily", "CellMap"]

from typing import TYPE_CHECKING, TypeVar

from .cell import Cell


if TYPE_CHECKING:
    from collections.abc import Callable


K = TypeVar("K")
V = TypeVar("V")


class CellMap[K, V]:
    """A keyed reactive collection — ``CellMap<K, V>``.

    Membership is reactive: reading :meth:`len` or :meth:`contains` inside a
    Slot/Signal subscribes to the membership signal; reading :meth:`keys`
    subscribes to the order signal; reading one entry's value subscribes to
    that entry's value cell alone. Editing one entry's value invalidates only
    that entry's readers — never a sibling or a membership/order reader.

    Mirrors ``lazily-rs/src/cell_family.rs`` and the Lean
    ``LazilyFormal.Collection`` model.
    """

    __slots__ = ("_membership_signal", "_order", "_order_signal", "_value_cells", "ctx")

    def __init__(self, ctx: dict) -> None:
        self.ctx = ctx
        self._value_cells: dict[K, Cell[V]] = {}
        self._order: list[K] = []
        self._membership_signal: Cell[int] = Cell(ctx, 0)
        self._order_signal: Cell[int] = Cell(ctx, 0)

    # -- signals -------------------------------------------------------- #

    @property
    def membership_signal(self) -> Cell[int]:
        """The set-membership version signal (bumps on add/remove only)."""
        return self._membership_signal

    @property
    def order_signal(self) -> Cell[int]:
        """The order version signal (bumps on add/remove *and* move)."""
        return self._order_signal

    @property
    def order(self) -> list[K]:
        """The authoritative insertion-ordered key list."""
        return list(self._order)

    # -- reads ---------------------------------------------------------- #

    def __len__(self) -> int:
        # Touch the membership signal so a len() reader is invalidated on
        # add/remove but NOT on a pure reorder (move_to).
        _ = self._membership_signal.value
        return len(self._order)

    def __contains__(self, key: object) -> bool:
        _ = self._membership_signal.value
        return key in self._value_cells

    def keys(self) -> list[K]:
        _ = self._order_signal.value
        return list(self._order)

    def value_cell(self, key: K) -> Cell[V] | None:
        return self._value_cells.get(key)

    def get(self, key: K) -> V | None:
        cell = self._value_cells.get(key)
        return cell.value if cell is not None else None

    # -- mutators ------------------------------------------------------- #

    def set_value(self, key: K, value: V) -> None:
        """Update the value cell at ``key`` (which must be a member). Leaves the
        membership and order signals untouched — only this entry's value readers
        are invalidated. Mirrors ``setEntryValue``."""
        cell = self._value_cells.get(key)
        if cell is not None:
            cell.set(value)

    def insert(self, key: K, value: V) -> None:
        """Insert ``key`` as a new member at the end, minting its value cell.
        Bumps both the membership and the order signal. (No-op if ``key`` is
        already a member.) Mirrors ``addKey``."""
        if key in self._value_cells:
            return
        self._value_cells[key] = Cell(self.ctx, value)
        self._order.append(key)
        self._membership_signal.set(self._membership_signal.value + 1)
        self._order_signal.set(self._order_signal.value + 1)

    def remove(self, key: K) -> None:
        """Remove ``key`` from the collection. Bumps both the membership and the
        order signal. (No-op if ``key`` is not a member.) Mirrors ``removeKey``."""
        if key not in self._value_cells:
            return
        del self._value_cells[key]
        self._order = [k for k in self._order if k != key]
        self._membership_signal.set(self._membership_signal.value + 1)
        self._order_signal.set(self._order_signal.value + 1)

    def move_to(self, key: K, index: int) -> None:
        """A pure reorder: move ``key`` to position ``index``. Bumps **only** the
        order signal; membership and every entry's value cell are untouched.
        This is the formal counterpart of ``lazily-rs``'s ``CellMap::move_to``
        (``#lzcellmove``). Mirrors ``moveKey``."""
        if key not in self._value_cells:
            return
        self._order = [k for k in self._order if k != key]
        clamped = min(index, len(self._order))
        self._order.insert(clamped, key)
        self._order_signal.set(self._order_signal.value + 1)

    def move_before(self, key: K, before: K) -> None:
        """Move ``key`` to immediately precede ``before`` (a pure reorder)."""
        if before not in self._value_cells or key not in self._value_cells:
            return
        self._order = [k for k in self._order if k != key]
        pos = self._order.index(before)
        self._order.insert(pos, key)
        self._order_signal.set(self._order_signal.value + 1)

    def move_after(self, key: K, after: K) -> None:
        """Move ``key`` to immediately follow ``after`` (a pure reorder)."""
        if after not in self._value_cells or key not in self._value_cells:
            return
        self._order = [k for k in self._order if k != key]
        pos = self._order.index(after) + 1
        self._order.insert(pos, key)
        self._order_signal.set(self._order_signal.value + 1)


class CellFamily[K, V]:
    """``CellFamily`` — a ``CellMap`` plus a per-key factory that lazily mints
    and caches one cell per key on first access.

    Mirrors ``lazily-rs/src/cell_family.rs`` (``CellFamily``). The universal
    guarantee is identity stability: the same key resolves to the same value
    cell across requests (:meth:`CellFamily.get` is idempotent after first
    access).
    """

    __slots__ = ("_coll", "_factory", "_minted")

    def __init__(
        self, coll: CellMap[K, V], factory: Callable[[K], V] | None = None
    ) -> None:
        self._coll = coll
        self._factory = factory
        self._minted: set[K] = set()

    @property
    def collection(self) -> CellMap[K, V]:
        return self._coll

    def get(self, key: K, value: V) -> Cell[V]:
        """The lazy mint: if ``key`` has already been minted, return its
        existing cell (identity-stable handle); otherwise mint it (via the
        factory if one was supplied, else ``value``) and record it. Mirrors
        ``Family.get`` — which always carries the entry value ``v``."""
        if key in self._minted:
            return self._coll._value_cells[key]
        minted = self._factory(key) if self._factory is not None else value
        self._coll.insert(key, minted)
        self._minted.add(key)
        return self._coll._value_cells[key]

    def is_minted(self, key: K) -> bool:
        return key in self._minted
