"""Async reactive slots — the ``Empty / Computing / Resolved / Error`` lifecycle
with revision-tracked stale-completion discard.

The Python counterpart of the Lean ``LazilyFormal.AsyncSlotState`` formal model
in ``lazily-formal`` and ``lazily-spec/docs/async.md`` § "Async slot state
machine". The pure transition kernel (:func:`step`) is a faithful port of the
Lean ``step`` — a total function of ``(slot, event)`` — so the stale-discard
guarantee ("a stale completion is never published", conformance point 2) holds
for *every* input.

The runtime :class:`AsyncSlot` wraps the pure kernel in an ``asyncio``
implementation: a single in-flight computation per revision, concurrent callers
attach as waiters, stale completions are discarded against the recorded
revision, and explicit cancellation / invalidation / disposal are honored. The
concurrency-specific properties (waiter cancellation, the two benign
``get_async`` races, one-in-flight-per-revision deduplication) are exercised by
targeted deterministic tests rather than the pure model.
"""

from __future__ import annotations


__all__ = [
    "AsyncSlot",
    "Revision",
    "SlotEvent",
    "SlotState",
    "SlotValue",
    "StepSlot",
    "step",
]

import asyncio
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, TypeVar


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


T = TypeVar("T")
Revision = int
SlotValue = object


class SlotState(Enum):
    """A slot's lifecycle state (``async.md:81-86``)."""

    EMPTY = auto()
    COMPUTING = auto()
    RESOLVED = auto()
    ERROR = auto()


@dataclass(frozen=True)
class StepSlot:
    """The pure async-slot state: lifecycle state, monotonic revision, the
    revision recorded for the in-flight computation (if any), and the optional
    cached value. Mirrors ``LazilyFormal.AsyncSlotState.AsyncSlot``."""

    state: SlotState
    revision: Revision
    compute_rev: Revision | None
    value: object = None

    def is_well_formed(self) -> bool:
        """Fields agree with lifecycle state: Computing carries an in-flight
        revision and no cached value; Resolved carries a cached value and no
        in-flight revision; Empty/Error carry neither. Mirrors
        ``AsyncSlot.WellFormed``."""
        if self.state is SlotState.EMPTY:
            return self.compute_rev is None and self.value is None
        if self.state is SlotState.COMPUTING:
            return self.compute_rev is not None and self.value is None
        if self.state is SlotState.RESOLVED:
            return self.compute_rev is None and self.value is not None
        # ERROR
        return self.compute_rev is None and self.value is None


class SlotEvent(Enum):
    """An event that drives the slot through its lifecycle (``async.md:94-105``)."""

    START = auto()
    COMPLETE_OK = auto()  # carries (revision, value) at runtime
    COMPLETE_ERR = auto()  # carries revision at runtime
    INVALIDATE = auto()
    RETRY = auto()
    HARD_CLEAR = auto()


def step(
    s: StepSlot, event: SlotEvent, *, rev: Revision = 0, value: object = None
) -> StepSlot:
    """One transition of the slot state machine. A completion is accepted only
    when its revision is the slot's current in-flight revision; otherwise the
    result is discarded and the slot is unchanged. Mirrors ``step``.

    ``rev``/``value`` are the carried payload for ``COMPLETE_OK``/``COMPLETE_ERR``.
    """
    st = s.state
    if event is SlotEvent.START:
        if st is SlotState.EMPTY:
            return StepSlot(SlotState.COMPUTING, s.revision, s.revision, None)
        return s
    if event is SlotEvent.COMPLETE_OK:
        if st is SlotState.COMPUTING and s.compute_rev == rev:
            return StepSlot(SlotState.RESOLVED, s.revision, None, value)
        return s
    if event is SlotEvent.COMPLETE_ERR:
        if st is SlotState.COMPUTING and s.compute_rev == rev:
            return StepSlot(SlotState.ERROR, s.revision, None, None)
        return s
    if event is SlotEvent.INVALIDATE:
        new_rev = s.revision + 1
        if st in (SlotState.EMPTY, SlotState.COMPUTING, SlotState.RESOLVED):
            return StepSlot(SlotState.COMPUTING, new_rev, new_rev, None)
        return s  # ERROR: invalidate is a no-op (retry instead)
    if event is SlotEvent.RETRY:
        if st is SlotState.ERROR:
            return StepSlot(SlotState.COMPUTING, s.revision, s.revision, None)
        return s
    if event is SlotEvent.HARD_CLEAR:
        return StepSlot(SlotState.EMPTY, s.revision + 1, None, None)
    return s


class AsyncSlot[T]:
    """An async reactive slot: a lazily-computed, memoized, asyncio-aware
    derived value with the 4-state lifecycle and stale-completion discard.

    Concurrent ``get_async`` callers attach as waiters on a single in-flight
    computation per revision; a completion whose revision no longer matches the
    slot's in-flight revision is discarded (the value is never published). A
    dependency invalidation (or explicit :meth:`invalidate`) during in-flight
    compute advances the revision so the completing future's result is dropped
    and a fresh compute is spawned.

    Mirrors ``lazily-spec/docs/async.md`` § "Async slot state machine".
    """

    __slots__ = (
        "_compute",
        "_compute_rev",
        "_drive_task",
        "_error",
        "_future",
        "_revision",
        "_state",
        "_value",
        "_waiters",
    )

    def __init__(self, compute: Callable[[], Awaitable[T]]) -> None:
        self._compute = compute
        self._state = SlotState.EMPTY
        self._revision: Revision = 0
        self._compute_rev: Revision | None = None
        self._value: T | None = None
        self._error: BaseException | None = None
        self._future: asyncio.Future[T] | None = None
        self._waiters: list[asyncio.Future[T]] = []
        self._drive_task: asyncio.Task[None] | None = None

    @property
    def state(self) -> SlotState:
        return self._state

    @property
    def revision(self) -> Revision:
        """The monotonic revision counter. Bumped by every ``invalidate`` /
        ``hard_clear``; a completion whose recorded revision no longer matches
        is discarded (conformance point 2)."""
        return self._revision

    def get(self) -> T | None:
        """Synchronous cached read: the value if resolved, else ``None``
        (warm fast path). Never spawns a computation."""
        return self._value

    async def get_async(self) -> T:
        """Await a slot value. If resolved, return the cached value immediately;
        otherwise spawn (or attach to) the in-flight computation for the current
        revision and await it. Stale completions are discarded against the
        recorded revision; ``get_async`` loops (re-resolves) until a current
        completion publishes, an error propagates, or a fresh compute resolves.
        """
        while True:
            if self._state is SlotState.RESOLVED:
                return self._value  # type: ignore[return-value]
            if self._state is SlotState.ERROR:
                self._spawn()  # retry (Error -> Computing)
            elif self._state is SlotState.EMPTY or self._future is None:
                self._spawn()
            # Attached to the in-flight future for the current revision. The
            # drive task always resolves this future (current completion
            # publishes / errors; stale completion resolves with a None
            # sentinel so the awaiter wakes and re-resolves).
            fut = self._future
            assert fut is not None
            await fut
            # A current Ok completion sets RESOLVED -> the loop returns above.
            # A current Err completion re-raises via the future. A stale
            # completion resolves with None -> loop and spawn a fresh compute.

    def invalidate(self) -> None:
        """Mark the slot stale: bump the revision. An in-flight completion will
        find a revision mismatch and be discarded; the value is dropped so the
        next ``get_async`` spawns a fresh compute. Resolved -> Computing."""
        self._revision += 1
        if self._state is SlotState.ERROR:
            return  # no-op on Error (retry instead)
        self._state = SlotState.COMPUTING
        self._compute_rev = self._revision
        self._value = None
        # Drop the reference to any in-flight future: a completing stale compute
        # resolves it with the None sentinel, but the next get_async must spawn
        # a fresh computation for the new revision (the old one is discarded).
        self._future = None

    def hard_clear(self) -> None:
        """Reset to Empty and bump the revision (cancellation). An in-flight
        completion will be discarded."""
        self._revision += 1
        self._state = SlotState.EMPTY
        self._compute_rev = None
        self._value = None
        self._error = None
        self._future = None

    # -- internal ------------------------------------------------------- #

    def _spawn(self) -> None:
        self._state = SlotState.COMPUTING
        self._compute_rev = self._revision
        self._value = None
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[T] = loop.create_future()
        self._future = fut

        async def _drive() -> None:
            # Capture the revision this computation belongs to. A completion is
            # current iff it matches the slot's in-flight revision at completion
            # time; otherwise it is discarded (conformance point 2).
            rev = self._revision
            try:
                value = await self._compute()
            except BaseException as exc:
                if self._compute_rev == rev and self._future is fut:
                    # Current error: publish the error to awaiters and go ERROR.
                    self._state = SlotState.ERROR
                    self._compute_rev = None
                    self._error = exc
                    if not fut.done():
                        fut.set_exception(exc)
                elif not fut.done():
                    # Stale error: discard, wake the awaiter to re-resolve.
                    fut.set_result(None)
                return
            if self._compute_rev == rev and self._future is fut:
                # Current Ok: publish the value and resolve awaiters.
                self._state = SlotState.RESOLVED
                self._compute_rev = None
                self._value = value
                if not fut.done():
                    fut.set_result(value)
            elif not fut.done():
                # Stale Ok: discard against the current revision, wake the
                # awaiter so get_async re-resolves (spawns a fresh compute).
                fut.set_result(None)

        # Keep a strong reference to the drive task so it is not cancelled by
        # garbage collection before it resolves the future.
        self._drive_task = loop.create_task(_drive())
