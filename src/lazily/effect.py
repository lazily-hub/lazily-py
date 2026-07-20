"""Sync effects — a side-effecting observer that reruns on dependency change.

The Python counterpart of ``lazily-spec/docs/reactive-graph.md`` § "The reactive
family" (``Effect``) and § "API surface" (``effect(run)`` /
``dispose_effect(handle)``). An :class:`Effect` is a side-effecting observer that
reruns whenever a tracked dependency invalidates. An optional cleanup closure
returned by the body runs before each rerun and on dispose (cleanup-before-body
ordering). Disposal is terminal.

This is the **sync** Effect — reruns fire synchronously within the invalidating
call. The async counterpart (:class:`lazily.async_effect.AsyncEffect`) queues
reruns at the batch boundary and is the right choice inside an ``asyncio``
reactor; a :class:`Signal` is composed from a memoized Slot plus an eager
puller-Effect (the eager puller here is local execution state, never serialized).

An Effect participates in the reactive graph's auto-dependency-tracking stack
exactly like a Slot: every Cell/Slot/Signal read inside its body registers a
dependency, and stale dependencies from a previous run are re-discovered on the
next rerun. Inside a :func:`lazily.batch.batch`, the rerun is coalesced into the
batch's single invalidation wave.
"""

from __future__ import annotations


__all__ = ["Effect", "effect"]

from typing import TYPE_CHECKING, Any

from .batch import enqueue_effect, in_batch
from .slot import (
    Slot,
    _detach_from_dependencies,
    _dirty_disposed_dependents,
    mypyc_attr,
    slot_stack,
)


if TYPE_CHECKING:
    from collections.abc import Callable


@mypyc_attr(allow_interpreted_subclasses=True)
class Effect(Slot[Any, dict, None]):
    """A sync reactive effect — a side-effecting observer.

    Register with :func:`effect` (or construct directly) and call
    ``effect(ctx)`` to start the body, which auto-tracks dependencies. Whenever a
    tracked dependency invalidates, the body reruns synchronously — after the
    previous run's cleanup closure (if any) completes. :meth:`dispose` runs the
    final cleanup and goes terminal; no subsequent event revives a disposed
    effect.

    The body ``run(ctx) -> cleanup | None`` may return a no-arg cleanup closure;
    the cleanup runs before the next body rerun and on dispose. Mirrors
    ``effect(run)`` in ``lazily-spec/docs/reactive-graph.md``.
    """

    # ``_disposed`` lives on :class:`~lazily.slot.Slot` now, so every node kind
    # answers ``disposed`` / ``dependent_count`` / ``dependency_count``
    # uniformly; redeclaring it here would shadow the base slot.
    __slots__ = (
        "_body",
        "_cleanup",
        "_ctx",
        "_running",
    )

    _body: Callable[[dict], Any | None]
    _cleanup: Any | None
    _ctx: dict | None
    _running: bool

    def __init__(self, body: Callable[[dict], Any | None]) -> None:
        # Slot.__init__ sets up `_parents` and the placeholder callable; we
        # override `__call__` and `reset` so the slot machinery is used only for
        # dependency tracking (the Effect pushes itself onto `slot_stack` during
        # the body so Cells/Slots/Signals auto-subscribe to it).
        super().__init__(callable=lambda _ctx: None)
        self._body = body
        self._cleanup = None
        self._ctx = None
        self._running = False

    @property
    def is_running(self) -> bool:
        """Whether the body is currently executing (rerun re-entrancy guard)."""
        return self._running

    def __call__(self, ctx: dict) -> None:
        """Run (or rerun) the body, auto-tracking dependencies.

        Pushes self onto the dependency-tracking stack so every Cell/Slot/Signal
        read inside ``body`` registers a dependency; a later invalidation of any
        tracked dependency calls :meth:`reset`, which reruns the body.
        """
        if self._disposed:
            return
        self._ctx = ctx
        # cleanup-before-body: the previous run's cleanup completes before the
        # next body starts.
        self._run_cleanup()
        self._running = True
        slot_stack.append(self)
        # Forward edges describe the current run and are rebuilt by the body's
        # reads, exactly as in ``Slot.__call__``.
        self._deps = None
        try:
            self._cleanup = self._body(ctx)
        finally:
            slot_stack.pop()
            self._running = False

    def reset(self, ctx: Any) -> None:
        """A tracked dependency invalidated — rerun the body synchronously.

        Re-entrant calls are suppressed: an invalidation fired while the body is
        itself executing schedules no extra rerun (the body is already running
        against the latest inputs). Clears downstream edges before the rerun so
        re-registration on the next body execution is exact.

        Inside a :func:`lazily.batch.batch`, the rerun is queued for the
        coalesced effect flush at the outermost boundary — so an effect reached
        through many changed cells in one batch reruns at most once per batch.
        """
        # Entry point: push self onto the iterative invalidation work-stack and
        # drain. The actual rerun / enqueue happens in :meth:`_invalidate`.
        super().reset(ctx)

    def _invalidate(self, ctx: Any) -> None:
        if self._disposed or self._running:
            return
        # Drop downstream edges before the rerun so re-registration on the next
        # body execution is exact. Setting to None also frees the ~216 B set.
        self._parents = None
        if in_batch():
            enqueue_effect(self)
            return
        if self._ctx is not None:
            self(self._ctx)

    def _batch_rerun(self) -> None:
        """Rerun at the batch boundary (the coalesced effect flush)."""
        if self._disposed:
            return
        if self._ctx is not None:
            self(self._ctx)

    def dispose(self) -> None:  # type: ignore[override]
        """Deschedule, drop edges *in both directions*, run cleanup. Terminal —
        no subsequent event revives a disposed effect.

        Overrides :meth:`Slot.dispose` with a no-argument signature: an effect,
        unlike a bare slot, already holds the context it last ran against, and
        this spelling is the one that shipped.

        Detaching the forward direction is what makes an effect's subscription
        actually end. Leaving it attached is the leak
        ``churn_returns_to_baseline`` measures: a subscribe/unsubscribe cycle
        would grow the source's dependent set without bound even though the live
        subscriber count never changes.
        """
        if self._disposed:
            return
        self._disposed = True
        _detach_from_dependencies(self)
        pare = self._parents
        self._parents = None
        ctx = self._ctx
        if pare and ctx is not None:
            _dirty_disposed_dependents(pare, ctx)
        self._run_cleanup()

    def _run_cleanup(self) -> None:
        cleanup = self._cleanup
        if cleanup is not None:
            self._cleanup = None
            cleanup()


def effect(body: Callable[[dict], Any | None]) -> Effect:
    """Register a sync reactive effect.

    ``body(ctx) -> cleanup | None`` runs immediately on first call (when you
    invoke the returned :class:`Effect` with a context), auto-tracking
    dependencies. Whenever a tracked dependency invalidates, the body reruns.
    """
    return Effect(body)
