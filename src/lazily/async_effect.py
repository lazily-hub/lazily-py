"""Async effects — the scheduling lifecycle of an async reactive observer.

The Python counterpart of the Lean ``LazilyFormal.AsyncEffect`` formal model in
``lazily-formal`` and ``lazily-spec/docs/async.md`` § "Async effects" + § "Batch
support". The pure transition kernel (:func:`step`) is a faithful port of the
Lean ``step`` — a total function of ``(state, event)`` — so the
cleanup-before-body, batch-boundary scheduling, and disposal guarantees hold for
*every* input.

The runtime :class:`AsyncEffect` wraps the pure kernel in an ``asyncio``
implementation: invalidation only *queues* a rerun (it never starts one inline);
the body runs when the executor fires it (at outermost batch exit); a body rerun
cannot start while a cleanup future is pending (cleanup-before-body); disposal
removes pending reruns, awaits the in-flight cleanup, and is terminal.
"""

from __future__ import annotations


__all__ = [
    "AsyncEffect",
    "EffectEvent",
    "EffectState",
    "StepEffect",
    "step",
]

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum, auto


class EffectState(Enum):
    """The lifecycle state of an async effect. Five states track whether a rerun
    is queued and/or a cleanup future is in flight; ``DISPOSED`` is terminal."""

    IDLE = auto()
    SCHEDULED = auto()
    CLEANUP_RUNNING = auto()
    CLEANUP_RUNNING_SCHEDULED = auto()
    DISPOSED = auto()


class EffectEvent(Enum):
    """An event that drives the effect through its lifecycle."""

    INVALIDATE = auto()
    FIRE = auto()  # carries has_cleanup at runtime
    CLEANUP_DONE = auto()
    DISPOSE = auto()


@dataclass(frozen=True)
class StepEffect:
    state: EffectState


def step(
    s: EffectState, event: EffectEvent, *, has_cleanup: bool = False
) -> EffectState:
    """One transition of the effect lifecycle. ``fire`` from a cleanup-pending
    state is a no-op (cleanup-before-body); ``invalidate`` only ever queues,
    never fires; ``dispose`` is absorbing. Mirrors ``step``.

    ``has_cleanup`` is the carried flag for ``FIRE`` (whether the body returned
    an async cleanup future)."""
    if event is EffectEvent.INVALIDATE:
        if s is EffectState.IDLE:
            return EffectState.SCHEDULED
        if s is EffectState.SCHEDULED:
            return EffectState.SCHEDULED
        if s is EffectState.CLEANUP_RUNNING:
            return EffectState.CLEANUP_RUNNING_SCHEDULED
        if s is EffectState.CLEANUP_RUNNING_SCHEDULED:
            return EffectState.CLEANUP_RUNNING_SCHEDULED
        return EffectState.DISPOSED
    if event is EffectEvent.FIRE:
        if s is EffectState.SCHEDULED:
            return EffectState.CLEANUP_RUNNING if has_cleanup else EffectState.IDLE
        if s is EffectState.DISPOSED:
            return EffectState.DISPOSED
        return s  # fire blocked during cleanup
    if event is EffectEvent.CLEANUP_DONE:
        if s is EffectState.CLEANUP_RUNNING:
            return EffectState.IDLE
        if s is EffectState.CLEANUP_RUNNING_SCHEDULED:
            return EffectState.SCHEDULED
        if s is EffectState.DISPOSED:
            return EffectState.DISPOSED
        return s
    if event is EffectEvent.DISPOSE:
        return EffectState.DISPOSED
    return s


CleanupFn = Callable[[], Awaitable[None]]


class AsyncEffect:
    """An async reactive effect: a side-effecting observer that reruns whenever
    a tracked dependency invalidates, with an optional async cleanup closure
    that completes before the next body rerun.

    Mirrors ``lazily-spec/docs/async.md`` § "Async effects". Invalidation only
    queues a rerun; the body runs when :meth:`flush` is called (the batch
    boundary). A rerun does not start until the previous cleanup completes.
    """

    __slots__ = ("_body", "_cleanup_task", "_pending", "_state")

    def __init__(self, body: Callable[[], Awaitable[CleanupFn | None]]) -> None:
        self._body = body
        self._state = EffectState.IDLE
        self._pending = False
        self._cleanup_task: asyncio.Task[None] | None = None

    @property
    def state(self) -> EffectState:
        return self._state

    def invalidate(self) -> None:
        """A tracked dependency was invalidated; queue a rerun (never start one
        inline). If a cleanup is in flight, the rerun is deferred until it
        completes."""
        if self._state is EffectState.DISPOSED:
            return
        if self._state is EffectState.CLEANUP_RUNNING:
            self._state = EffectState.CLEANUP_RUNNING_SCHEDULED
        else:
            self._state = EffectState.SCHEDULED
        self._pending = True

    async def flush(self) -> None:
        """Fire queued reruns at the batch boundary. Runs the body (after
        awaiting any in-flight cleanup), and — if the body returns an async
        cleanup closure — awaits it before the next rerun can start. A no-op if
        nothing is queued."""
        while self._pending and self._state is not EffectState.DISPOSED:
            # Wait for an in-flight cleanup before the next body rerun.
            if self._cleanup_task is not None:
                await self._cleanup_task
                self._cleanup_task = None
            self._pending = False
            self._state = EffectState.CLEANUP_RUNNING
            cleanup = await self._run_body()
            if cleanup is not None:
                loop = asyncio.get_running_loop()
                self._cleanup_task = loop.create_task(self._await_cleanup(cleanup))
                # If a new rerun was queued during the body, stay pending; the
                # next loop iteration awaits the cleanup first.
                if not self._pending:
                    await self._cleanup_task
                    self._cleanup_task = None
                    self._state = (
                        EffectState.IDLE if not self._pending else EffectState.SCHEDULED
                    )
            else:
                self._state = (
                    EffectState.IDLE if not self._pending else EffectState.SCHEDULED
                )

    async def dispose(self) -> None:
        """Remove pending reruns, await the in-flight cleanup, and go terminal.
        No subsequent event revives a disposed effect."""
        self._state = EffectState.DISPOSED
        self._pending = False
        if self._cleanup_task is not None:
            await self._cleanup_task
            self._cleanup_task = None

    async def _run_body(self) -> CleanupFn | None:
        try:
            return await self._body()
        except BaseException:
            return None

    async def _await_cleanup(self, cleanup: CleanupFn) -> None:
        with contextlib.suppress(BaseException):
            await cleanup()
