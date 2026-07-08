"""Batch boundary — coalesce cell writes into one invalidation pass.

The Python counterpart of ``lazily-spec/docs/reactive-graph.md`` § "API surface"
(``batch(run)``) and the coalesced-frontier invariant. ``batch`` coalesces
several cell updates into one invalidation + effect flush: writes inside the
batch set the cell value but defer the ``touch()`` (the subscriber notification)
to the outermost boundary, so a dependent reached through many changed cells in
one batch appears at most once per batch.

This is the top-level batch primitive (the single-threaded coalescing boundary).
The lock-serialized counterpart that also linearizes concurrent writers lives
at :class:`lazily.thread_safe.ThreadSafeContext.batch`; a singleton ``batch``
(idempotent with a plain ``Cell.set``) is the refinement.

:func:`notify_change` is the hook :class:`lazily.cell.Cell` calls on a value
change. Outside a batch it touches immediately; inside a batch it queues the
cell for the coalesced flush.
"""

from __future__ import annotations


__all__ = ["batch", "batch_context", "in_batch", "notify_change"]

import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from collections.abc import Callable

    from .cell import Cell


_state = threading.local()


def _depth() -> int:
    return getattr(_state, "depth", 0)


def in_batch() -> bool:
    """Whether the calling thread is currently inside a :func:`batch` boundary."""
    return _depth() > 0


def notify_change(cell: Cell[Any]) -> None:
    """Coalesce-aware change notification for :class:`~lazily.cell.Cell`.

    Outside a batch, touch the cell immediately (subscribers fire at once). Inside
    a batch, queue the cell for one coalesced ``touch()`` at the outermost
    boundary — so multiple writes to the same cell (or to cells sharing a
    dependent) produce a single invalidation wave per batch.
    """
    if _depth() > 0 and hasattr(_state, "pending_cells"):
        _state.pending_cells.add(cell)
    else:
        cell.touch()


def batch[R](run: Callable[[], R]) -> R:
    """Run ``run``, queuing cell writes, then at the outermost boundary flush
    one coalesced invalidation pass.

    A singleton batch (one write) is observationally identical to a plain
    ``Cell.set``: the cell's ``!=`` (PartialEq) guard applies, and the flush
    touches each changed cell exactly once. Nested ``batch`` calls only flush at
    the outermost boundary.
    """
    _ensure_state()
    _state.depth += 1
    try:
        return run()
    finally:
        # Flush while still inside the batch boundary (depth > 0) so that
        # ``in_batch()`` is True during the invalidation pass and effects queue
        # for the coalesced Phase-2 flush instead of rerunning inline.
        if _state.depth == 1:
            _flush()
        _state.depth -= 1


@contextmanager
def batch_context():
    """Context-manager form of :func:`batch`::

    with batch_context():
        name.value = "x"
        count.value = 2
    # one coalesced invalidation wave fires here
    """
    _ensure_state()
    _state.depth += 1
    try:
        yield
    finally:
        if _state.depth == 1:
            _flush()
        _state.depth -= 1


def enqueue_effect(eff: Any) -> None:
    """Queue an :class:`~lazily.effect.Effect` rerun for the batch flush.

    Called by ``Effect.reset`` when inside a batch. Each effect is deduplicated
    by identity, so the coalesced flush reruns it at most once per batch — the
    "a dependent reached through many changed cells in one batch appears at most
    once" invariant.
    """
    if hasattr(_state, "pending_effects"):
        _state.pending_effects.add(eff)


def _ensure_state() -> None:
    if not hasattr(_state, "depth"):
        _state.depth = 0
        _state.pending_cells = set()
        _state.pending_effects = set()


def _flush() -> None:
    """The coalesced frontier: touch each changed cell exactly once, then rerun
    each queued effect exactly once."""
    _ensure_state()
    # Phase 1 — one coalesced invalidation pass: touch each changed cell once,
    # dirtying its slot/effect dependents.
    pending_cells = _state.pending_cells
    _state.pending_cells = set()
    for cell in pending_cells:
        cell.touch()
    # Phase 2 — the coalesced effect flush: rerun each queued effect once
    # against the now-final inputs. Effects queued during Phase 1 (by cells
    # touching during the invalidation pass) are deduplicated by identity.
    pending_effects = _state.pending_effects
    _state.pending_effects = set()
    for eff in pending_effects:
        eff._batch_rerun()
