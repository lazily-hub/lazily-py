"""Workflow-safe reactive context (``#lzpyworkflowdeterminism``).

The property the module exists for: a reactive graph inside a replayed workflow
takes its time from the engine, and anything that is not replayable raises at the
line that reached it rather than diverging the replay silently.
"""

from __future__ import annotations

import os
import random
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from lazily import (
    DeadlineCore,
    IntervalCore,
    NonDeterminismError,
    TimerCore,
    WorkflowClock,
    WorkflowContext,
    WorkflowScheduler,
    deterministic_scope,
    workflow_context,
)


class FakeEngine:
    """A test double for the two operations a durable engine has to provide."""

    def __init__(self, *, start_ms: int = 0) -> None:
        self.now_ms = start_ms
        self.timers: list[int] = []
        self.activities: list[tuple[str, tuple, dict]] = []

    # The clock accessor — ``workflow.now`` under temporal.io.
    def now(self) -> datetime:
        return datetime.fromtimestamp(self.now_ms / 1000, tz=UTC)

    def start_timer(self, delay: int) -> str:
        self.timers.append(delay)
        return f"timer:{delay}"

    def start_activity(self, name: str, *args: Any, **kwargs: Any) -> str:
        self.activities.append((name, args, kwargs))
        return f"activity:{name}"


# -- clock --------------------------------------------------------------------


def test_clock_reads_the_engine_on_every_tick() -> None:
    engine = FakeEngine(start_ms=1_000)
    clock = WorkflowClock(engine.now)

    assert clock.tick() == 1_000
    engine.now_ms = 2_500
    assert clock.tick() == 2_500
    assert clock.last_tick() == 2_500


def test_clock_refuses_to_go_backwards() -> None:
    """A clamp would hide exactly the bug this module exists to surface."""
    engine = FakeEngine(start_ms=5_000)
    clock = WorkflowClock(engine.now)
    assert clock.tick() == 5_000

    engine.now_ms = 4_000
    with pytest.raises(NonDeterminismError, match="went backwards"):
        clock.tick()


@pytest.mark.parametrize(
    ("reading", "expected"),
    [
        (1_500_000_000, 1_500),  # int nanoseconds
        (1.5, 1_500),  # float seconds
        (datetime.fromtimestamp(1.5, tz=UTC), 1_500),
    ],
)
def test_clock_normalizes_engine_reading_shapes(reading: Any, expected: int) -> None:
    assert WorkflowClock(lambda: reading).tick() == expected


def test_naive_datetime_is_read_as_utc() -> None:
    aware = datetime.fromtimestamp(1.5, tz=UTC)
    naive = aware.replace(tzinfo=None)
    assert WorkflowClock(lambda: naive).tick() == WorkflowClock(lambda: aware).tick()


def test_clock_rejects_an_unusable_reading() -> None:
    with pytest.raises(TypeError, match="expected a datetime"):
        WorkflowClock(lambda: "now").tick()


def test_resolution_must_be_positive() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        WorkflowClock(lambda: 0, resolution_ns=0)


def test_nanosecond_resolution() -> None:
    clock = WorkflowClock(lambda: 1_500_000_000, resolution_ns=1)
    assert clock.tick() == 1_500_000_000


# -- source driving -----------------------------------------------------------


def test_one_clock_reading_drives_every_source() -> None:
    engine = FakeEngine()
    wf = workflow_context({}, engine.now, scheduler=engine)
    early = wf.register(TimerCore(fire_at=100))
    late = wf.register(TimerCore(fire_at=500))

    engine.now_ms = 100
    assert wf.advance() == [early]
    assert early.fired() and not late.fired()

    engine.now_ms = 500
    assert wf.advance() == [late]


def test_advance_returns_fires_in_registration_order() -> None:
    engine = FakeEngine()
    wf = workflow_context({}, engine.now, scheduler=engine)
    first = wf.register(TimerCore(fire_at=10))
    second = wf.register(TimerCore(fire_at=10))

    engine.now_ms = 10
    assert wf.advance() == [first, second]
    assert wf.sources() == [first, second]


def test_next_wakeup_is_the_earliest_pending_fire() -> None:
    engine = FakeEngine()
    wf = workflow_context({}, engine.now, scheduler=engine)
    wf.register(TimerCore(fire_at=800))
    wf.register(IntervalCore(period=300))

    wf.advance()  # tick 0
    assert wf.next_wakeup() == 300

    engine.now_ms = 300
    wf.advance()
    assert wf.next_wakeup() == 300  # the interval's next period


def test_next_wakeup_is_none_when_nothing_is_pending() -> None:
    engine = FakeEngine()
    wf = workflow_context({}, engine.now, scheduler=engine)
    timer = wf.register(TimerCore(fire_at=10))
    engine.now_ms = 10
    wf.advance()
    assert timer.fired()
    assert wf.next_wakeup() is None


def test_an_already_due_source_reports_zero_not_none() -> None:
    """``0`` is an instruction to the engine; ``None`` means there is none."""
    engine = FakeEngine(start_ms=900)
    wf = workflow_context({}, engine.now, scheduler=engine)
    wf.register(DeadlineCore(deadline=100))
    wf.clock.tick()
    assert wf.next_wakeup() == 0


# -- engine hand-offs ---------------------------------------------------------


def test_schedule_next_hands_the_delay_to_the_engine() -> None:
    engine = FakeEngine()
    wf = workflow_context({}, engine.now, scheduler=engine)
    wf.register(TimerCore(fire_at=250))
    wf.advance()

    assert wf.schedule_next() == "timer:250"
    assert engine.timers == [250]


def test_schedule_next_is_none_with_nothing_pending() -> None:
    engine = FakeEngine()
    wf = workflow_context({}, engine.now, scheduler=engine)
    assert wf.schedule_next() is None
    assert engine.timers == []


def test_activity_is_the_sanctioned_side_effect() -> None:
    engine = FakeEngine()
    wf = workflow_context({}, engine.now, scheduler=engine)
    assert wf.activity("charge_card", 42, currency="usd") == "activity:charge_card"
    assert engine.activities == [("charge_card", (42,), {"currency": "usd"})]


def test_sleeping_or_acting_without_an_engine_raises() -> None:
    engine = FakeEngine()
    wf = workflow_context({}, engine.now)
    wf.register(TimerCore(fire_at=10))
    wf.advance()

    with pytest.raises(NonDeterminismError, match="no WorkflowScheduler"):
        wf.schedule_next()
    with pytest.raises(NonDeterminismError, match="no WorkflowScheduler"):
        wf.activity("charge_card")


def test_scheduler_protocol_is_structural() -> None:
    assert isinstance(FakeEngine(), WorkflowScheduler)


# -- determinism guard --------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda: time.time(),
        lambda: time.time_ns(),
        lambda: time.monotonic(),
        lambda: time.monotonic_ns(),
        lambda: time.perf_counter(),
        lambda: time.perf_counter_ns(),
        lambda: random.random(),
        lambda: random.randint(0, 1),
        lambda: random.randrange(2),
        lambda: random.choice([1, 2]),
        lambda: random.shuffle([1, 2]),
        lambda: random.uniform(0, 1),
        lambda: os.urandom(4),
        lambda: uuid.uuid1(),
        lambda: uuid.uuid4(),
    ],
)
def test_guarded_calls_raise_inside_the_scope(call) -> None:
    with deterministic_scope(), pytest.raises(NonDeterminismError, match="replayable"):
        call()


def test_the_scope_restores_everything_it_patched() -> None:
    originals = (time.time, time.monotonic, random.random, os.urandom, uuid.uuid4)
    with deterministic_scope():
        assert time.time is not originals[0]
    assert (
        time.time,
        time.monotonic,
        random.random,
        os.urandom,
        uuid.uuid4,
    ) == originals


def test_the_scope_restores_on_an_exception() -> None:
    original = time.time
    with pytest.raises(ZeroDivisionError), deterministic_scope():
        _ = 1 / 0
    assert time.time is original


def test_the_scope_nests() -> None:
    original = time.time
    with deterministic_scope():
        with deterministic_scope():
            with pytest.raises(NonDeterminismError):
                time.time()
        # still guarded by the outer scope
        with pytest.raises(NonDeterminismError):
            time.time()
    assert time.time is original


def test_the_engine_clock_still_works_inside_the_scope() -> None:
    """The guard blocks wall time; it must not block the replayed clock."""
    engine = FakeEngine(start_ms=7_000)
    wf = workflow_context({}, engine.now, scheduler=engine)
    timer = wf.register(TimerCore(fire_at=7_000))

    with deterministic_scope():
        assert wf.advance() == [timer]
    assert wf.clock.last_tick() == 7_000


def test_run_executes_the_body_under_the_guard() -> None:
    engine = FakeEngine()
    wf = workflow_context({}, engine.now, scheduler=engine)

    assert wf.run(lambda: 21 * 2) == 42
    with pytest.raises(NonDeterminismError):
        wf.run(lambda: time.time())
    assert time.time is not None  # restored


def test_a_reference_bound_before_the_scope_escapes_the_guard() -> None:
    """The monkeypatch limitation, asserted rather than discovered later.

    ``deterministic_scope`` rebinds module attributes, so a name resolved before
    the scope opened still points at the original. This is the same escape as
    ``from datetime import datetime``, and it is why the scope is documented as
    a guard for what a reactive body reaches, not as a sandbox.
    """
    escaped = time.time  # bound before the scope
    with deterministic_scope():
        assert isinstance(escaped(), float)  # not guarded
        with pytest.raises(NonDeterminismError):
            time.time()  # resolved through the module: guarded


# -- surface ------------------------------------------------------------------


def test_context_exposes_its_wiring() -> None:
    engine = FakeEngine()
    ctx: dict = {}
    wf = WorkflowContext(ctx, WorkflowClock(engine.now), scheduler=engine)
    assert wf.ctx is ctx
    assert wf.scheduler is engine
    assert wf.clock.resolution_ns == 1_000_000
    assert "WorkflowContext" in repr(wf)
    assert "WorkflowClock" in repr(wf.clock)


def test_lazily_temporal_says_it_is_not_temporal_io() -> None:
    """The collision note is API, not a comment: it is what a reader lands on."""
    import lazily.temporal

    assert "not temporal.io" in (lazily.temporal.__doc__ or "")
    assert "lazily.workflow" in (lazily.temporal.__doc__ or "")
