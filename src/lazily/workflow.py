"""Workflow-safe reactive context for durable execution (``#lzpyworkflowdeterminism``).

A durable-execution engine (temporal.io, and anything else that re-runs a
function from an event log) replays workflow code from the beginning after every
failure and expects the **same decisions in the same order**. A reactive graph is
a natural fit — it is already a pure function of its sources — right up to the
moment something in it reads the wall clock, a random number, or a UUID. Then
replay silently diverges, and the damage surfaces much later as a workflow that
will not complete.

This module is the boundary that makes that failure loud instead of silent:

* :class:`WorkflowClock` binds lazily's logical tick to the **engine's** clock
  (``workflow.now()``), which is exactly the value the engine replays. It is
  strictly monotone; a backwards reading means something is feeding it wall time
  and raises rather than being clamped away.
* :class:`WorkflowContext` owns the registered :class:`~lazily.temporal.TimelineSource`
  set and is the only sanctioned way to advance them, so no part of the graph can
  invent its own idea of "now". :meth:`WorkflowContext.next_wakeup` computes the
  delay to the next fire and :meth:`WorkflowContext.schedule_next` hands it to
  the engine's timer, so a scheduled effect becomes a durable timer rather than a
  sleeping thread.
* :func:`deterministic_scope` raises :class:`NonDeterminismError` on the
  non-determinism a reactive body can reach — see its docstring for exactly what
  it does and does not intercept.

**No dependency on ``temporalio``.** The engine is injected as two callables (a
clock and a scheduler), so this works against temporal.io, against a test double,
and against any other durable engine with the same two operations.

.. warning::

   ``lazily.temporal`` is **not** temporal.io. It predates this module and is
   lazily's *time-operator* family (``TimerCell``, ``IntervalCell``,
   ``CronCell``, ``DeadlineCell``) — "temporal" in the tense/logical-clock sense.
   The name is unfortunate now that temporal.io exists, but it is public API in
   nine bindings and renaming it would be a breaking change across all of them to
   buy what this paragraph buys. The temporal.io integration is **this** module,
   ``lazily.workflow``.

Example::

    # inside a temporalio workflow
    clock = WorkflowClock(workflow.now)
    wf = WorkflowContext(ctx, clock, scheduler=TemporalScheduler())

    timer = wf.register(TimerCore(fire_at=clock.tick() + 5_000))
    while not timer.fired():
        await wf.schedule_next()  # a durable engine timer, not a sleep
        wf.advance()  # ticks every registered source from workflow.now()
"""

from __future__ import annotations


__all__ = [
    "NonDeterminismError",
    "WorkflowClock",
    "WorkflowContext",
    "WorkflowScheduler",
    "deterministic_scope",
    "workflow_context",
]

import contextlib
import os
import random
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable


if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from .temporal import TimelineSource


class NonDeterminismError(RuntimeError):
    """A workflow-scoped body reached something replay cannot reproduce.

    Raised instead of returning the value, because returning it is what corrupts
    the replay — and does so silently, far from the line that caused it.
    """


# ---------------------------------------------------------------------------
# Clock
# ---------------------------------------------------------------------------


def _epoch_nanos(value: Any) -> int:
    """Normalize an engine clock reading to integer nanoseconds since the epoch."""
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return int(moment.timestamp() * 1_000_000_000)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value * 1_000_000_000)
    raise TypeError(
        f"workflow clock returned {type(value).__name__}; expected a datetime, "
        f"int nanoseconds, or float seconds"
    )


class WorkflowClock:
    """lazily's logical tick, bound to the durable engine's replayed clock.

    ``now`` is the engine's clock accessor — ``workflow.now`` under temporal.io.
    It is called on demand rather than stored, because during replay its value is
    fed from the event history and must be read at the same points every time.

    ``resolution_ns`` sets the tick unit (default milliseconds). A
    :class:`~lazily.temporal.TimelineSource` compares ticks with ``>=``, so the
    unit only has to be finer than the shortest timer the workflow sets.

    **Strictly monotone.** :class:`~lazily.temporal.ManualClock` clamps a
    backwards move, which is right for a game loop and wrong here: an engine
    clock cannot go backwards, so a backwards reading means something is feeding
    this wall time, and clamping it would hide exactly the bug this module
    exists to surface.
    """

    __slots__ = ("_last", "_now", "_resolution_ns")

    def __init__(
        self, now: Callable[[], Any], *, resolution_ns: int = 1_000_000
    ) -> None:
        if resolution_ns <= 0:
            raise ValueError(f"resolution_ns must be positive, got {resolution_ns!r}")
        self._now = now
        self._resolution_ns = resolution_ns
        self._last = 0

    @property
    def resolution_ns(self) -> int:
        """The tick unit in nanoseconds."""
        return self._resolution_ns

    def tick(self) -> int:
        """Read the engine clock and return the current logical tick.

        Raises :class:`NonDeterminismError` if the reading went backwards.
        """
        current = _epoch_nanos(self._now()) // self._resolution_ns
        if current < self._last:
            raise NonDeterminismError(
                f"workflow clock went backwards ({current} < {self._last}); a "
                f"replayed clock cannot regress, so this reading did not come "
                f"from the engine"
            )
        self._last = current
        return current

    def last_tick(self) -> int:
        """The most recent tick, without reading the engine clock again."""
        return self._last

    def __repr__(self) -> str:
        return f"<WorkflowClock resolution_ns={self._resolution_ns} last={self._last}>"


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


@runtime_checkable
class WorkflowScheduler(Protocol):
    """The two engine operations a reactive workflow needs.

    Implement over the engine's own API — under temporal.io, ``start_timer``
    wraps ``workflow.sleep`` / ``workflow.start_timer`` and ``start_activity``
    wraps ``workflow.start_activity``. Both return whatever the engine returns
    (normally an awaitable); this module never awaits them, so the workflow
    author stays in control of ordering.
    """

    def start_timer(self, delay: int) -> Any:
        """Start a durable timer ``delay`` ticks from now."""
        ...

    def start_activity(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """Start a durable activity — the only sanctioned side effect."""
        ...


# ---------------------------------------------------------------------------
# Determinism guard
# ---------------------------------------------------------------------------

#: What :func:`deterministic_scope` intercepts: ``(module, attribute)`` pairs
#: that are rebindable at runtime and that a reactive body can plausibly reach.
_GUARDED: tuple[tuple[Any, str], ...] = (
    (time, "time"),
    (time, "time_ns"),
    (time, "monotonic"),
    (time, "monotonic_ns"),
    (time, "perf_counter"),
    (time, "perf_counter_ns"),
    (random, "random"),
    (random, "randint"),
    (random, "randrange"),
    (random, "choice"),
    (random, "shuffle"),
    (random, "uniform"),
    (os, "urandom"),
)

#: ``uuid`` is guarded separately: importing it eagerly would pull it into every
#: ``import lazily``, and a workflow that never enters a scope never needs it.
_GUARDED_UUID: tuple[str, ...] = ("uuid1", "uuid4")


def _raiser(label: str) -> Callable[..., Any]:
    def blocked(*_args: Any, **_kwargs: Any) -> Any:
        raise NonDeterminismError(
            f"{label} is not replayable; a workflow-scoped body must take time "
            f"from the WorkflowClock and randomness/ids from a durable activity"
        )

    return blocked


@contextlib.contextmanager
def deterministic_scope() -> Iterator[None]:
    """Make the reachable non-determinism raise for the duration of the block.

    Rebinds ``time.time`` / ``time_ns`` / ``monotonic`` / ``monotonic_ns`` /
    ``perf_counter`` / ``perf_counter_ns``, the module-level ``random``
    functions, ``os.urandom``, and ``uuid.uuid1`` / ``uuid4`` to functions that
    raise :class:`NonDeterminismError`, and restores every one of them on exit —
    including on an exception. Nesting is safe (each level restores what it
    saw).

    **What it does not catch, and why.** ``datetime.datetime.now()`` and
    ``utcnow()`` are methods on a C type whose attributes cannot be rebound, and
    a name already bound by ``from datetime import datetime`` would not see a
    module-level patch anyway. The same holds for any non-deterministic call a
    C extension makes internally, and for I/O. Catching those needs module
    re-import isolation — which is what temporal.io's own workflow sandbox does,
    and the right layer for it. This scope is the cheap, dependency-free guard
    for the cases a reactive body actually reaches; it is not a sandbox, and
    saying otherwise would be worse than not having it.

    It rebinds module attributes, so a reference resolved **before** the scope
    opened still points at the original — ``f = time.time`` outside, ``f()``
    inside, is not guarded. Same escape as ``from datetime import datetime``, and
    the same reason: this is a guard for what a body reaches through the module,
    not a sandbox.

    It is also **process-global while active**, like any monkeypatch. Enter it
    around the workflow body, not around an entire event loop shared with
    non-workflow code.
    """
    saved: list[tuple[Any, str, Any]] = []
    try:
        for module, attribute in _GUARDED:
            original = getattr(module, attribute)
            saved.append((module, attribute, original))
            setattr(module, attribute, _raiser(f"{module.__name__}.{attribute}()"))
        import uuid

        for attribute in _GUARDED_UUID:
            original = getattr(uuid, attribute)
            saved.append((uuid, attribute, original))
            setattr(uuid, attribute, _raiser(f"uuid.{attribute}()"))
        yield
    finally:
        for module, attribute, original in reversed(saved):
            setattr(module, attribute, original)


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


class WorkflowContext:
    """The reactive context of one durable workflow run.

    Owns the registered :class:`~lazily.temporal.TimelineSource` set and is the
    only sanctioned way to advance it, so every source in the graph sees the same
    replayed ``now`` at the same points.
    """

    __slots__ = ("_clock", "_ctx", "_scheduler", "_sources")

    def __init__(
        self,
        ctx: dict,
        clock: WorkflowClock,
        *,
        scheduler: WorkflowScheduler | None = None,
    ) -> None:
        self._ctx = ctx
        self._clock = clock
        self._scheduler = scheduler
        self._sources: list[TimelineSource] = []

    @property
    def ctx(self) -> dict:
        """The lazily context the workflow's reactive nodes live in."""
        return self._ctx

    @property
    def clock(self) -> WorkflowClock:
        """The engine-bound clock."""
        return self._clock

    @property
    def scheduler(self) -> WorkflowScheduler | None:
        """The engine scheduler, when one was injected."""
        return self._scheduler

    # -- sources ------------------------------------------------------------ #

    def register[S: TimelineSource](self, source: S) -> S:
        """Register ``source`` to be driven by :meth:`advance`. Returns it."""
        self._sources.append(source)
        return source

    def sources(self) -> list[TimelineSource]:
        """The registered sources, in registration order."""
        return list(self._sources)

    def advance(self) -> list[TimelineSource]:
        """Read the engine clock once and tick **every** registered source.

        One clock reading drives the whole set, so two sources can never
        disagree about what time it is — the shape that makes a replay diverge
        from the original run. Returns the sources that fired on this tick, in
        registration order.
        """
        now = self._clock.tick()
        return [source for source in self._sources if source.tick(now)]

    def next_wakeup(self) -> int | None:
        """Ticks until the earliest pending fire, or ``None`` when none is pending.

        ``0`` means a source is already due — a timer for ``0`` is a real
        instruction to the engine, not "nothing to do", so it is distinct from
        ``None``.
        """
        pending = [
            fire for fire in (s.next_fire() for s in self._sources) if fire is not None
        ]
        if not pending:
            return None
        return max(0, min(pending) - self._clock.last_tick())

    # -- engine hand-offs --------------------------------------------------- #

    def schedule_next(self) -> Any:
        """Hand the next wake-up to the engine's timer. ``None`` when none is due.

        Returns whatever the scheduler returns (normally an awaitable), so the
        workflow author decides when to await it.
        """
        delay = self.next_wakeup()
        if delay is None:
            return None
        return self._require_scheduler().start_timer(delay)

    def activity(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """Start a durable activity — the only sanctioned side effect.

        A reactive effect inside a workflow must not perform I/O itself: the
        effect reruns on replay, and the I/O would rerun with it. Routing it
        here makes the engine responsible for running it exactly once and for
        replaying its recorded result.
        """
        return self._require_scheduler().start_activity(name, *args, **kwargs)

    def _require_scheduler(self) -> WorkflowScheduler:
        if self._scheduler is None:
            raise NonDeterminismError(
                "no WorkflowScheduler was injected; a workflow cannot sleep or "
                "perform a side effect except through the engine"
            )
        return self._scheduler

    # -- guarded execution -------------------------------------------------- #

    def run[T](self, body: Callable[[], T]) -> T:
        """Run ``body`` inside :func:`deterministic_scope` and return its result."""
        with deterministic_scope():
            return body()

    def __repr__(self) -> str:
        return (
            f"<WorkflowContext sources={len(self._sources)} "
            f"tick={self._clock.last_tick()}>"
        )


def workflow_context(
    ctx: dict,
    now: Callable[[], Any],
    *,
    scheduler: WorkflowScheduler | None = None,
    resolution_ns: int = 1_000_000,
) -> WorkflowContext:
    """Build a :class:`WorkflowContext` from the engine's clock accessor."""
    return WorkflowContext(
        ctx, WorkflowClock(now, resolution_ns=resolution_ns), scheduler=scheduler
    )
