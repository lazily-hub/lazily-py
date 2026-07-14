"""Fault-tolerance primitives (``#lzresilience``).

The Python counterpart of ``lazily-rs``'s ``src/resilience.rs`` — see
``lazily-spec/docs/resilience.md`` and the formal model
``lazily-formal/LazilyFormal/Resilience.lean``. Circuit breaker / retry /
bulkhead / timeout, each a pure compute **core** (a state machine / counter over
a logical clock) split from a reactive **cell** projecting the salient reader
onto a :class:`~lazily.cell.Cell`.

Each cell owns one internal :class:`~lazily.cell.Cell` per asserted reader
(``state`` / ``delay`` / ``permits_in_use`` / ``is_timed_out``). After each op the
cell recomputes the reader and assigns it to the cell; the cell's ``!=``
(PartialEq) guard invalidates dependents only when the projected value actually
changes. Reads inside a :class:`~lazily.slot.Slot` / ``Signal`` / ``Effect``
subscribe to the reader cell, so derived computeds recompute exactly when their
reader changes.
"""

from __future__ import annotations


__all__ = [
    "BreakerState",
    "BulkheadCell",
    "BulkheadCore",
    "CircuitBreakerCell",
    "CircuitBreakerCore",
    "RetryPolicyCell",
    "RetryPolicyCore",
    "TimeoutCell",
    "TimeoutCore",
]

from collections import deque
from enum import Enum

from .batch import batch
from .cell import Cell


# u64 / u32 saturation bounds — mirror the Rust cores' fixed-width integer math.
_U64_MASK = (1 << 64) - 1
_U32_MAX = (1 << 32) - 1


# ===========================================================================
# Circuit breaker
# ===========================================================================


class BreakerState(Enum):
    """Circuit-breaker state."""

    #: Calls pass; failures accumulate in the window.
    CLOSED = "Closed"
    #: Fast-fail until the reset deadline.
    OPEN = "Open"
    #: Allow a single probe.
    HALF_OPEN = "HalfOpen"


class CircuitBreakerCore:
    """Circuit-breaker compute core.

    A sliding window of outcomes trips ``Closed -> Open`` at
    ``failure_threshold``; ``Open -> HalfOpen`` at the deadline; a HalfOpen
    success closes, a HalfOpen failure re-opens.
    """

    __slots__ = (
        "_failure_threshold",
        "_open_until",
        "_outcomes",
        "_reset_timeout",
        "_state",
        "_window",
    )

    def __init__(self, window: int, failure_threshold: int, reset_timeout: int) -> None:
        self._window = max(window, 1)
        self._failure_threshold = max(failure_threshold, 1)
        self._reset_timeout = reset_timeout
        self._state = BreakerState.CLOSED
        self._outcomes: deque[bool] = deque()  # True = success
        self._open_until = 0

    def state(self) -> BreakerState:
        return self._state

    def _failures(self) -> int:
        return sum(1 for s in self._outcomes if not s)

    def allow(self, now: int) -> bool:
        """Whether a call is permitted; performs the ``Open -> HalfOpen``
        transition at the deadline."""
        if self._state is BreakerState.CLOSED:
            return True
        if self._state is BreakerState.OPEN:
            if now >= self._open_until:
                self._state = BreakerState.HALF_OPEN
                return True
            return False
        # HalfOpen
        return True

    def record(self, success: bool, now: int) -> None:
        """Feed a call outcome and drive the state machine."""
        if self._state is BreakerState.HALF_OPEN:
            if success:
                self._state = BreakerState.CLOSED
                self._outcomes.clear()
            else:
                self._state = BreakerState.OPEN
                self._open_until = now + self._reset_timeout
        elif self._state is BreakerState.CLOSED:
            self._outcomes.append(success)
            while len(self._outcomes) > self._window:
                self._outcomes.popleft()
            if self._failures() >= self._failure_threshold:
                self._state = BreakerState.OPEN
                self._open_until = now + self._reset_timeout
        # Open: no-op


class CircuitBreakerCell:
    """Reactive circuit breaker: projects the ``state`` onto a
    :class:`~lazily.cell.Cell`."""

    __slots__ = ("_core", "_state", "ctx")

    def __init__(
        self,
        ctx: dict,
        window: int,
        failure_threshold: int,
        reset_timeout: int,
    ) -> None:
        self.ctx = ctx
        self._core = CircuitBreakerCore(window, failure_threshold, reset_timeout)
        self._state: Cell[BreakerState] = Cell(ctx, BreakerState.CLOSED)

    def _refresh(self) -> None:
        def apply() -> None:
            self._state.value = self._core.state()

        batch(apply)

    def allow(self, now: int) -> bool:
        result = self._core.allow(now)
        self._refresh()
        return result

    def record(self, success: bool, now: int) -> None:
        self._core.record(success, now)
        self._refresh()

    def state(self) -> BreakerState:
        """Reactive read of the breaker state."""
        return self._state.value

    def state_cell(self) -> Cell[BreakerState]:
        """Handle to the ``state`` reader cell (advanced wiring)."""
        return self._state


# ===========================================================================
# Retry backoff
# ===========================================================================


class RetryPolicyCore:
    """Exponential-backoff compute core.

    ``delay(attempt) = min(cap, base * 2**attempt)``, saturating to ``cap`` on
    shift overflow (attempt >= 64, matching the Rust ``checked_shl``).
    """

    __slots__ = ("_attempt", "_base", "_cap")

    def __init__(self, base: int, cap: int) -> None:
        self._base = base
        self._cap = cap
        self._attempt = 0

    def delay(self, attempt: int) -> int:
        """The delay for ``attempt`` (saturating at ``cap``)."""
        if attempt >= 64:
            return self._cap
        shifted = (self._base << attempt) & _U64_MASK
        return min(self._cap, shifted)

    def next_delay(self) -> int:
        """The current attempt's delay, then advance."""
        d = self.delay(self._attempt)
        self._attempt = min(self._attempt + 1, _U32_MAX)
        return d

    def reset(self) -> None:
        self._attempt = 0


class RetryPolicyCell:
    """Reactive retry policy: projects the current delay onto a
    :class:`~lazily.cell.Cell`."""

    __slots__ = ("_core", "_delay", "ctx")

    def __init__(self, ctx: dict, base: int, cap: int) -> None:
        self.ctx = ctx
        self._core = RetryPolicyCore(base, cap)
        self._delay: Cell[int] = Cell(ctx, 0)

    def next_delay(self) -> int:
        d = self._core.next_delay()

        def apply() -> None:
            self._delay.value = d

        batch(apply)
        return d

    def reset(self) -> None:
        self._core.reset()

        def apply() -> None:
            self._delay.value = 0

        batch(apply)

    def delay(self) -> int:
        """Reactive read of the current delay."""
        return self._delay.value

    def delay_cell(self) -> Cell[int]:
        """Handle to the ``delay`` reader cell (advanced wiring)."""
        return self._delay


# ===========================================================================
# Bulkhead
# ===========================================================================


class BulkheadCore:
    """Bounded isolation-pool compute core."""

    __slots__ = ("_capacity", "_in_use")

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._in_use = 0

    def in_use(self) -> int:
        return self._in_use

    def acquire(self) -> bool:
        if self._in_use < self._capacity:
            self._in_use += 1
            return True
        return False

    def release(self) -> None:
        if self._in_use > 0:
            self._in_use -= 1


class BulkheadCell:
    """Reactive bulkhead: projects ``permits_in_use`` onto a
    :class:`~lazily.cell.Cell`."""

    __slots__ = ("_core", "_in_use", "ctx")

    def __init__(self, ctx: dict, capacity: int) -> None:
        self.ctx = ctx
        self._core = BulkheadCore(capacity)
        self._in_use: Cell[int] = Cell(ctx, 0)

    def _refresh(self) -> None:
        def apply() -> None:
            self._in_use.value = self._core.in_use()

        batch(apply)

    def acquire(self) -> bool:
        result = self._core.acquire()
        self._refresh()
        return result

    def release(self) -> None:
        self._core.release()
        self._refresh()

    def permits_in_use(self) -> int:
        """Reactive read of the permits in use."""
        return self._in_use.value

    def permits_in_use_cell(self) -> Cell[int]:
        """Handle to the ``permits_in_use`` reader cell (advanced wiring)."""
        return self._in_use


# ===========================================================================
# Timeout
# ===========================================================================


class TimeoutCore:
    """Deadline-bounded call compute core."""

    __slots__ = ("_armed", "_deadline", "_timed_out")

    def __init__(self) -> None:
        self._deadline = 0
        self._armed = False
        self._timed_out = False

    def arm(self, now: int, timeout: int) -> None:
        """Arm the timeout with ``deadline = now + timeout``."""
        self._deadline = now + timeout
        self._armed = True
        self._timed_out = False

    def tick(self, now: int) -> bool:
        """Fast-fail when ``now >= deadline``; returns the timeout edge (once)."""
        if self._armed and not self._timed_out and now >= self._deadline:
            self._timed_out = True
            return True
        return False

    def is_timed_out(self) -> bool:
        return self._timed_out


class TimeoutCell:
    """Reactive timeout: projects ``is_timed_out`` onto a
    :class:`~lazily.cell.Cell`."""

    __slots__ = ("_core", "_timed_out", "ctx")

    def __init__(self, ctx: dict) -> None:
        self.ctx = ctx
        self._core = TimeoutCore()
        self._timed_out: Cell[bool] = Cell(ctx, False)

    def _refresh(self) -> None:
        def apply() -> None:
            self._timed_out.value = self._core.is_timed_out()

        batch(apply)

    def arm(self, now: int, timeout: int) -> None:
        self._core.arm(now, timeout)
        self._refresh()

    def tick(self, now: int) -> bool:
        result = self._core.tick(now)
        self._refresh()
        return result

    def is_timed_out(self) -> bool:
        """Reactive read of the timed-out flag."""
        return self._timed_out.value

    def is_timed_out_cell(self) -> Cell[bool]:
        """Handle to the ``is_timed_out`` reader cell (advanced wiring)."""
        return self._timed_out
