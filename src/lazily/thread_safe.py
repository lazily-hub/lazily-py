"""Thread-safe reactive context — a lock-serialized ``batch`` boundary.

The Python counterpart of the Lean ``LazilyFormal.ThreadSafe`` formal model in
``lazily-formal`` and ``lazily-spec/protocol.md`` § "Concurrency layers are
required". The behavioral contract this module fixes: serializing concurrent
cell writes through a ``batch`` boundary coalesces them into one invalidation
pass whose result is a deterministic function of the writes — independent of the
interleaving the lock happened to pick. This is the formal core of the spec's
"Coalesced frontier: a dependent reached through many changed cells in one batch
appears at most once per delta" invariant, lifted from the wire to the reactive
graph.

The lock is an ordinary ``threading.RLock``. ``batch(run)`` queues cell writes
under the lock and flushes them once at the outermost boundary — so multiple
``set_cell`` calls in one batch produce a single coalesced invalidation pass.
The single-threaded ``Cell.set`` semantics (the ``!=`` PartialEq guard) are
reused unchanged, so a one-write batch is observationally identical to a plain
``Cell.set`` (the thread-safe context *refines* the single-threaded kernel).
"""

from __future__ import annotations


__all__ = ["ThreadSafeContext"]

import threading
from typing import TYPE_CHECKING, Any, TypeVar

from .batch import batch as _sync_batch
from .teardown import TeardownScope


if TYPE_CHECKING:
    from collections.abc import Callable

    from .cell import Cell


T = TypeVar("T")


class _PendingWrite:
    __slots__ = ("cell", "value")

    def __init__(self, cell: Cell[Any], value: Any) -> None:
        self.cell = cell
        self.value = value


class ThreadSafeContext:
    """A thread-safe reactive context: a lock plus a batch queue.

    ``set_cell`` writes a cell's value into the queue (under the lock); the
    value is applied lazily (lazily-applied reactivity) at flush. ``batch(run)``
    runs ``run`` (typically a block of ``set_cell`` calls) under the lock, then
    flushes the queued writes in one coalesced invalidation pass at the outermost
    boundary. Concurrent callers are linearized by the lock, so the result is a
    deterministic function of the writes — glitch-free for every batch.

    ``flush`` applies each queued write's value via the cell's ``!=`` (PartialEq)
    guard, then touches each changed cell exactly once at the boundary. Because
    :class:`~lazily.cell.Cell` already coalesces equal writes and propagates to
    its subscribers on ``touch``, the outermost flush yields one invalidation
    wave per batch — the coalesced frontier.
    """

    __slots__ = ("_cells_touched", "_depth", "_lock", "_pending")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._depth = 0
        self._pending: list[_PendingWrite] = []
        self._cells_touched: set[int] = set()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def set_cell[T](self, cell: Cell[T], value: T) -> None:
        """Queue a cell write under the lock. Outside a batch, apply it
        immediately (refining the single-threaded kernel). Inside a batch,
        defer the application to the outermost :meth:`batch` flush."""
        with self._lock:
            if self._depth == 0:
                # Outside a batch: apply immediately (singleton batch ≡ setCell).
                cell.set(value)
                return
            self._pending.append(_PendingWrite(cell, value))

    def scope(self, ctx: dict) -> TeardownScope:
        """Open a teardown scope over ``ctx``.

        The thread-safe context wraps the *write* path only — the graph it
        serializes writes into is the ordinary synchronous one — so this returns
        the same :class:`~lazily.teardown.TeardownScope` the sync surface uses,
        and disposal semantics are identical. The method exists so a caller
        holding only a :class:`ThreadSafeContext` never has to reach past it.

        Teardown itself is *not* serialized by the lock: disposal mutates the
        graph rather than queueing a write, so it belongs inside a
        :meth:`batch`, or on the thread that owns the nodes.
        """
        return TeardownScope(ctx)

    def batch[R](self, run: Callable[[], R]) -> R:
        """Run ``run`` under the lock, queuing all ``set_cell`` writes, then at
        the outermost boundary flush them in one coalesced invalidation pass.
        Nested ``batch`` calls only flush at the outermost boundary."""
        with self._lock:
            self._depth += 1
            try:
                return run()
            finally:
                self._depth -= 1
                if self._depth == 0:
                    self._flush()

    def _flush(self) -> None:
        """Apply the queued writes' values via each cell's ``!=`` guard, then
        touch each changed cell exactly once — the coalesced frontier. Mirrors
        ``flushBatch``: apply values, then one coalesced invalidation pass."""
        pending = self._pending
        self._pending = []
        touched = self._cells_touched
        self._cells_touched = set()
        # Phase 1 — apply each write's value through the PartialEq guard,
        # recording which cells actually changed value (without yet notifying).
        changed: list[Cell[Any]] = []
        seen_changed: set[int] = set()
        for w in pending:
            cell = w.cell
            old = cell._value
            cell._value = w.value
            if cell._value != old and id(cell) not in seen_changed:
                changed.append(cell)
                seen_changed.add(id(cell))
        # Phase 2 — one coalesced invalidation pass: touch each changed cell
        # exactly once, so dependents reached through many changed sources fire
        # once per batch (the coalesced frontier).
        #
        # Run inside the single-threaded ``batch`` boundary so the *effect* half
        # of the frontier coalesces too. Touching cells directly leaves
        # ``in_batch()`` false, so every scheduled reader — an Effect, and
        # therefore a Signal's eager puller — reruns inline once per changed
        # cell instead of once per batch. That is one compute per write rather
        # than one per flush, which ``reactive-graph.md`` § "Signal eagerness"
        # clause 3 forbids. The lock is already held here, and the sync boundary
        # is re-entrant, so this nests safely under a caller's own ``batch``.
        _sync_batch(lambda: [cell.touch() for cell in changed])
        _ = touched  # retained for parity with the formal model's bookkeeping

    # -- direct graph-flush helpers (mirror the Lean pure kernel) -------- #

    @staticmethod
    def apply_batch(
        nodes: dict[Any, tuple[Any | None, Any]],
        batch: list[tuple[Any, Any]],
    ) -> tuple[dict[Any, tuple[Any | None, Any]], list[Any]]:
        """The pure batch-application kernel — a faithful port of the Lean
        ``applyBatch``. Given a node table ``(id -> (value, state))``, apply the
        batch's value updates (with the PartialEq guard) and return
        ``(new_nodes, changed_sources)``.

        Exposed for property testing against ``LazilyFormal.ThreadSafe``.
        """
        new_nodes = dict(nodes)
        changed: list[Any] = []
        for node_id, value in batch:
            cur = new_nodes.get(node_id)
            if cur is None:
                continue
            old_value, _state = cur
            if old_value == value:
                continue
            new_nodes[node_id] = (value, "dirty")
            changed.append(node_id)
        return new_nodes, changed

    @staticmethod
    def flush_batch(
        nodes: dict[Any, tuple[Any | None, Any]],
        dependents: dict[Any, list[Any]],
        batch: list[tuple[Any, Any]],
    ) -> dict[Any, tuple[Any | None, Any]]:
        """The pure batch-flush kernel — a faithful port of the Lean
        ``flushBatch``: apply the batch's values, then mark the coalesced union
        of changed sources' dependents dirty in one pass. Exposed for property
        testing."""
        new_nodes, changed = ThreadSafeContext.apply_batch(nodes, batch)
        frontier: list[Any] = []
        for src in changed:
            for d in dependents.get(src, []):
                if d not in frontier:
                    frontier.append(d)
        for d in frontier:
            val, _state = new_nodes.get(d, (None, None))
            new_nodes[d] = (val, "dirty")
        return new_nodes

    @staticmethod
    def union_dependents(
        dependents: dict[Any, list[Any]], sources: list[Any]
    ) -> list[Any]:
        """The flat union of dependents over a list of source nodes — a faithful
        port of the Lean ``unionDependents``."""
        out: list[Any] = []
        for n in sources:
            for d in dependents.get(n, []):
                out.append(d)
        return out
