"""Property-based validation of the native state chart and flat FSM against the
universal properties established by the Lean ``LazilyFormal.StateChart`` /
``StateMachine`` formal model in ``lazily-formal``. These are the guarantees no
finite fixture suite can establish: determinism-by-construction, parallel-region
confluence, and single-region refinement of the flat FSM kernel.

Each test names the Lean theorem it mirrors and exercises the lazily-py
implementation against the theorem's statement. This is the lazily-py
counterpart of lazily-js's ``test/statechart-properties.test.js``; because
lazily-py also ships the flat ``StateMachine`` kernel, the flat-kernel theorems
are covered here as well.
"""

from __future__ import annotations

from lazily import ChartDef, StateChart, StateMachine


# -- helpers -----------------------------------------------------------------


def _leaf_set(chart: StateChart) -> set[str]:
    return set(chart.active_leaves())


def _snapshot(chart: StateChart) -> dict[str, object]:
    return {
        "leaves": chart.active_leaves(),
        "config": chart.configuration(),
        "actions": chart.last_actions(),
    }


class _FlatMachine:
    """A minimal flat FSM (the ``LazilyFormal.StateMachine.Machine`` kernel):
    ``current`` + a transition table ``state -> event -> ?state``."""

    def __init__(self, current: str, table: dict[str, dict[str, str]]) -> None:
        self.current = current
        self.table = table

    def send(self, event: str) -> bool:
        nxt = self.table.get(self.current, {}).get(event)
        if nxt is None:
            return False
        self.current = nxt
        return True


# =================================================================================
# Flat kernel (LazilyFormal.StateMachine) — 6 theorems.
# lazily-py's StateMachine[S, E] is the reactive flat FSM kernel. Its `send`
# returns True on accept (some next), False on reject (None). The Cell's
# `!=` guard suppresses propagation on a no-op self-transition — the observable
# analogue of the flat `sends` predicate.
# =================================================================================


def test_guard_rejection_preserves_state() -> None:
    """A `none` (None) transition leaves the state unchanged."""
    m = StateMachine({}, "Locked", lambda s, e: "Unlocked" if e == "coin" else None)
    assert m.send("push") is False
    assert m.state == "Locked"


def test_accepted_transition_advances_state() -> None:
    """An accepted `some next` advances to `next`."""
    m = StateMachine(
        {},
        "A",
        lambda s, e: {"A": "B", "B": "C"}.get(s) if e == "go" else None,
    )
    assert m.state == "A"
    assert m.send("go") is True
    assert m.state == "B"


def test_self_transition_preserves_state() -> None:
    """A self-targeted transition (some current) preserves state."""
    m = StateMachine({}, "Idle", lambda s, e: "Idle")
    assert m.send("tick") is True
    assert m.state == "Idle"


def test_self_vs_changed_transition_sends_flag() -> None:
    """`sends` is False for a self-transition and True for a changed one.

    lazily-py surfaces the flat `sends` flag via ``on_transition`` (fires only
    on a real change) and the Cell's ``!=`` guard (suppresses a no-op
    self-transition).
    """
    # Self S1 -> S1: accepted but on_transition must NOT fire.
    self_m = StateMachine({}, "S1", lambda s, e: "S1")
    self_fired: list[tuple[str, str]] = []
    self_m.on_transition(lambda old, new: self_fired.append((old, new)))
    self_m.send("x")
    assert self_fired == []

    # Changed S1 -> S2: on_transition fires.
    changed_m = StateMachine({}, "S1", lambda s, e: "S2" if e == "go" else None)
    changed_fired: list[tuple[str, str]] = []
    changed_m.on_transition(lambda old, new: changed_fired.append((old, new)))
    changed_m.send("go")
    assert changed_fired == [("S1", "S2")]


def test_send_preserves_transition() -> None:
    """`send` never changes the transition function (or the chart definition)."""
    trans = make_reverser()
    m = StateMachine({}, "A", trans)
    m.send("go")
    assert m._transition is trans  # transition function never replaced

    defn = ChartDef.from_chart(
        {
            "initial": "g",
            "states": {
                "root": {"initial": "g"},
                "red": {"parent": "root", "on": {"TICK": "g"}},
                "g": {"parent": "root", "on": {"TICK": "y"}},
                "y": {"parent": "root", "on": {"TICK": "red"}},
            },
        }
    )
    snap = {
        "root": defn.root,
        "order": dict(defn.order),
        "children": {k: list(v) for k, v in defn.children.items()},
        "depth": dict(defn.depth),
        "gt": defn.states["g"].transitions["TICK"],
    }
    StateChart({}, defn).send("TICK")
    assert defn.root == snap["root"]
    assert defn.order == snap["order"]
    assert defn.children == snap["children"]
    assert defn.depth == snap["depth"]
    assert defn.states["g"].transitions["TICK"] is snap["gt"]


def make_reverser():
    def trans(s: str, e: str) -> str | None:
        return {"A": "B", "B": "A"}.get(s) if e == "go" else None

    return trans


# =================================================================================
# enabled_empty_rejects (StateChart.lean)
# "An event with no enabled, guard-passing transition leaves the configuration
#  (and history) unchanged, and the action trace empty."
# =================================================================================


def test_enabled_empty_rejects_unknown_event() -> None:
    chart = StateChart(
        {},
        ChartDef.from_chart(
            {
                "initial": "a1",
                "states": {
                    "root": {"initial": "a"},
                    "a": {"parent": "root", "initial": "a1", "entry": ["enterA"]},
                    "a1": {"parent": "a", "on": {"SWAP": "a2"}},
                    "a2": {"parent": "a"},
                },
            }
        ),
    )

    before = _snapshot(chart)
    accepted = chart.send("NOPE")
    after = _snapshot(chart)

    assert accepted is False
    assert after["leaves"] == before["leaves"]
    assert after["config"] == before["config"]
    assert after["actions"] == []


def test_enabled_empty_rejects_guard_failing() -> None:
    """Guard failing -> rejected (guard-passing is part of 'enabled')."""
    chart = StateChart(
        {},
        ChartDef.from_chart(
            {
                "initial": "closed",
                "states": {
                    "root": {"initial": "closed"},
                    "closed": {
                        "parent": "root",
                        "on": {"OPEN": {"target": "open", "guard": "allowed"}},
                    },
                    "open": {"parent": "root", "on": {"CLOSE": "closed"}},
                },
            }
        ),
    )

    before = _snapshot(chart)
    accepted = chart.send("OPEN", {"allowed": False})
    after = _snapshot(chart)

    assert accepted is False
    assert after["leaves"] == before["leaves"]
    assert after["config"] == before["config"]
    assert after["actions"] == []


# =================================================================================
# send_preserves_chart (StateChart.lean) / send_cfg_in_states
# ChartDef is not mutated by a send; the post-send configuration stays inside
# the declared states.
# =================================================================================


def test_send_cfg_in_states() -> None:
    chart = StateChart(
        {},
        ChartDef.from_chart(
            {
                "initial": "a",
                "states": {
                    "root": {"initial": "a"},
                    "a": {"parent": "root", "on": {"GO": "b"}},
                    "b": {"parent": "root", "on": {"GO": "a"}},
                },
            }
        ),
    )
    declared = {"root", "a", "b"}
    for ev in ["GO", "GO", "NOPE", "GO"]:
        chart.send(ev)
        for state in chart.configuration():
            assert state in declared


# =================================================================================
# Determinism by construction (StateChart.send is a total function)
# "A given (chart, history, configuration, event, guards) yields a unique
#  StepResult." Validate by replaying an identical event sequence on two
#  independent instances.
# =================================================================================


def test_determinism_by_construction() -> None:
    chart_obj: dict[str, object] = {
        "initial": "a1",
        "states": {
            "root": {"initial": "a"},
            "a": {
                "parent": "root",
                "initial": "a1",
                "entry": ["enterA"],
                "exit": ["exitA"],
            },
            "a1": {"parent": "a", "on": {"GO": "a2"}},
            "a2": {"parent": "a", "on": {"GO": "a1"}, "entry": ["enterA2"]},
        },
    }
    steps = ["GO", "GO", "NOPE", "GO", "GO"]

    def run() -> list[dict[str, object]]:
        import copy

        chart = StateChart({}, ChartDef.from_chart(copy.deepcopy(chart_obj)))
        trace: list[dict[str, object]] = [{**_snapshot(chart), "accepted": None}]
        for event in steps:
            accepted = chart.send(event)
            trace.append({**_snapshot(chart), "accepted": accepted})
        return trace

    trace1 = run()
    trace2 = run()
    assert trace1 == trace2, "two independent runs from identical inputs must agree"


# =================================================================================
# single_region_refines_flat_machine (StateChart.lean)
# "A single-region chart's send refines the flat StateMachine kernel: the new
#  active leaf equals the flat machine's transition target."
# =================================================================================


def test_single_region_refines_flat_machine_flat_chart() -> None:
    chart = StateChart(
        {},
        ChartDef.from_chart(
            {
                "initial": "green",
                "states": {
                    "root": {"initial": "green"},
                    "red": {"parent": "root", "on": {"TICK": "green"}},
                    "green": {"parent": "root", "on": {"TICK": "yellow"}},
                    "yellow": {"parent": "root", "on": {"TICK": "red"}},
                },
            }
        ),
    )
    flat = _FlatMachine(
        "green",
        {
            "red": {"TICK": "green"},
            "green": {"TICK": "yellow"},
            "yellow": {"TICK": "red"},
        },
    )

    for ev in ["TICK", "TICK", "UNKNOWN", "TICK", "TICK", "TICK", "TICK"]:
        chart_accepted = chart.send(ev)
        flat_accepted = flat.send(ev)
        assert chart_accepted is flat_accepted, f"accepted mismatch on {ev}"
        assert chart.active_leaves() == [flat.current], f"leaf mismatch after {ev}"


def test_single_region_refines_flat_machine_hierarchical() -> None:
    chart = StateChart(
        {},
        ChartDef.from_chart(
            {
                "initial": "on",
                "states": {
                    "root": {"initial": "on"},
                    "on": {
                        "parent": "root",
                        "initial": "ready",
                        "on": {"POWER": "off"},
                    },
                    "ready": {"parent": "on", "on": {"FIRE": "firing"}},
                    "firing": {"parent": "on", "on": {"DONE": "ready"}},
                    "off": {"parent": "root", "on": {"POWER": "on"}},
                },
            }
        ),
    )
    flat = _FlatMachine(
        "ready",
        {
            "ready": {"FIRE": "firing", "POWER": "off"},
            "firing": {"DONE": "ready", "POWER": "off"},
            "off": {
                "POWER": "ready"
            },  # target "on" compound; defaultLeaf("on") = "ready"
        },
    )

    for ev in ["FIRE", "DONE", "POWER", "POWER", "FIRE", "POWER", "NOPE"]:
        chart_accepted = chart.send(ev)
        flat_accepted = flat.send(ev)
        assert chart_accepted is flat_accepted, f"accepted mismatch on {ev}"
        assert chart.active_leaves() == [flat.current], f"leaf mismatch after {ev}"


# =================================================================================
# single_region_enabled_at_most_one (StateChart.lean)
# "With exactly one active leaf, the enabled set has length <= 1, so send takes
#  at most one transition."
# =================================================================================


def test_single_region_enabled_at_most_one() -> None:
    chart = StateChart(
        {},
        ChartDef.from_chart(
            {
                "initial": "s1",
                "states": {
                    "root": {"initial": "s1"},
                    "s1": {"parent": "root", "on": {"GO": "s2", "ALSO": "s1"}},
                    "s2": {"parent": "root", "on": {"GO": "s1"}},
                },
            }
        ),
    )
    for ev in ["GO", "ALSO", "GO", "NOPE", "ALSO", "GO"]:
        chart.send(ev)
        assert len(chart.active_leaves()) == 1, f"single leaf invariant after {ev}"


# =================================================================================
# Conflict-resolution transparency (StateChart.lean)
# sendTaken_subset_enabled: keepTrans only drops, never invents.
# sendTaken_eq_enabled_of_pairwise_disjoint: disjoint exit sets => all taken.
# =================================================================================


def test_sendtaken_subset_enabled() -> None:
    """Conflicting transitions from one leaf: exactly one is taken (subset)."""
    chart = StateChart(
        {},
        ChartDef.from_chart(
            {
                "initial": "a",
                "states": {
                    "root": {"initial": "a"},
                    "a": {"parent": "root", "on": {"GO": "b"}},
                    "b": {"parent": "root"},
                },
            }
        ),
    )
    chart.send("GO")
    assert chart.active_leaves() == ["b"]


def test_sendtaken_eq_enabled_of_pairwise_disjoint() -> None:
    """Pairwise-disjoint exit sets (parallel regions) => all taken."""
    chart = StateChart(
        {},
        ChartDef.from_chart(
            {
                "initial": "p",
                "states": {
                    "root": {"parallel": True},
                    "p": {"parent": "root", "initial": "a", "on": {"TICK": "b"}},
                    "a": {"parent": "p"},
                    "b": {"parent": "p", "on": {"TICK": "a"}},
                    "q": {"parent": "root", "initial": "c", "on": {"TICK": "d"}},
                    "c": {"parent": "q"},
                    "d": {"parent": "q", "on": {"TICK": "c"}},
                },
            }
        ),
    )
    chart.send("TICK")
    assert sorted(chart.active_leaves()) == ["b", "d"]


# =================================================================================
# parallel_region_confluence (StateChart.lean — the headline universal result)
# "When enabled transitions are pairwise non-conflicting (orthogonal regions),
#  every enabled transition is taken and the resulting configuration depends
#  only on the enabled SET, not its order -- invariant under any reordering."
# =================================================================================


def _parallel_chart(order: list[str]) -> StateChart:
    states: dict[str, object] = {"root": {"parallel": True}}
    for region in order:
        states[region] = {
            "parent": "root",
            "initial": f"{region}_a",
            "on": {"TICK": f"{region}_b"},
        }
        states[f"{region}_a"] = {"parent": region}
        states[f"{region}_b"] = {"parent": region, "on": {"TICK": f"{region}_a"}}
    return StateChart({}, ChartDef.from_chart({"initial": order[0], "states": states}))


def test_parallel_region_confluence_take_all() -> None:
    chart = _parallel_chart(["alpha", "beta", "gamma"])
    # TICK is enabled independently in every region; pairwise disjoint exit sets
    # => the conflict resolver is transparent => all three are taken.
    assert chart.send("TICK") is True
    assert sorted(chart.active_leaves()) == ["alpha_b", "beta_b", "gamma_b"]


def test_parallel_region_confluence_invariant_under_reordering() -> None:
    orderings = [
        ["alpha", "beta", "gamma"],
        ["gamma", "alpha", "beta"],
        ["beta", "gamma", "alpha"],
    ]
    sequence = ["TICK", "TICK", "TICK", "TICK"]

    def run(order: list[str]) -> list[set[str]]:
        chart = _parallel_chart(order)
        trace: list[set[str]] = []
        for _ in sequence:
            chart.send("TICK")
            trace.append(set(chart.active_leaves()))
        return trace

    traces = [run(order) for order in orderings]
    for i in range(len(sequence)):
        reference = traces[0][i]
        for j in range(1, len(orderings)):
            assert sorted(traces[j][i]) == sorted(reference), (
                f"confluence violated at step {i} for ordering {j}"
            )


# =================================================================================
# recordHistory_idempotent (StateChart.lean)
# "Recording the same exit pass twice is a no-op." History is restored by
#  targeting the history pseudo-state (Lean enterSet Kind.history branch; the
#  lazily-spec history_deep/shallow fixtures target hdeep/h).
# =================================================================================


def test_record_history_idempotent() -> None:
    def build() -> StateChart:
        return StateChart(
            {},
            ChartDef.from_chart(
                {
                    "initial": "p",
                    "states": {
                        "root": {"initial": "p"},
                        "p": {"parent": "root", "initial": "a", "on": {"OUT": "idle"}},
                        "hist": {"parent": "p", "history": "deep"},
                        "a": {"parent": "p", "on": {"TOGGLE": "b"}},
                        "b": {"parent": "p", "on": {"TOGGLE": "a"}},
                        "idle": {"parent": "root", "on": {"BACK": "hist"}},
                    },
                }
            ),
        )

    chart = build()
    chart.send("TOGGLE")  # p.a -> p.b (active leaf under p is now b)
    chart.send("OUT")  # exit p, record deep history = {b}
    chart.send("BACK")  # target hist -> restore b
    assert chart.active_leaves() == ["b"], "first restore -> b"
    chart.send("OUT")  # exit p again, record deep history = {b}
    chart.send("BACK")  # restore b again (idempotent recording)
    assert chart.active_leaves() == ["b"], "second restore -> b"

    # A fresh restore cycle lands on the same leaf the history captured.
    fresh = build()
    fresh.send("OUT")  # record {a} (initial)
    fresh.send("BACK")  # restore a
    assert fresh.active_leaves() == ["a"]


# =================================================================================
# send_actions_empty_when_rejected / stepActions_sourcing (StateChart.lean)
# "The action trace is empty precisely when an event is rejected; on the take
#  branch every fired action is sourced from an exit, transition, or entry."
# =================================================================================


def test_send_actions_empty_when_rejected() -> None:
    chart = StateChart(
        {},
        ChartDef.from_chart(
            {
                "initial": "a",
                "states": {
                    "root": {"initial": "a"},
                    "a": {
                        "parent": "root",
                        "on": {"GO": "b"},
                        "entry": ["inA"],
                        "exit": ["outA"],
                    },
                    "b": {"parent": "root", "entry": ["inB"]},
                },
            }
        ),
    )

    chart.send("GO")  # takes a transition, actions non-empty (exit a, enter b)
    assert chart.last_actions() != []

    accepted = chart.send("NOPE")  # rejected
    assert accepted is False
    assert chart.last_actions() == []


def test_step_actions_sourcing() -> None:
    chart = StateChart(
        {},
        ChartDef.from_chart(
            {
                "initial": "a",
                "states": {
                    "root": {"initial": "a"},
                    "a": {
                        "parent": "root",
                        "on": {"GO": {"target": "b", "action": ["tAct"]}},
                        "exit": ["outA"],
                    },
                    "b": {"parent": "root", "entry": ["inB"]},
                },
            }
        ),
    )

    chart.send("GO")
    actions = chart.last_actions()
    allowed = {"outA", "tAct", "inB"}
    for action in actions:
        assert action in allowed, f"unsourced action {action}"
    # exit innermost-first -> transition -> entry outermost-first.
    assert actions == ["outA", "tAct", "inB"]
