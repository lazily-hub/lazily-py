"""A chart that projects an external authority (``#lzpyprojectedchart``).

The properties the module exists for:

* the authority always wins — an observation is adopted even when the declared
  lifecycle forbids it — and the chart says so instead of quietly agreeing or
  loudly refusing;
* a repeated observation of the same state does **not** reset the deadline, so
  a poller re-reading one row is evidence of a wedge rather than of progress;
* out-of-order delivery cannot walk the state backwards (last-writer-wins on
  the authority's own timestamp);
* `wedged` is a derived cell, so an effect on it runs exactly on the edge.
"""

from __future__ import annotations

from typing import Any

import pytest

from lazily import (
    IllegalTransition,
    ProjectedChart,
    ProjectedChartCore,
    ProjectedChartDef,
    ProjectionOutcomeKind,
    ReplayEvent,
    ReplayHarness,
    ReplayLog,
    computed,
    effect,
)


# The lifecycle FPE's `workflow_run.status` is expected to follow. Postgres and
# Temporal own the column; this is only what this side believes about it.
def _defn() -> ProjectedChartDef:
    return ProjectedChartDef.of(
        initial="queued",
        transitions={
            "queued": ["started", "failed"],
            "started": ["completed", "failed"],
            "completed": [],
            "failed": ["started"],  # a retry is legal
        },
        deadlines={"queued": 30_000, "started": 300_000},
        terminal=["completed"],
    )


def _core(**kwargs: Any) -> ProjectedChartCore:
    return ProjectedChartCore(_defn(), **kwargs)


# -- the definition -----------------------------------------------------------


def test_a_definition_must_close_over_its_own_states() -> None:
    with pytest.raises(ValueError, match="undeclared successor"):
        ProjectedChartDef.of(initial="a", transitions={"a": ["b"]})

    with pytest.raises(ValueError, match="initial state"):
        ProjectedChartDef.of(initial="z", transitions={"a": []})

    with pytest.raises(ValueError, match="deadline names undeclared"):
        ProjectedChartDef.of(initial="a", transitions={"a": []}, deadlines={"b": 1})

    with pytest.raises(ValueError, match="terminal names undeclared"):
        ProjectedChartDef.of(initial="a", transitions={"a": []}, terminal=["b"])

    with pytest.raises(ValueError, match="must be positive"):
        ProjectedChartDef.of(initial="a", transitions={"a": []}, deadlines={"a": 0})


def test_terminal_states_never_wedge_even_with_a_deadline() -> None:
    defn = ProjectedChartDef.of(
        initial="a",
        transitions={"a": ["b"], "b": []},
        deadlines={"a": 10, "b": 10},
        terminal=["b"],
    )

    assert defn.deadline("a") == 10
    assert defn.deadline("b") is None


# -- adopting the authority ---------------------------------------------------


def test_a_legal_step_is_entered() -> None:
    core = _core()

    outcome = core.observe("started", at=1_000)

    assert outcome.kind is ProjectionOutcomeKind.ENTERED
    assert outcome.adopted
    assert outcome.legal
    assert core.state == "started"
    assert core.entered_at == 1_000
    assert core.violations == ()


def test_an_illegal_step_is_adopted_and_recorded() -> None:
    core = _core()
    # queued -> completed skips the run entirely. Postgres says it happened.
    outcome = core.observe("completed", at=1_000)

    assert outcome.kind is ProjectionOutcomeKind.ILLEGAL
    assert outcome.adopted is True
    assert outcome.legal is False
    # Adopted: refusing it would fork this side from the row it is projecting.
    assert core.state == "completed"
    assert core.violations == (
        IllegalTransition(
            from_state="queued",
            to_state="completed",
            at=1_000,
            reason=ProjectionOutcomeKind.ILLEGAL,
        ),
    )
    assert "does not allow" in str(core.last_violation)


def test_an_undeclared_state_is_adopted_and_recorded() -> None:
    core = _core()

    outcome = core.observe("cancelling", at=1_000)

    assert outcome.kind is ProjectionOutcomeKind.UNKNOWN_STATE
    assert outcome.adopted is True
    assert outcome.legal is False
    assert core.state == "cancelling"
    assert core.last_violation is not None
    assert "undeclared state" in str(core.last_violation)
    # No deadline is knowable for a state the chart has never heard of, so it
    # cannot wedge — the violation is the signal, not a fabricated deadline.
    core.tick(10_000_000)
    assert core.wedged is False


def test_an_out_of_order_observation_cannot_walk_the_state_backwards() -> None:
    core = _core()
    core.observe("started", at=2_000)

    outcome = core.observe("queued", at=1_000)

    assert outcome.kind is ProjectionOutcomeKind.STALE
    assert outcome.adopted is False
    assert core.state == "started"
    assert core.observed_at == 2_000
    assert core.observations == 1


def test_a_same_timestamp_observation_is_not_stale() -> None:
    core = _core()
    core.observe("started", at=2_000)

    # Two rows written in the same millisecond is ordinary, not out-of-order.
    assert core.observe("failed", at=2_000).kind is ProjectionOutcomeKind.ENTERED
    assert core.state == "failed"


# -- the wedge ----------------------------------------------------------------


def test_a_repeated_observation_does_not_reset_the_deadline() -> None:
    core = _core()
    core.observe("started", at=0)

    # A poller re-reading the same `started` row every minute.
    for at in (60_000, 120_000, 180_000, 240_000, 300_000):
        outcome = core.observe("started", at=at)
        assert outcome.kind is ProjectionOutcomeKind.REAFFIRMED
        assert outcome.legal

    assert core.entered_at == 0, "re-observation must not count as an entry"
    assert core.observed_at == 300_000
    assert core.observations == 6

    core.tick(300_001)
    assert core.wedged is True
    assert core.overdue_by == 1


def test_the_wedge_edge_fires_once() -> None:
    core = _core()
    core.observe("started", at=0)

    assert core.tick(299_999) is False
    assert core.wedged is False
    assert core.tick(300_000) is False, "at the deadline is not past it"
    assert core.tick(300_001) is True
    assert core.tick(400_000) is False, "already wedged, not a new edge"
    assert core.overdue_by == 100_000


def test_advancing_the_state_clears_the_wedge() -> None:
    core = _core()
    core.observe("started", at=0)
    core.tick(400_000)
    assert core.wedged is True

    core.observe("completed", at=400_000)

    assert core.wedged is False
    assert core.overdue_by == 0


def test_a_state_with_no_deadline_never_wedges() -> None:
    defn = ProjectedChartDef.of(
        initial="a", transitions={"a": ["b"], "b": []}, deadlines={"a": 10}
    )
    core = ProjectedChartCore(defn)
    core.observe("b", at=0)

    core.tick(10_000_000)

    assert core.wedged is False
    assert core.next_fire() is None


def test_next_fire_is_the_wedge_instant() -> None:
    core = _core()

    # Still queued, entered at 0, 30s deadline.
    assert core.next_fire() == 30_000

    core.observe("started", at=5_000)
    assert core.next_fire() == 305_000
    assert core.wedges_at == 305_000

    core.tick(400_000)
    # Already wedged: there is no future edge left to schedule.
    assert core.next_fire() is None


def test_the_observer_clock_refuses_to_go_backwards() -> None:
    core = _core()
    core.tick(1_000)

    with pytest.raises(ValueError, match="moved backwards"):
        core.tick(999)


def test_an_observation_carries_the_observer_clock_forward() -> None:
    core = _core()

    core.observe("started", at=5_000)

    assert core.now == 5_000
    core.tick(5_000)  # not a regression


def test_an_older_authority_timestamp_does_not_pull_the_clock_back() -> None:
    core = _core()
    core.tick(10_000)

    # A row written five seconds before the poll is ordinary, not stale.
    outcome = core.observe("started", at=5_000)

    assert outcome.kind is ProjectionOutcomeKind.ENTERED
    assert core.entered_at == 5_000
    assert core.now == 10_000


# -- the reactive shell -------------------------------------------------------


def test_the_wedge_is_a_derived_cell_that_fires_on_the_edge() -> None:
    ctx: dict = {}
    chart = ProjectedChart(ctx, _defn())
    seen: list[bool] = []

    alarm = effect(lambda c: seen.append(chart.wedged(c)))
    alarm(ctx)

    assert seen == [False]

    chart.observe("started", at=0)
    chart.observe("started", at=60_000)
    chart.tick(299_999)
    assert seen == [False], "no edge yet, so no invalidation"

    chart.tick(300_001)
    assert seen == [False, True]

    chart.tick(400_000)
    assert seen == [False, True], "still wedged is not a new edge"

    chart.observe("completed", at=400_000)
    assert seen == [False, True, False]


def test_a_reaffirmed_state_does_not_invalidate_the_state_cell() -> None:
    ctx: dict = {}
    chart = ProjectedChart(ctx, _defn())
    computes = 0

    def label(c: Any) -> str:
        nonlocal computes
        computes += 1
        return chart.state(c).upper()

    derived = computed(ctx, label)
    assert derived.value == "QUEUED"
    assert computes == 1

    chart.observe("started", at=0)
    assert derived.value == "STARTED"
    assert computes == 2

    chart.observe("started", at=60_000)
    assert derived.value == "STARTED"
    assert computes == 2, "the != store guard suppresses a reaffirmation"


def test_the_violation_count_is_reactive() -> None:
    ctx: dict = {}
    chart = ProjectedChart(ctx, _defn())
    counts: list[int] = []

    watcher = effect(lambda c: counts.append(chart.violation_count(c)))
    watcher(ctx)

    assert counts == [0]

    chart.observe("started", at=0)
    assert counts == [0]

    chart.observe("queued", at=1_000)  # started -> queued is not declared
    assert counts == [0, 1]
    assert chart.last_violation is not None
    assert chart.last_violation.to_state == "queued"


def test_the_shell_exposes_the_cells_and_the_core() -> None:
    ctx: dict = {}
    chart = ProjectedChart(ctx, _defn(), at=1_000)

    assert chart.state() == "queued"
    assert chart.wedged() is False
    assert chart.state_cell().value == "queued"
    assert chart.wedged_cell().value is False
    assert chart.violation_count_cell().value == 0
    assert chart.core.entered_at == 1_000
    assert chart.next_fire() == 31_000
    assert chart.overdue_by == 0
    assert chart.violations == ()


# -- the projection is replayable ---------------------------------------------


class ProjectedChartGraph:
    """The projection as a `ReplayGraph` — both its clocks are log inputs."""

    def __init__(self) -> None:
        self.core = _core()

    def apply(self, event: ReplayEvent) -> None:
        if event.name == "observe":
            state, at = event.payload
            self.core.observe(state, at=at)
        else:
            self.core.tick(event.payload)

    def observe(self) -> dict[str, Any]:
        return {
            "state": self.core.state,
            "wedged": self.core.wedged,
            "overdue_by": self.core.overdue_by,
            "violations": self.core.violations,
        }


def test_the_projection_replays_to_a_fingerprint() -> None:
    log = ReplayLog.from_records(
        [
            ("observe", ("started", 0)),
            ("observe", ("started", 60_000)),
            ("tick", 300_001),
            ("observe", ("queued", 300_001)),
            ("observe", ("started", 310_000)),
            ("tick", 700_000),
            ("observe", ("completed", 700_000)),
        ]
    )

    fingerprint = ReplayHarness(ProjectedChartGraph, deterministic=True).prove(log)

    assert fingerprint.final.seq == 6
    # The wedge, the illegal started -> queued step, and the recovery are all a
    # pure function of the log — which is the whole claim.
    assert set(fingerprint.final.as_dict()) == {
        "state",
        "wedged",
        "overdue_by",
        "violations",
    }
