"""A state chart that projects an external authority (``#lzpyprojectedchart``).

:mod:`lazily.statechart` owns its transitions: you send it an event and it
decides where to go. That is the wrong shape when the state lives somewhere
else. A workflow run's ``status`` column is owned by Postgres and advanced by
Temporal; the database is the authority and the graph is strictly downstream of
it. Sending such a chart an event would fork the model from the row.

So this chart never decides anything. You **feed it observed states** and it
answers two questions the authority cannot:

1. **Was that transition legal?** The authority is still the authority — an
   observation is always adopted — but a step the declared lifecycle does not
   allow is recorded as an :class:`IllegalTransition` and counted on a reactive
   cell. A projection that refused the row would silently disagree with the
   database, which is worse than not modelling it at all. A projection that
   adopted it *quietly* would tell you nothing. It adopts and says so.
2. **Is it wedged?** Sitting in a state past that state's deadline with no
   advance. :meth:`ProjectedChart.wedged` is a derived cell, so an effect,
   alarm, or health cell reading it is invalidated exactly on the edge — the
   declarative form of the hand-written "is it still ``started`` and has it been
   too long" branch that otherwise accretes in a service.

Because :meth:`~ProjectedChartCore.tick` / :meth:`~ProjectedChartCore.next_fire`
match :class:`~lazily.temporal.TimelineSource`, the wedge composes with the rest
of the time-operator family: register it with a
:class:`~lazily.workflow.WorkflowContext` and the wedge check becomes a durable
engine timer instead of a polling loop.

**Two clocks, one time base.** ``at`` on an observation is the *authority's*
timestamp for that row, and staleness is last-writer-wins on it, so an
out-of-order delivery cannot walk the state backwards. ``tick(now)`` is the
*observer's* clock and is what makes a deadline elapse. The wedge compares the
two, so they must share a time base (both epoch milliseconds, which is what a
database timestamp and a local clock reading naturally are).

**A repeated observation does not reset the deadline.** Re-observing the state
the chart is already in advances the authority timestamp but *not*
``entered_at``: a poller that keeps reading the same ``started`` row is evidence
the run is wedged, not evidence it just advanced. Resetting on re-observation
would make the wedge unreachable, which is the bug this module exists to
express.

Example::

    defn = ProjectedChartDef.of(
        initial="queued",
        transitions={
            "queued": ["started"],
            "started": ["completed", "failed"],
            "completed": [],
            "failed": ["started"],  # a retry is legal
        },
        deadlines={"queued": 30_000, "started": 300_000},
        terminal=["completed"],
    )
    chart = ProjectedChart(ctx, defn)

    chart.observe("started", at=row.updated_at_ms)  # adopt what the DB says
    chart.tick(now_ms)  # elapse the deadline

    if chart.wedged():  # a derived cell
        alert(chart.state(), chart.overdue_by())
"""

from __future__ import annotations


__all__ = [
    "IllegalTransition",
    "ProjectedChart",
    "ProjectedChartCore",
    "ProjectedChartDef",
    "ProjectionOutcome",
    "ProjectionOutcomeKind",
]

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .cell import Cell


if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping


class ProjectionOutcomeKind(StrEnum):
    """What one observation did to the projection."""

    #: A legal transition into a different state.
    ENTERED = "entered"
    #: The state the chart was already in. ``entered_at`` is unchanged.
    REAFFIRMED = "reaffirmed"
    #: Adopted, but the declared lifecycle does not allow this step.
    ILLEGAL = "illegal"
    #: Adopted, but the chart has never heard of this state.
    UNKNOWN_STATE = "unknown_state"
    #: Older than the last adopted observation; dropped.
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class ProjectionOutcome:
    """The result of one :meth:`ProjectedChartCore.observe`."""

    kind: ProjectionOutcomeKind
    from_state: str
    to_state: str
    at: int

    @property
    def adopted(self) -> bool:
        """Whether the chart now reports ``to_state``.

        True for everything but :attr:`~ProjectionOutcomeKind.STALE` — the
        authority wins, including when its step was illegal.
        """
        return self.kind is not ProjectionOutcomeKind.STALE

    @property
    def legal(self) -> bool:
        """Whether the declared lifecycle justifies the step."""
        return self.kind not in (
            ProjectionOutcomeKind.ILLEGAL,
            ProjectionOutcomeKind.UNKNOWN_STATE,
        )


@dataclass(frozen=True, slots=True)
class IllegalTransition:
    """An adopted step the declared lifecycle does not allow."""

    from_state: str
    to_state: str
    at: int
    reason: ProjectionOutcomeKind

    def __str__(self) -> str:
        if self.reason is ProjectionOutcomeKind.UNKNOWN_STATE:
            return f"observed undeclared state {self.to_state!r} at {self.at}"
        return (
            f"observed {self.from_state!r} -> {self.to_state!r} at {self.at}, "
            "which the declared lifecycle does not allow"
        )


@dataclass(frozen=True, slots=True)
class ProjectedChartDef:
    """The lifecycle the authority is *expected* to follow.

    ``transitions`` maps each state to the states that may legally follow it.
    ``deadlines`` is how long a state may be occupied before it counts as wedged
    (milliseconds); a state with no deadline never wedges. ``terminal`` states
    never wedge either — a finished run is not late.
    """

    initial: str
    transitions: Mapping[str, frozenset[str]]
    deadlines: Mapping[str, int]
    terminal: frozenset[str]

    @classmethod
    def of(
        cls,
        *,
        initial: str,
        transitions: Mapping[str, Iterable[str]],
        deadlines: Mapping[str, int] | None = None,
        terminal: Iterable[str] | None = None,
    ) -> ProjectedChartDef:
        """Build a definition, validating it closes over its own states."""
        built = {state: frozenset(targets) for state, targets in transitions.items()}
        declared = set(built)
        if initial not in declared:
            msg = f"initial state {initial!r} is not declared in transitions"
            raise ValueError(msg)
        for state, targets in built.items():
            undeclared = sorted(targets - declared)
            if undeclared:
                msg = (
                    f"state {state!r} names undeclared successor(s) "
                    f"{undeclared}; declare them in transitions"
                )
                raise ValueError(msg)
        deadline_map = dict(deadlines or {})
        for state, deadline in deadline_map.items():
            if state not in declared:
                msg = f"deadline names undeclared state {state!r}"
                raise ValueError(msg)
            if deadline <= 0:
                msg = f"deadline for {state!r} must be positive, got {deadline}"
                raise ValueError(msg)
        terminal_set = frozenset(terminal or ())
        undeclared_terminal = sorted(terminal_set - declared)
        if undeclared_terminal:
            msg = f"terminal names undeclared state(s) {undeclared_terminal}"
            raise ValueError(msg)
        return cls(
            initial=initial,
            transitions=built,
            deadlines=deadline_map,
            terminal=terminal_set,
        )

    def knows(self, state: str) -> bool:
        return state in self.transitions

    def allows(self, from_state: str, to_state: str) -> bool:
        """Whether ``from_state -> to_state`` is a declared step."""
        return to_state in self.transitions.get(from_state, frozenset())

    def deadline(self, state: str) -> int | None:
        """The occupancy deadline for ``state``, or ``None`` if it never wedges."""
        if state in self.terminal:
            return None
        return self.deadlines.get(state)


class ProjectedChartCore:
    """The graph-agnostic projection: observe, validate, expose the wedge.

    Pure state machine — no cells, no clock of its own. Both clock inputs are
    passed in, which is what makes the whole thing replayable (see
    :mod:`lazily.replay`).
    """

    __slots__ = (
        "_defn",
        "_entered_at",
        "_now",
        "_observations",
        "_observed_at",
        "_state",
        "_violations",
    )

    def __init__(self, defn: ProjectedChartDef, *, at: int = 0) -> None:
        self._defn = defn
        self._state = defn.initial
        self._entered_at = at
        self._observed_at = at
        self._now = at
        self._observations = 0
        self._violations: list[IllegalTransition] = []

    @property
    def defn(self) -> ProjectedChartDef:
        return self._defn

    @property
    def state(self) -> str:
        """The state the authority was last seen in."""
        return self._state

    @property
    def entered_at(self) -> int:
        """The authority timestamp at which the current state was entered."""
        return self._entered_at

    @property
    def observed_at(self) -> int:
        """The authority timestamp of the last adopted observation."""
        return self._observed_at

    @property
    def now(self) -> int:
        """The observer clock, advanced by :meth:`tick`."""
        return self._now

    @property
    def observations(self) -> int:
        """How many observations were adopted."""
        return self._observations

    @property
    def violations(self) -> tuple[IllegalTransition, ...]:
        """Every adopted step the lifecycle did not justify, oldest first."""
        return tuple(self._violations)

    @property
    def last_violation(self) -> IllegalTransition | None:
        return self._violations[-1] if self._violations else None

    def observe(self, state: str, *, at: int) -> ProjectionOutcome:
        """Adopt one observed state and say what it was.

        Older-than-last observations are dropped (last-writer-wins on the
        authority's own timestamp), so an out-of-order delivery cannot walk the
        state backwards. Everything else is adopted, including a step the
        lifecycle forbids — the authority owns the state; this chart only owns
        the opinion about it.
        """
        previous = self._state
        if at < self._observed_at:
            return ProjectionOutcome(
                kind=ProjectionOutcomeKind.STALE,
                from_state=previous,
                to_state=state,
                at=at,
            )

        if not self._defn.knows(state):
            kind = ProjectionOutcomeKind.UNKNOWN_STATE
        elif state == previous:
            kind = ProjectionOutcomeKind.REAFFIRMED
        elif self._defn.allows(previous, state):
            kind = ProjectionOutcomeKind.ENTERED
        else:
            kind = ProjectionOutcomeKind.ILLEGAL

        self._observations += 1
        self._observed_at = at
        if at > self._now:
            self._now = at
        if kind is not ProjectionOutcomeKind.REAFFIRMED:
            # A re-observation of the same state is deliberately NOT an entry:
            # a poller re-reading one row must not reset the wedge deadline.
            self._state = state
            self._entered_at = at

        outcome = ProjectionOutcome(
            kind=kind, from_state=previous, to_state=state, at=at
        )
        if not outcome.legal:
            self._violations.append(
                IllegalTransition(
                    from_state=previous, to_state=state, at=at, reason=kind
                )
            )
        return outcome

    def tick(self, now: int) -> bool:
        """Advance the observer clock; returns the wedge edge.

        Strictly monotone: a backwards clock reading raises rather than being
        clamped, because a deadline computed from a regressing clock is not a
        deadline. (:class:`~lazily.temporal.ManualClock` clamps, which is right
        for a game loop and wrong here.)
        """
        if now < self._now:
            msg = f"observer clock moved backwards: {now} < {self._now}"
            raise ValueError(msg)
        before = self.wedged
        self._now = now
        return self.wedged and not before

    @property
    def wedged(self) -> bool:
        """In a state past its deadline with no advance."""
        return self.overdue_by > 0

    @property
    def overdue_by(self) -> int:
        """Milliseconds past the current state's deadline, ``0`` if not wedged."""
        deadline = self._defn.deadline(self._state)
        if deadline is None:
            return 0
        return max(0, self._now - self._entered_at - deadline)

    @property
    def wedges_at(self) -> int | None:
        """When the current state wedges, or ``None`` if it never does."""
        deadline = self._defn.deadline(self._state)
        if deadline is None:
            return None
        return self._entered_at + deadline

    def next_fire(self) -> int | None:
        """:class:`~lazily.temporal.TimelineSource`: the next wedge instant."""
        wedges_at = self.wedges_at
        if wedges_at is None or self.wedged:
            return None
        return wedges_at


class ProjectedChart:
    """Reactive shell: the projected state and the wedge as cells.

    ``state`` / ``wedged`` / ``violation_count`` are backed by cells, so the
    ``!=`` store guard means a re-observation of the same state invalidates
    nothing and an effect on the wedge runs exactly on the edge.
    """

    __slots__ = ("_core", "_state", "_violation_count", "_wedged", "ctx")

    def __init__(self, ctx: dict, defn: ProjectedChartDef, *, at: int = 0) -> None:
        self.ctx = ctx
        self._core = ProjectedChartCore(defn, at=at)
        self._state: Cell[str] = Cell(ctx, self._core.state)
        self._wedged: Cell[bool] = Cell(ctx, self._core.wedged)
        self._violation_count: Cell[int] = Cell(ctx, 0)

    @property
    def core(self) -> ProjectedChartCore:
        """The underlying projection, for the non-reactive reads."""
        return self._core

    def observe(self, state: str, *, at: int) -> ProjectionOutcome:
        """Adopt one observed state, publishing any change onto the cells."""
        outcome = self._core.observe(state, at=at)
        self._publish()
        return outcome

    def tick(self, now: int) -> bool:
        """Advance the observer clock; returns the wedge edge."""
        edge = self._core.tick(now)
        self._publish()
        return edge

    def _publish(self) -> None:
        self._state.value = self._core.state
        self._wedged.value = self._core.wedged
        self._violation_count.value = len(self._core.violations)

    def state(self, ctx: Any = None) -> str:
        """The projected state (reactive read)."""
        if ctx is None:
            return self._state.value
        return ctx.read(self._state)

    def wedged(self, ctx: Any = None) -> bool:
        """Whether the authority is wedged (reactive read)."""
        if ctx is None:
            return self._wedged.value
        return ctx.read(self._wedged)

    def violation_count(self, ctx: Any = None) -> int:
        """How many adopted steps the lifecycle did not justify (reactive)."""
        if ctx is None:
            return self._violation_count.value
        return ctx.read(self._violation_count)

    def state_cell(self) -> Cell[str]:
        return self._state

    def wedged_cell(self) -> Cell[bool]:
        return self._wedged

    def violation_count_cell(self) -> Cell[int]:
        return self._violation_count

    @property
    def violations(self) -> tuple[IllegalTransition, ...]:
        return self._core.violations

    @property
    def last_violation(self) -> IllegalTransition | None:
        return self._core.last_violation

    @property
    def overdue_by(self) -> int:
        return self._core.overdue_by

    def next_fire(self) -> int | None:
        """:class:`~lazily.temporal.TimelineSource`: the next wedge instant."""
        return self._core.next_fire()
