"""The thread-safe keyed reactive family (``ThreadSafeReactiveFamily``,
``#lzmatmode`` thread-safe flavor) — the :class:`~lazily.ThreadSafeContext`
analog of :class:`~lazily.ReactiveFamily`.

The Python counterpart of ``lazily-rs/src/thread_safe_reactive_family.rs``, the
Lean ``LazilyFormal.Materialization`` confluence theorems in ``lazily-formal``,
and ``lazily-spec/cell-model.md`` § "Execution-context flavors (thread-safe /
async)".

Keys ``K`` map to per-entry reactive nodes (:class:`~lazily.Cell` inputs /
:class:`~lazily.slot` derived nodes) whose writes are serialized through an
owning :class:`~lazily.ThreadSafeContext`, allocated per the family's
:class:`~lazily.MaterializationMode`. The present-set state is guarded by its own
lock so a keyed family can be materialized concurrently from multiple threads.

The eager/lazy contract, present-set monotonicity, and transparency law are
identical to the single-threaded :class:`~lazily.ReactiveFamily`. What the
thread-safe flavor adds is **materialization confluence** (proved in
``lazily-formal``'s ``Materialization`` module as ``materialize_present_comm`` /
``materialize_observe_comm``): whatever order the lock admits concurrent
materializations in, the present set and every observed value are identical.
:meth:`ThreadSafeReactiveFamily._materialize_key` computes the node **outside**
the family lock, then commits under it **first-writer-wins**, so a raced key
keeps a single stable handle — the confluent commit that makes lock-serialized
concurrent materialization safe.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, TypeVar

from .cell import Cell
from .reactive_family import EntryKind, MaterializationMode
from .slot import slot
from .thread_safe import ThreadSafeContext


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from .reactive_family import FamilyHandle


__all__ = ["ThreadSafeReactiveFamily"]

V = TypeVar("V")


class ThreadSafeReactiveFamily[K, V]:
    """The thread-safe unified keyed reactive family (``#lzmatmode``): keys map to
    per-entry reactive nodes on a :class:`~lazily.ThreadSafeContext`, allocated
    per the family's :class:`~lazily.MaterializationMode`. Present-set state is
    guarded by a lock; materialization is confluent under concurrent access.

    Cell entries are written through the owning context's coalescing
    :meth:`~lazily.ThreadSafeContext.set_cell` / :meth:`~lazily.ThreadSafeContext.batch`
    boundary; expose it via :attr:`context`.
    """

    __slots__ = (
        "_ctx",
        "_entry_kind",
        "_factory",
        "_materialized",
        "_mode",
        "_mutex",
        "_order",
        "_ts",
    )

    def __init__(
        self,
        ctx: dict,
        mode: MaterializationMode,
        keys: Iterable[K],
        factory: Callable[[K], V],
        *,
        entry_kind: EntryKind = EntryKind.SLOT,
        ts: ThreadSafeContext | None = None,
    ) -> None:
        self._ctx = ctx
        self._mode = mode
        self._factory = factory
        self._entry_kind = entry_kind
        self._ts = ts if ts is not None else ThreadSafeContext()
        # (key -> (kind, handle)); present-set order for first-materialization.
        self._materialized: dict[K, tuple[EntryKind, FamilyHandle]] = {}
        self._order: list[K] = []
        # A dedicated present-set lock, separate from the context's write lock, so
        # a slot recompute triggered while committing cannot re-enter it.
        self._mutex = threading.RLock()

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
        ts: ThreadSafeContext | None = None,
    ) -> ThreadSafeReactiveFamily[K, V]:
        """Build an **eager** thread-safe family: every declared key allocated now
        (the default mode)."""
        return cls(
            ctx, MaterializationMode.EAGER, keys, factory, entry_kind=entry_kind, ts=ts
        )

    @classmethod
    def lazy(
        cls,
        ctx: dict,
        keys: Iterable[K],
        factory: Callable[[K], V],
        *,
        entry_kind: EntryKind = EntryKind.SLOT,
        ts: ThreadSafeContext | None = None,
    ) -> ThreadSafeReactiveFamily[K, V]:
        """Build a **lazy** thread-safe family: derived (slot) entries deferred to
        first read; input (cell) entries in ``keys`` are still materialized."""
        return cls(
            ctx, MaterializationMode.LAZY, keys, factory, entry_kind=entry_kind, ts=ts
        )

    @classmethod
    def new(
        cls,
        ctx: dict,
        keys: Iterable[K],
        factory: Callable[[K], V],
        *,
        entry_kind: EntryKind = EntryKind.SLOT,
        ts: ThreadSafeContext | None = None,
    ) -> ThreadSafeReactiveFamily[K, V]:
        """Build a thread-safe family in the **default** mode (eager). Alias for
        :meth:`eager`."""
        return cls.eager(ctx, keys, factory, entry_kind=entry_kind, ts=ts)

    @classmethod
    def cell_family(
        cls,
        ctx: dict,
        keys: Iterable[K],
        factory: Callable[[K], V],
        *,
        mode: MaterializationMode = MaterializationMode.EAGER,
        ts: ThreadSafeContext | None = None,
    ) -> ThreadSafeReactiveFamily[K, V]:
        """Build an **input-cell** thread-safe family (:attr:`EntryKind.CELL`):
        writable inputs, always materialized regardless of ``mode``."""
        return cls(ctx, mode, keys, factory, entry_kind=EntryKind.CELL, ts=ts)

    @classmethod
    def slot_family(
        cls,
        ctx: dict,
        keys: Iterable[K],
        factory: Callable[[K], V],
        *,
        mode: MaterializationMode = MaterializationMode.EAGER,
        ts: ThreadSafeContext | None = None,
    ) -> ThreadSafeReactiveFamily[K, V]:
        """Build a **derived-slot** thread-safe family (:attr:`EntryKind.SLOT`) —
        the entry kind materialization mode governs."""
        return cls(ctx, mode, keys, factory, entry_kind=EntryKind.SLOT, ts=ts)

    # -- internals ------------------------------------------------------ #

    def _materialize_key(self, key: K) -> tuple[EntryKind, FamilyHandle]:
        # Fast path under the present-set lock: return the warm entry if present.
        with self._mutex:
            warm = self._materialized.get(key)
        if warm is not None:
            return warm
        # Build the node OUTSIDE the lock so a slot recompute cannot re-enter it.
        kind = self._entry_kind
        if kind is EntryKind.CELL:
            handle: FamilyHandle = Cell(self._ctx, self._factory(key))
        else:
            handle = slot(lambda _c, k=key: self._factory(k))
        entry = (kind, handle)
        # First-writer-wins commit: on a lost race the freshly-built node is
        # orphaned (unreferenced) and the key keeps its single stable handle.
        with self._mutex:
            existing = self._materialized.get(key)
            if existing is not None:
                return existing
            self._materialized[key] = entry
            self._order.append(key)
            return entry

    # -- reads / writes ------------------------------------------------- #

    def get(self, key: K) -> FamilyHandle:
        """Materialize (the lazy pull) and return the entry handle for ``key``."""
        return self._materialize_key(key)[1]

    def observe(self, key: K) -> V:
        """Observe ``key``'s value — the transparency law: identical under either
        mode, and confluent across concurrent materialization orders. Materializes
        the entry if absent."""
        kind, handle = self._materialize_key(key)
        if kind is EntryKind.CELL:
            return handle.value  # type: ignore[union-attr]
        return handle(self._ctx)  # type: ignore[operator]

    def set_cell(self, key: K, value: V) -> None:
        """Set a cell entry's value through the coalescing context (input entries
        only). Materializes the entry if absent."""
        kind, handle = self._materialize_key(key)
        if kind is not EntryKind.CELL:
            msg = f"key {key!r} is a derived slot, not a writable input cell"
            raise TypeError(msg)
        self._ts.set_cell(handle, value)  # type: ignore[arg-type]

    def is_present(self, key: K) -> bool:
        """Whether ``key`` is currently materialized. Non-reactive."""
        with self._mutex:
            return key in self._materialized

    def present_keys(self) -> list[K]:
        """The currently-materialized keys, in first-materialization order."""
        with self._mutex:
            return list(self._order)

    def present_count(self) -> int:
        """Number of currently-materialized entries."""
        with self._mutex:
            return len(self._order)

    @property
    def context(self) -> ThreadSafeContext:
        """The owning thread-safe context (its coalescing write boundary)."""
        return self._ts

    @property
    def mode(self) -> MaterializationMode:
        """This family's materialization mode."""
        return self._mode

    @property
    def entry_kind(self) -> EntryKind:
        """This family's entry kind."""
        return self._entry_kind
