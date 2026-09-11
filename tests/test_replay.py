"""Replay-equivalence proof (``#lzpyreplayproof``).

The properties the module exists for:

* a rebuilt graph fed the same ordered log observes the same values, and any
  deviation fails at the **first** event where it appears rather than only at
  the end;
* a fingerprint is bound to the log bytes that produced it, so a stale
  fingerprint is deterministically suppressed (tsift's body-hash revalidation)
  instead of passing by coincidence or being misreported as a value divergence;
* the encoding a fingerprint hashes is canonical, so dict/set ordering never
  reports a divergence and an unhashable-by-value object never silently
  degrades to a `repr` that differs every run.

The last two tests apply the contract to the machinery it was written for:
``latest_durable_projection`` (a deterministic state machine over a log) and
``reliable_sync``'s retained outbox (the log itself).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import Any

import pytest

from lazily import (
    Delta,
    InMemoryOutbox,
    IpcMessage,
    LatestDurableProjectionCore,
    NonDeterminismError,
    ReplayDivergenceError,
    ReplayEncodingError,
    ReplayEvent,
    ReplayFingerprint,
    ReplayHarness,
    ReplayLog,
    ReplayLogMismatchError,
    ReplayProofError,
    ResyncCoordinator,
    canonical_bytes,
    canonical_digest,
    replay_log_from_outbox,
)
from lazily.replay import INITIAL_SEQ


# -- graphs under proof -------------------------------------------------------


class Counter:
    """A graph that is a pure function of its log."""

    def __init__(self) -> None:
        self.total = 0
        self.names: list[str] = []

    def apply(self, event: ReplayEvent) -> None:
        self.total += event.payload
        self.names.append(event.name)

    def observe(self) -> dict[str, Any]:
        return {"total": self.total, "names": tuple(self.names)}


class DriftingCounter(Counter):
    """A graph that is not: one event reads state from outside the log."""

    drift = 0

    def apply(self, event: ReplayEvent) -> None:
        super().apply(event)
        if event.seq == 1:
            self.total += DriftingCounter.drift


class ClockReader:
    """A graph that reaches for the wall clock, which replay cannot reproduce."""

    def __init__(self) -> None:
        self.at = 0.0

    def apply(self, event: ReplayEvent) -> None:
        self.at = time.time()

    def observe(self) -> dict[str, Any]:
        return {"at": self.at}


def _log() -> ReplayLog:
    return ReplayLog.from_records([("add", 1), ("add", 2), ("add", 3)])


# -- canonical encoding -------------------------------------------------------


def test_mapping_and_set_order_are_not_part_of_the_value() -> None:
    assert canonical_digest({"a": 1, "b": 2}) == canonical_digest({"b": 2, "a": 1})
    assert canonical_digest({1, 2, 3}) == canonical_digest({3, 1, 2})
    assert canonical_digest(frozenset("ab")) == canonical_digest(frozenset("ba"))


def test_sequence_order_is_part_of_the_value() -> None:
    assert canonical_digest([1, 2]) != canonical_digest([2, 1])


def test_type_tags_keep_lookalike_values_distinct() -> None:
    digests = {
        canonical_digest(1),
        canonical_digest("1"),
        canonical_digest(1.0),
        canonical_digest(True),
        canonical_digest(b"1"),
        canonical_digest([1]),
        canonical_digest({1}),
    }
    assert len(digests) == 7


def test_length_prefixes_keep_concatenations_distinct() -> None:
    # Without length framing both encode to the same member bytes.
    assert canonical_digest(["a", "bc"]) != canonical_digest(["ab", "c"])


def test_signed_zero_and_nan_are_exact() -> None:
    assert canonical_digest(0.0) != canonical_digest(-0.0)
    assert canonical_digest(float("nan")) == canonical_digest(float("nan"))
    assert canonical_digest(float("inf")) != canonical_digest(float("-inf"))


def test_enum_is_not_its_underlying_value() -> None:
    class Kind(StrEnum):
        APPLY = "apply"

    class Code(Enum):
        ONE = 1

    assert canonical_digest(Kind.APPLY) != canonical_digest("apply")
    assert canonical_digest(Code.ONE) != canonical_digest(1)


def test_dataclass_encodes_by_type_and_field_order() -> None:
    @dataclass(frozen=True)
    class Pair:
        left: int
        right: int

    @dataclass(frozen=True)
    class Other:
        left: int
        right: int

    assert canonical_digest(Pair(1, 2)) != canonical_digest(Pair(2, 1))
    assert canonical_digest(Pair(1, 2)) != canonical_digest(Other(1, 2))
    assert canonical_digest(Pair(1, 2)) != canonical_digest({"left": 1, "right": 2})


def test_nested_structures_encode() -> None:
    @dataclass(frozen=True)
    class Node:
        name: str
        children: tuple[Any, ...]

    value = {"root": Node("a", (Node("b", ()), {1: [None, True]}))}
    assert canonical_bytes(value) == canonical_bytes(value)


def test_an_unencodable_value_raises_instead_of_falling_back_to_repr() -> None:
    class Opaque:
        pass

    with pytest.raises(ReplayEncodingError) as excinfo:
        canonical_digest({"cell": [Opaque()]})

    # The path names where it was reached, and repr() is never the fallback:
    # an address-bearing repr would diverge on every replay.
    assert "value['cell'][0]" in str(excinfo.value)
    assert "Opaque" in str(excinfo.value)


# -- the log ------------------------------------------------------------------


def test_from_records_numbers_events_in_order() -> None:
    log = _log()

    assert [event.seq for event in log] == [0, 1, 2]
    assert [event.payload for event in log] == [1, 2, 3]
    assert len(log) == 3


def test_seq_must_strictly_increase() -> None:
    with pytest.raises(ValueError, match="strictly increasing"):
        ReplayLog.of(ReplayEvent(1, "a"), ReplayEvent(1, "b"))

    with pytest.raises(ValueError, match="strictly increasing"):
        ReplayLog.of(ReplayEvent(2, "a"), ReplayEvent(1, "b"))

    # Non-contiguous is fine — an ack-truncated outbox replays real epochs.
    assert len(ReplayLog.of(ReplayEvent(7, "a"), ReplayEvent(9, "b"))) == 2


def test_event_rejects_a_negative_seq_or_empty_name() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        ReplayEvent(-1, "a")
    with pytest.raises(ValueError, match="non-empty"):
        ReplayEvent(0, "")


def test_log_digest_covers_every_field_of_every_event() -> None:
    base = _log()

    assert base.digest == _log().digest
    assert base.digest != ReplayLog.from_records([("add", 1), ("add", 2)]).digest
    assert (
        base.digest
        != ReplayLog.from_records([("add", 1), ("add", 9), ("add", 3)]).digest
    )
    assert (
        base.digest
        != ReplayLog.from_records([("add", 1), ("sub", 2), ("add", 3)]).digest
    )


# -- record / verify ----------------------------------------------------------


def test_a_deterministic_graph_replays_to_its_fingerprint() -> None:
    log = _log()
    harness = ReplayHarness(Counter)

    fingerprint = harness.record(log)

    assert fingerprint.log_digest == log.digest
    assert fingerprint.stride == 1
    # The initial state plus one checkpoint per event.
    assert [checkpoint.seq for checkpoint in fingerprint.checkpoints] == [
        INITIAL_SEQ,
        0,
        1,
        2,
    ]
    assert harness.verify(log, fingerprint) == fingerprint
    assert harness.check(log, fingerprint) == ()


def test_each_replay_rebuilds_the_graph() -> None:
    builds = 0

    def build() -> Counter:
        nonlocal builds
        builds += 1
        return Counter()

    harness = ReplayHarness(build)
    harness.prove(_log())

    assert builds == 2


def test_divergence_names_the_first_diverging_event_and_cell() -> None:
    log = _log()
    harness = ReplayHarness(DriftingCounter)
    fingerprint = harness.record(log)

    DriftingCounter.drift = 100
    try:
        with pytest.raises(ReplayDivergenceError) as excinfo:
            harness.verify(log, fingerprint)
    finally:
        DriftingCounter.drift = 0

    first = excinfo.value.first
    # seq=1 is where the outside state entered, not seq=2 where it is still
    # visible — a final-state-only fingerprint could not say that.
    assert first.seq == 1
    assert first.label == "total"
    assert first.kind == "value"
    assert first.expected != first.actual
    assert first.preview == "103"
    assert "event seq=1" in str(first)
    assert "'total'" in str(excinfo.value)


def test_check_reports_divergence_without_raising() -> None:
    log = _log()
    harness = ReplayHarness(DriftingCounter)
    fingerprint = harness.record(log)

    DriftingCounter.drift = 5
    try:
        divergences = harness.check(log, fingerprint)
    finally:
        DriftingCounter.drift = 0

    assert [d.label for d in divergences] == ["total"]


def test_prove_catches_non_determinism_without_a_recorded_fingerprint() -> None:
    seen = iter([0, 7])

    class Unstable(Counter):
        def apply(self, event: ReplayEvent) -> None:
            super().apply(event)
            if event.seq == 0:
                self.total += next(seen)

    harness = ReplayHarness(Unstable)

    with pytest.raises(ReplayDivergenceError) as excinfo:
        harness.prove(_log())

    assert excinfo.value.first.seq == 0


def test_prove_needs_at_least_two_replays() -> None:
    with pytest.raises(ValueError, match="at least 2 replays"):
        ReplayHarness(Counter).prove(_log(), replays=1)


def test_a_cell_that_stops_being_observed_is_a_divergence() -> None:
    log = _log()
    fingerprint = ReplayHarness(Counter).record(log)

    class Narrowed(Counter):
        def observe(self) -> dict[str, Any]:
            return {"total": self.total}

    with pytest.raises(ReplayDivergenceError) as excinfo:
        ReplayHarness(Narrowed).verify(log, fingerprint)

    first = excinfo.value.first
    assert first.label == "names"
    assert first.kind == "missing"
    assert first.seq == INITIAL_SEQ
    assert "was not observed on replay" in str(first)


def test_a_newly_observed_cell_is_a_divergence() -> None:
    log = _log()

    class Widened(Counter):
        def observe(self) -> dict[str, Any]:
            return {**super().observe(), "extra": 1}

    fingerprint = ReplayHarness(Counter).record(log)

    with pytest.raises(ReplayDivergenceError) as excinfo:
        ReplayHarness(Widened).verify(log, fingerprint)

    assert excinfo.value.first.label == "extra"
    assert excinfo.value.first.kind == "unexpected"


def test_non_string_cell_labels_are_rejected() -> None:
    class BadLabels(Counter):
        def observe(self) -> dict[Any, Any]:
            return {1: "one"}

    with pytest.raises(TypeError, match="labels must be str"):
        ReplayHarness(BadLabels).record(_log())


# -- the log binding (tsift's revalidation rule) ------------------------------


def test_a_fingerprint_from_another_log_is_suppressed_not_compared() -> None:
    recorded_on = ReplayLog.from_records([("add", 1), ("add", 2), ("add", 3)])
    # Same final total, different log: a value comparison would pass.
    replayed_on = ReplayLog.from_records([("add", 3), ("add", 2), ("add", 1)])
    harness = ReplayHarness(Counter)
    fingerprint = harness.record(recorded_on)

    with pytest.raises(ReplayLogMismatchError) as excinfo:
        harness.verify(replayed_on, fingerprint)

    assert excinfo.value.expected_digest == recorded_on.digest
    assert excinfo.value.actual_digest == replayed_on.digest

    # `check` is non-raising for value divergence, but a stale fingerprint is
    # not a divergence report — it is an unanswerable question.
    with pytest.raises(ReplayLogMismatchError):
        harness.check(replayed_on, fingerprint)


def test_a_fingerprint_recorded_at_another_stride_is_refused() -> None:
    log = ReplayLog.from_records([("add", index) for index in range(4)])
    sparse = ReplayHarness(Counter, stride=2).record(log)

    assert [checkpoint.seq for checkpoint in sparse.checkpoints] == [INITIAL_SEQ, 1, 3]

    with pytest.raises(ReplayProofError, match="recorded at stride 2"):
        ReplayHarness(Counter).verify(log, sparse)

    assert ReplayHarness(Counter, stride=2).verify(log, sparse) == sparse


def test_stride_always_checkpoints_the_final_state() -> None:
    log = ReplayLog.from_records([("add", index) for index in range(5)])
    fingerprint = ReplayHarness(Counter, stride=3).record(log)

    assert [checkpoint.seq for checkpoint in fingerprint.checkpoints] == [
        INITIAL_SEQ,
        2,
        4,
    ]
    assert fingerprint.final.seq == 4


def test_stride_must_be_positive() -> None:
    with pytest.raises(ValueError, match="stride must be >= 1"):
        ReplayHarness(Counter, stride=0)


# -- deterministic mode -------------------------------------------------------


def test_deterministic_mode_raises_at_the_wall_clock_read() -> None:
    with pytest.raises(NonDeterminismError):
        ReplayHarness(ClockReader, deterministic=True).record(_log())

    # time.time is restored on the way out, so the guard is scoped.
    assert time.time() > 0


def test_without_deterministic_mode_the_clock_read_shows_up_as_divergence() -> None:
    harness = ReplayHarness(ClockReader)
    log = _log()
    fingerprint = harness.record(log)

    with pytest.raises(ReplayDivergenceError):
        harness.verify(log, fingerprint)


# -- pinning a fingerprint ----------------------------------------------------


def test_fingerprint_round_trips_through_wire_form() -> None:
    log = _log()
    harness = ReplayHarness(Counter)
    fingerprint = harness.record(log)

    wire = fingerprint.to_wire()
    restored = ReplayFingerprint.from_wire(wire)

    assert restored == fingerprint
    assert restored.digest == fingerprint.digest
    assert harness.verify(log, restored) == fingerprint


def test_wire_form_rejects_an_unknown_schema_version() -> None:
    wire = ReplayHarness(Counter).record(_log()).to_wire()
    wire["schema_version"] = 99

    with pytest.raises(ReplayProofError, match="schema_version"):
        ReplayFingerprint.from_wire(wire)


def test_a_fingerprint_needs_at_least_the_initial_checkpoint() -> None:
    with pytest.raises(ValueError, match="initial checkpoint"):
        ReplayFingerprint(log_digest="x", stride=1, checkpoints=())


# -- the machinery this contract was written for ------------------------------


class DurableProjectionGraph:
    """``LatestDurableProjectionCore`` driven entirely by an event log."""

    def __init__(self) -> None:
        self.core: LatestDurableProjectionCore[str, int] = LatestDurableProjectionCore(
            1
        )
        self.outcomes: list[Any] = []

    def apply(self, event: ReplayEvent) -> None:
        kind = event.name
        args = event.payload
        if kind == "upsert":
            _, outcome = self.core.upsert_desired(*args)
        elif kind == "claim":
            _, outcome = self.core.claim(*args)
        elif kind == "ack":
            _, outcome = self.core.ack_applied(*args)
        elif kind == "fail":
            _, outcome = self.core.fail_retryable(*args)
        elif kind == "reconnect":
            _, outcome = self.core.reconnect(*args)
        else:  # pragma: no cover - the log is written by this test
            raise AssertionError(kind)
        self.outcomes.append(outcome)

    def observe(self) -> dict[str, Any]:
        return {
            "snapshot": self.core.snapshot(),
            "pending": tuple(self.core.pending_keys()),
            "outcomes": tuple(self.outcomes),
        }


def _projection_log() -> ReplayLog:
    return ReplayLog.from_records(
        [
            ("upsert", ("a", 1, 10)),
            ("upsert", ("b", 1, 20)),
            ("claim", ("a", 1)),
            ("upsert", ("a", 2, 11)),
            ("fail", ("a", 1, 1)),
            ("claim", ("a", 1)),
            ("ack", ("a", 1, 2)),
            ("reconnect", (2,)),
            ("claim", ("b", 2)),
            ("ack", ("b", 2, 1)),
        ]
    )


def test_latest_durable_projection_is_replay_equivalent() -> None:
    log = _projection_log()
    harness = ReplayHarness(DurableProjectionGraph, deterministic=True)

    fingerprint = harness.prove(log)

    # Both keys ended durable, which is the trace this pins.
    final = fingerprint.final
    assert final.seq == log[-1].seq
    assert set(final.as_dict()) == {"snapshot", "pending", "outcomes"}

    # A fingerprint survives being committed and reloaded...
    reloaded = ReplayFingerprint.from_wire(fingerprint.to_wire())
    assert harness.verify(log, reloaded) == fingerprint

    # ...and is deterministically suppressed against an edited log, rather than
    # being compared and reported as a graph defect.
    edited = ReplayLog.of(
        *log.events[:3], ReplayEvent(3, "upsert", ("a", 3, 11)), *log.events[4:]
    )
    with pytest.raises(ReplayLogMismatchError):
        harness.verify(edited, reloaded)


def test_a_reordered_projection_log_is_a_different_log_and_a_different_state() -> None:
    log = _projection_log()
    swapped = ReplayLog.from_records(
        [(event.name, event.payload) for event in [log[1], log[0], *log.events[2:]]]
    )
    harness = ReplayHarness(DurableProjectionGraph)

    # Key-insertion order is observable, so the state genuinely differs...
    assert harness.record(log).final != harness.record(swapped).final
    # ...and the binding refuses the cross-comparison outright.
    with pytest.raises(ReplayLogMismatchError):
        harness.verify(swapped, harness.record(log))


class ResyncGraph:
    """A reliable-sync receiver rebuilt from the sender's retained frames."""

    def __init__(self) -> None:
        self.coordinator = ResyncCoordinator.with_epoch(2)
        self.actions: list[Any] = []

    def apply(self, event: ReplayEvent) -> None:
        self.actions.append(self.coordinator.ingest(event.payload))

    def observe(self) -> dict[str, Any]:
        return {
            "last_epoch": self.coordinator.last_epoch,
            "resyncing": self.coordinator.is_resyncing,
            "actions": tuple(action.kind for action in self.actions),
        }


def _outbox() -> InMemoryOutbox:
    outbox = InMemoryOutbox()
    for epoch in range(1, 5):
        outbox.append(epoch, IpcMessage.of_delta(Delta.next(epoch - 1, [])))
    return outbox


def test_a_reliable_sync_outbox_replay_is_a_fingerprinted_log() -> None:
    outbox = _outbox()
    outbox.ack_through(2)

    log = replay_log_from_outbox(outbox)

    # Outbox epochs, not 0..n-1: a truncated prefix stays visible.
    assert [event.seq for event in log] == [3, 4]

    harness = ReplayHarness(ResyncGraph, deterministic=True)
    fingerprint = harness.prove(log)

    assert fingerprint.final.seq == 4
    assert ResyncGraph().observe()["last_epoch"] == 2


def test_truncating_the_outbox_further_invalidates_the_fingerprint() -> None:
    outbox = _outbox()
    outbox.ack_through(2)
    harness = ReplayHarness(ResyncGraph)
    fingerprint = harness.record(replay_log_from_outbox(outbox))

    outbox.ack_through(3)

    with pytest.raises(ReplayLogMismatchError):
        harness.verify(replay_log_from_outbox(outbox), fingerprint)


def test_a_replay_cursor_selects_a_suffix() -> None:
    outbox = _outbox()

    assert [event.seq for event in replay_log_from_outbox(outbox)] == [1, 2, 3, 4]
    assert [event.seq for event in replay_log_from_outbox(outbox, cursor=2)] == [3, 4]
