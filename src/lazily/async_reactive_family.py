"""The async keyed reactive family (``AsyncReactiveFamily``, ``#lzmatmode`` async
flavor) — the async-context analog of :class:`~lazily.ReactiveFamily`.

The Python counterpart of ``lazily-rs/src/async_reactive_family.rs``, the Lean
``LazilyFormal.AsyncMaterialization`` model in ``lazily-formal``, and
``lazily-spec/cell-model.md`` § "Execution-context flavors (thread-safe /
async)".

Keys ``K`` map to per-entry async reactive nodes: :attr:`EntryKind.CELL` input
cells (:class:`~lazily.Cell`, always resolved) or :attr:`EntryKind.SLOT` derived
slots (:class:`~lazily.AsyncSlot`, resolved **asynchronously**), allocated per the
family's :class:`~lazily.MaterializationMode`.

The eager/lazy contract and present-set monotonicity are identical to the
single-threaded family. The transparency law here is **eventual**: a non-blocking
:meth:`AsyncReactiveFamily.observe` of a derived slot returns ``None`` while
pending and the canonical value once resolved — so ``observe`` returns
``V | None``. Input cells are always resolved. Drive a slot to resolution with
:meth:`AsyncReactiveFamily.resolve` (``await``). Once resolved, the observed
value equals what the synchronous family observes; a pending read is never a
stale value (proved in ``lazily-formal``'s ``AsyncMaterialization`` module as
``eventual_transparency`` / ``async_resolved_matches_sync`` /
``observe_pending_is_none``).

To keep the sync/thread-safe/async families API-parallel, the per-key factory is
the same **sync** ``Callable[[K], V]``; a derived slot wraps it in a ready async
recomputation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

from .async_slot import AsyncSlot
from .cell import Cell
from .reactive_family import EntryKind, MaterializationMode


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable


__all__ = ["AsyncReactiveFamily"]

V = TypeVar("V")

# A family entry's async reactive handle: an input ``Cell`` or a derived ``AsyncSlot``.
type AsyncFamilyHandle = Cell | AsyncSlot


class AsyncReactiveFamily[K, V]:
    """The async unified keyed reactive family (``#lzmatmode``): keys map to
    per-entry async reactive nodes (:attr:`EntryKind.CELL` input cells resolved
    synchronously, or :attr:`EntryKind.SLOT` derived slots resolved
    asynchronously), allocated per the family's :class:`~lazily.MaterializationMode`.

    Input cells operate against the owning ``ctx`` dict (as the rest of
    ``lazily`` does); derived slots are :class:`~lazily.AsyncSlot`\\ s driven by
    ``asyncio``. See the module docs for the eventual-transparency contract.
    """

    __slots__ = (
        "_ctx",
        "_entry_kind",
        "_factory",
        "_materialized",
        "_mode",
        "_order",
    )

    def __init__(
        self,
        ctx: dict,
        mode: MaterializationMode,
        keys: Iterable[K],
        factory: Callable[[K], V],
        *,
        entry_kind: EntryKind = EntryKind.SLOT,
    ) -> None:
        self._ctx = ctx
        self._mode = mode
        self._factory = factory
        self._entry_kind = entry_kind
        self._materialized: dict[K, tuple[EntryKind, AsyncFamilyHandle]] = {}
        self._order: list[K] = []

        eager = mode is MaterializationMode.EAGER
        is_cell = entry_kind is EntryKind.CELL
        for key in keys:
            # A cell entry is always materialized regardless of mode; a slot entry
            # only under eager.
            if is_cell or eager:
                self._materialize_key(key)

    # -- constructors --------------------------------------------------- #

    @classmethod
    def eager(
        cls,
        ctx: dict,
        keys: Iterable[K],
        factory: Callable[[K], V],
        *,
        entry_kind: EntryKind = EntryKind.SLOT,
    ) -> AsyncReactiveFamily[K, V]:
        """Build an **eager** async family: every declared key's node is allocated
        now (the default mode)."""
        return cls(ctx, MaterializationMode.EAGER, keys, factory, entry_kind=entry_kind)

    @classmethod
    def lazy(
        cls,
        ctx: dict,
        keys: Iterable[K],
        factory: Callable[[K], V],
        *,
        entry_kind: EntryKind = EntryKind.SLOT,
    ) -> AsyncReactiveFamily[K, V]:
        """Build a **lazy** async family: derived (slot) entries deferred to first
        read; input (cell) entries in ``keys`` are still materialized at build."""
        return cls(ctx, MaterializationMode.LAZY, keys, factory, entry_kind=entry_kind)

    @classmethod
    def new(
        cls,
        ctx: dict,
        keys: Iterable[K],
        factory: Callable[[K], V],
        *,
        entry_kind: EntryKind = EntryKind.SLOT,
    ) -> AsyncReactiveFamily[K, V]:
        """Build an async family in the **default** mode (eager). Alias for
        :meth:`eager`."""
        return cls.eager(ctx, keys, factory, entry_kind=entry_kind)

    @classmethod
    def cell_family(
        cls,
        ctx: dict,
        keys: Iterable[K],
        factory: Callable[[K], V],
        *,
        mode: MaterializationMode = MaterializationMode.EAGER,
    ) -> AsyncReactiveFamily[K, V]:
        """Build an **input-cell** async family (:attr:`EntryKind.CELL` — always
        materialized, always resolved)."""
        return cls(ctx, mode, keys, factory, entry_kind=EntryKind.CELL)

    @classmethod
    def slot_family(
        cls,
        ctx: dict,
        keys: Iterable[K],
        factory: Callable[[K], V],
        *,
        mode: MaterializationMode = MaterializationMode.EAGER,
    ) -> AsyncReactiveFamily[K, V]:
        """Build a **derived-slot** async family (:attr:`EntryKind.SLOT`) — its
        entries resolve asynchronously."""
        return cls(ctx, mode, keys, factory, entry_kind=EntryKind.SLOT)

    # -- internals ------------------------------------------------------ #

    def _materialize_key(self, key: K) -> tuple[EntryKind, AsyncFamilyHandle]:
        existing = self._materialized.get(key)
        if existing is not None:
            return existing  # warm: already allocated (stable handle).
        kind = self._entry_kind
        if kind is EntryKind.CELL:
            # An input cell sets its value directly (always resolved).
            handle: AsyncFamilyHandle = Cell(self._ctx, self._factory(key))
        else:
            # A derived slot wraps the sync factory in a ready async recomputation
            # — the same node an eager build would allocate.
            async def _compute(k: K = key) -> V:
                return self._factory(k)

            handle = AsyncSlot(_compute)
        entry = (kind, handle)
        self._materialized[key] = entry
        self._order.append(key)
        return entry

    # -- reads / writes ------------------------------------------------- #

    def get(self, key: K) -> AsyncFamilyHandle:
        """Materialize (the lazy pull) and return the entry handle for ``key``.
        For a slot family this is the :class:`~lazily.AsyncSlot` to drive with
        :meth:`resolve` (or its own ``get_async``)."""
        return self._materialize_key(key)[1]

    def observe(self, key: K) -> V | None:
        """Non-blocking observe: the resolved value for a cell or a resolved slot,
        or ``None`` for a slot still pending. The **eventual**-transparency law:
        once resolved this equals the canonical value under either mode.
        Materializes the entry if absent."""
        kind, handle = self._materialize_key(key)
        if kind is EntryKind.CELL:
            return handle.value  # type: ignore[union-attr]
        return handle.get()  # type: ignore[union-attr]

    async def resolve(self, key: K) -> V:
        """Drive ``key`` to resolution and return its canonical value. For a cell
        this is immediate; for a slot it awaits the async recomputation.
        Materializes the entry if absent."""
        kind, handle = self._materialize_key(key)
        if kind is EntryKind.CELL:
            return handle.value  # type: ignore[union-attr]
        return await handle.get_async()  # type: ignore[union-attr]

    def set_cell(self, key: K, value: V) -> None:
        """Set a cell entry's value (input entries only). Materializes it if
        absent."""
        kind, handle = self._materialize_key(key)
        if kind is not EntryKind.CELL:
            msg = f"key {key!r} is a derived slot, not a writable input cell"
            raise TypeError(msg)
        handle.set(value)  # type: ignore[union-attr]

    def is_present(self, key: K) -> bool:
        """Whether ``key`` is currently materialized (present). Non-reactive."""
        return key in self._materialized

    def present_keys(self) -> list[K]:
        """The currently-materialized keys, in first-materialization order."""
        return list(self._order)

    def present_count(self) -> int:
        """Number of currently-materialized entries."""
        return len(self._order)

    @property
    def mode(self) -> MaterializationMode:
        """This family's materialization mode."""
        return self._mode

    @property
    def entry_kind(self) -> EntryKind:
        """This family's entry kind."""
        return self._entry_kind
