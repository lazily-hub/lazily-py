"""Temporal source primitives (``#lztime``).

The Python counterpart of ``lazily-rs/src/time.rs`` (and
``lazily-spec/docs/temporal-sources.md`` / the Lean model
``lazily-formal/LazilyFormal/Temporal.lean``). Time is modeled by a **logical
clock** — a monotone ``now: int`` tick — exactly like ``relay_policy``: a
binding drives the sources from its own runtime timer (a game loop, a test) by
feeding a non-decreasing ``now``.

Core vs cell
------------

Each source is a pure **compute core** (:class:`TimerCore`,
:class:`IntervalCore`, :class:`CronCore`, :class:`DeadlineCore`) — a
side-effect-free state machine over plain integers — split from a thin reactive
**cell** (:class:`TimerCell`, :class:`IntervalCell`, :class:`CronCell`,
:class:`DeadlineCell`) that projects the core's fire edge onto a
:class:`~lazily.cell.Cell` so dependents invalidate **only on an actual fire**
(the backend-portability rule). :class:`DeadlineCell` additionally carries an
opaque user value alongside its bytes-eligible deadline core.

Edge-only invalidation is implemented for free by the ``!=`` (PartialEq) guard
on :class:`~lazily.cell.Cell`: after a fire the shell flips its backing cell and
a repeat tick that does not re-fire is a no-op, so dependents invalidate exactly
once per edge.
"""

from __future__ import annotations


__all__ = [
    "CronCell",
    "CronCore",
    "DeadlineCell",
    "DeadlineCore",
    "DeadlineState",
    "Deadlined",
    "IntervalCell",
    "IntervalCore",
    "ManualClock",
    "TimelineSource",
    "TimerCell",
    "TimerCore",
    "count_upto",
]

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from .cell import Cell


class TimelineSource(Protocol):
    """A pure temporal compute core driven by a monotone logical clock.

    A runtime advances any source uniformly via :meth:`tick`; :meth:`next_fire`
    lets a scheduler compute the delay to the next wake-up.
    """

    def tick(self, now: int) -> bool:
        """Advance to logical time ``now`` (callers must not go backwards).

        Returns ``True`` on a **fire edge** — a fire happened on this tick.
        """
        ...

    def next_fire(self) -> int | None:
        """Logical time of the next fire, or ``None`` when exhausted."""
        ...


@dataclass
class ManualClock:
    """A monotone logical clock a manual runtime (game loop, test) can own to
    drive sources. :meth:`advance` clamps backwards moves so ``now`` is always
    non-decreasing."""

    _now: int = 0

    def now(self) -> int:
        return self._now

    def advance(self, now: int) -> int:
        """Advance to ``now`` (monotone: a smaller value is clamped to the
        current time). Returns the effective ``now`` a source should be ticked
        with."""
        self._now = max(self._now, now)
        return self._now


# ---------------------------------------------------------------------------
# Single-shot timer
# ---------------------------------------------------------------------------


@dataclass
class TimerCore:
    """Single-shot compute core: ``None -> Some(())`` at the first tick with
    ``now >= fire_at``; fires exactly once (idempotent thereafter)."""

    fire_at: int
    _fired: bool = False

    def fired(self) -> bool:
        return self._fired

    def tick(self, now: int) -> bool:
        if self._fired or now < self.fire_at:
            return False
        self._fired = True
        return True

    def next_fire(self) -> int | None:
        return None if self._fired else self.fire_at


class TimerCell:
    """Reactive single-shot timer: projects :class:`TimerCore`'s fire edge onto
    a cell so ``has_fired`` / ``value`` dependents invalidate only on the fire
    (idempotent)."""

    __slots__ = ("_core", "_fired", "ctx")

    def __init__(self, ctx: dict, fire_at: int) -> None:
        self.ctx = ctx
        self._core = TimerCore(fire_at)
        self._fired: Cell[bool] = Cell(ctx, False)

    def tick(self, now: int) -> bool:
        """Advance to logical time ``now``; returns the fire edge. On a fire the
        backing cell flips to ``True`` (the ``!=`` store-guard makes a repeat
        tick a no-op, so dependents invalidate exactly once)."""
        edge = self._core.tick(now)
        if edge:
            self._fired.value = True
        return edge

    def has_fired(self) -> bool:
        """Whether the timer has fired (reactive read)."""
        return self._fired.value

    def value(self) -> tuple[()] | None:
        """``None`` before the fire, ``()`` (the unit ``Some(())``) after
        (reactive read)."""
        return () if self._fired.value else None

    def fired_cell(self) -> Cell[bool]:
        """The backing cell, for dependents that want to subscribe directly."""
        return self._fired

    def next_fire(self) -> int | None:
        return self._core.next_fire()


# ---------------------------------------------------------------------------
# Periodic interval
# ---------------------------------------------------------------------------


@dataclass
class IntervalCore:
    """Periodic compute core: fire boundaries at ``period, 2*period, ...``. A
    tick counts every boundary in ``(frontier, now]``, so a jump past several
    boundaries counts them all."""

    period: int
    _next: int = field(init=False)
    _count: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        self.period = max(self.period, 1)
        self._next = self.period

    def count(self) -> int:
        return self._count

    def _fires_this_tick(self, now: int) -> int:
        """Boundaries crossed on a single tick (0 when ``now`` is below the
        frontier)."""
        if now < self._next:
            return 0
        return (now - self._next) // self.period + 1

    def tick(self, now: int) -> bool:
        fires = self._fires_this_tick(now)
        if fires == 0:
            return False
        self._count += fires
        self._next += fires * self.period
        return True

    def next_fire(self) -> int | None:
        return self._next


class IntervalCell:
    """Reactive periodic interval: projects :class:`IntervalCore`'s fire count
    onto a cell (invalidates only when ``count`` changes)."""

    __slots__ = ("_core", "_count", "ctx")

    def __init__(self, ctx: dict, period: int) -> None:
        self.ctx = ctx
        self._core = IntervalCore(period)
        self._count: Cell[int] = Cell(ctx, 0)

    def tick(self, now: int) -> bool:
        """Advance to logical time ``now``; returns whether a boundary fired.
        The count cell mirrors the core's total fire count."""
        edge = self._core.tick(now)
        if edge:
            self._count.value = self._core.count()
        return edge

    def count(self) -> int:
        """Total fires so far (reactive read)."""
        return self._count.value

    def count_cell(self) -> Cell[int]:
        return self._count

    def next_fire(self) -> int | None:
        return self._core.next_fire()


# ---------------------------------------------------------------------------
# Cron pattern
# ---------------------------------------------------------------------------


def count_upto(n: int, o: int, cycle: int) -> int:
    """Count of ``m in 1..=n`` with ``m mod cycle == o`` (``0 <= o < cycle``)."""
    if o == 0:
        return n // cycle
    if o <= n:
        return (n - o) // cycle + 1
    return 0


@dataclass
class CronCore:
    """Pattern-periodic compute core: a tick ``m >= 1`` fires iff
    ``m mod cycle in offsets``. Structurally an interval with a match set — a
    cron expression's shape. The match count in ``(cursor, now]`` is computed
    arithmetically, so a large ``now`` jump is ``O(offsets)``."""

    cycle: int
    offsets: list[int]
    _cursor: int = field(init=False, default=0)
    _count: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        self.cycle = max(self.cycle, 1)
        reduced = sorted({o % self.cycle for o in self.offsets})
        self.offsets = reduced

    def count(self) -> int:
        return self._count

    def _matches_in(self, lo: int, hi: int) -> int:
        return sum(
            count_upto(hi, o, self.cycle) - count_upto(lo, o, self.cycle)
            for o in self.offsets
        )

    def tick(self, now: int) -> bool:
        if now <= self._cursor:
            self._cursor = max(self._cursor, now)
            return False
        fires = self._matches_in(self._cursor, now)
        self._cursor = now
        if fires == 0:
            return False
        self._count += fires
        return True

    def next_fire(self) -> int | None:
        if not self.offsets:
            return None
        # Smallest m > cursor with m mod cycle in offsets.
        start = self._cursor + 1
        base = start // self.cycle * self.cycle
        for cyc in range(2):
            block = base + cyc * self.cycle
            for o in self.offsets:
                cand = block + o
                if cand >= start:
                    return cand
        return None


class CronCell:
    """Reactive cron source: same reactive contract as :class:`IntervalCell`."""

    __slots__ = ("_core", "_count", "ctx")

    def __init__(self, ctx: dict, cycle: int, offsets: list[int]) -> None:
        self.ctx = ctx
        self._core = CronCore(cycle, list(offsets))
        self._count: Cell[int] = Cell(ctx, 0)

    def tick(self, now: int) -> bool:
        edge = self._core.tick(now)
        if edge:
            self._count.value = self._core.count()
        return edge

    def count(self) -> int:
        return self._count.value

    def count_cell(self) -> Cell[int]:
        return self._count

    def next_fire(self) -> int | None:
        return self._core.next_fire()


# ---------------------------------------------------------------------------
# Value + deadline
# ---------------------------------------------------------------------------


class DeadlineState(Enum):
    """The liveness state of a :class:`Deadlined` value."""

    LIVE = "Live"
    EXPIRED = "Expired"


@dataclass(frozen=True)
class Deadlined[T]:
    """A value paired with a liveness state: ``Live`` until its deadline, then
    ``Expired`` — the value is preserved across the flip."""

    state: DeadlineState
    value: T

    def is_expired(self) -> bool:
        return self.state is DeadlineState.EXPIRED


@dataclass
class DeadlineCore:
    """Deadline compute core (bytes-eligible): a :class:`TimerCore` over the
    deadline. The value lives in the reactive cell."""

    deadline: int
    _timer: TimerCore = field(init=False)

    def __post_init__(self) -> None:
        self._timer = TimerCore(self.deadline)

    def is_expired(self) -> bool:
        return self._timer.fired()

    def tick(self, now: int) -> bool:
        return self._timer.tick(now)

    def next_fire(self) -> int | None:
        return self._timer.next_fire()


class DeadlineCell[T]:
    """Reactive value + deadline: flips ``Live(v) -> Expired(v)`` at the
    deadline, preserving the value; the ``state`` reader invalidates only on the
    expiry edge."""

    __slots__ = ("_core", "_expired", "_value", "ctx")

    def __init__(self, ctx: dict, value: T, deadline: int) -> None:
        self.ctx = ctx
        self._core = DeadlineCore(deadline)
        self._value = value
        self._expired: Cell[bool] = Cell(ctx, False)

    def tick(self, now: int) -> bool:
        """Advance to logical time ``now``; returns the expiry edge."""
        edge = self._core.tick(now)
        if edge:
            self._expired.value = True
        return edge

    def state(self) -> Deadlined[T]:
        """The current state, preserving the value (reactive read)."""
        if self._expired.value:
            return Deadlined(DeadlineState.EXPIRED, self._value)
        return Deadlined(DeadlineState.LIVE, self._value)

    def is_expired(self) -> bool:
        return self._expired.value

    def expired_cell(self) -> Cell[bool]:
        return self._expired

    def next_fire(self) -> int | None:
        return self._core.next_fire()
