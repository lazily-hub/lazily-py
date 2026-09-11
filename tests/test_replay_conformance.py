"""Replay the canonical replay-equivalence corpus against `lazily.replay`.

Three fixtures, one obligation each (`lazily-spec/docs/replay-equivalence.md`):
the fingerprint is bound to its log and that binding is revalidated before any
value compare; a divergence is reported at the first checkpoint where the values
parted; the observation encoding agrees with the family on which differences are
differences.

The corpus declares its subjects in prose because a JSON fixture cannot carry a
reactive graph, so `_Accumulator` below is this binding's copy of that
declaration — kept to the letter, including that `observe` exposes `sum` and
`names` under exactly those labels.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from conformance_assert import assert_key, corpus_path, instrument

from lazily import (
    ReplayDivergenceError,
    ReplayEncodingError,
    ReplayEvent,
    ReplayHarness,
    ReplayLog,
    ReplayLogMismatchError,
    ReplayStrideMismatchError,
    canonical_digest,
)


def _load(name: str) -> Any:
    path = corpus_path("replay", name)
    if not path.exists():
        pytest.skip(f"canonical replay fixture not found at {path}")
    return instrument(json.loads(path.read_text()), name=f"replay/{name}")


# -- the corpus's canonical subjects -----------------------------------------


class _Accumulator:
    """`accumulator`, and `drifting_accumulator` when a drift is configured."""

    def __init__(self, *, drift_at: int | None = None, drift: int = 0) -> None:
        self.sum = 0
        self.names: list[str] = []
        self._drift_at = drift_at
        self._drift = drift

    def apply(self, event: ReplayEvent) -> None:
        self.sum += event.payload
        self.names.append(event.name)
        if self._drift_at is not None and event.seq == self._drift_at:
            self.sum += self._drift

    def observe(self) -> dict[str, Any]:
        return {"sum": self.sum, "names": tuple(self.names)}


def _log(entries: list[dict[str, Any]]) -> ReplayLog:
    return ReplayLog(
        events=tuple(
            ReplayEvent(seq=entry["seq"], name=entry["name"], payload=entry["payload"])
            for entry in entries
        )
    )


def _build(config: Any, op: Any) -> Any:
    subject = config["subject"]
    if subject == "accumulator":
        return _Accumulator
    if subject == "drifting_accumulator":
        drift_at = config["drift_at"]
        drift = op.get("drift", 0)
        return lambda: _Accumulator(drift_at=drift_at, drift=drift)
    raise AssertionError(f"unknown canonical replay subject {subject!r}")


def _final_sum(build: Any, log: ReplayLog) -> int:
    subject = build()
    for event in log:
        subject.apply(event)
    return int(subject.observe()["sum"])


# -- obligations 1 and 2 ------------------------------------------------------


def _drive_harness_fixture(name: str, *, minimum_steps: int) -> None:
    fixture = _load(name)
    assert fixture["kind"] == "Replay"
    assert fixture["model"] == "ReplayHarness"
    config = fixture["config"]
    logs = {key: _log(value) for key, value in config["logs"].items()}
    fingerprints: dict[str, Any] = {}
    steps = fixture["steps"]
    assert len(steps) >= minimum_steps

    for index, step in enumerate(steps):
        op = step["op"]
        where = f"{name} step {index} ({op['type']})"
        expected = step.get("expected")

        if op["type"] == "log_digest_equal":
            actual = logs[op["left"]].digest == logs[op["right"]].digest
            assert step["returns"] == actual, f"{where}: returns"
            continue

        build = _build(config, op)
        stride = op.get("stride", config.get("stride", 1))
        harness = ReplayHarness(build, stride=stride)
        log = logs[op["log"]]

        if op["type"] == "record":
            fingerprint = harness.record(log)
            fingerprints[op["into"]] = fingerprint
            assert_key(expected, "outcome", "recorded", where)
            assert_key(
                expected,
                "checkpoint_seqs",
                [checkpoint.seq for checkpoint in fingerprint.checkpoints],
                where,
            )
            assert_key(expected, "stride", fingerprint.stride, where)
            final_sum = _final_sum(build, log)
            assert_key(expected, "final_sum", final_sum, where)
            # The fingerprint must have observed the value the subject ends on,
            # not merely some value: this is the one place the digest and the
            # declared state meet.
            assert fingerprint.final.as_dict()["sum"] == canonical_digest(final_sum), (
                f"{where}: the recorded `sum` digest is not the subject's final sum"
            )
            continue

        if op["type"] == "prove":
            harness.prove(log, replays=op["replays"])
            assert_key(expected, "outcome", "ok", where)
            assert_key(expected, "divergences", 0, where)
            continue

        fingerprint = fingerprints[op["fingerprint"]]

        if op["type"] == "verify":
            outcome, divergences, first = "ok", 0, None
            try:
                harness.verify(log, fingerprint)
            except ReplayLogMismatchError:
                outcome = "log_mismatch"
            except ReplayStrideMismatchError:
                outcome = "stride_mismatch"
            except ReplayDivergenceError as error:
                outcome, first = "divergent", error.first
            assert_key(expected, "outcome", outcome, where)
            if first is None:
                assert_key(expected, "divergences", divergences, where)
            else:
                assert_key(expected, "first_divergent_seq", first.seq, where)
                assert_key(expected, "first_divergent_label", first.label, where)
                assert_key(expected, "first_divergent_kind", first.kind, where)
            continue

        if op["type"] == "check":
            try:
                divergences = len(harness.check(log, fingerprint))
            except ReplayLogMismatchError:
                assert_key(expected, "outcome", "log_mismatch", where)
                assert_key(expected, "divergences", 0, where)
                continue
            assert_key(expected, "outcome", "ok", where)
            assert_key(expected, "divergences", divergences, where)
            continue

        raise AssertionError(f"unknown canonical replay operation {op['type']!r}")


def test_canonical_fingerprint_log_binding() -> None:
    _drive_harness_fixture("fingerprint_log_binding.json", minimum_steps=8)


def test_canonical_divergence_localization() -> None:
    _drive_harness_fixture("divergence_localization.json", minimum_steps=7)


# -- obligation 3 -------------------------------------------------------------


class _Opaque:
    """A value the encoding does not define, as the corpus's `opaque` tag."""


def _value(tagged: Any) -> Any:
    tag = tagged["t"]
    if tag == "int":
        return int(tagged["v"])
    if tag == "str":
        return tagged["v"]
    if tag == "float":
        return float(tagged["v"])
    if tag == "bool":
        return tagged["v"]
    if tag == "bytes":
        return bytes.fromhex(tagged["v"])
    if tag == "seq":
        return [_value(item) for item in tagged["v"]]
    if tag == "set":
        return frozenset(_value(item) for item in tagged["v"])
    if tag == "map":
        return {key: _value(item) for key, item in tagged["v"]}
    if tag == "opaque":
        return _Opaque()
    raise AssertionError(f"unknown canonical value tag {tag!r}")


def test_canonical_encoding_equality_classes() -> None:
    name = "canonical_encoding_equality.json"
    fixture = _load(name)
    assert fixture["kind"] == "Replay"
    assert fixture["model"] == "CanonicalEncoding"
    values = fixture["config"]["values"]
    steps = fixture["steps"]
    assert len(steps) >= 11
    outcomes: set[bool] = set()

    for index, step in enumerate(steps):
        op = step["op"]
        where = f"{name} step {index} ({op['type']})"

        if op["type"] == "digest_equal":
            actual = canonical_digest(_value(values[op["left"]])) == canonical_digest(
                _value(values[op["right"]])
            )
            assert step["returns"] == actual, f"{where}: returns"
            outcomes.add(actual)
            continue

        if op["type"] == "digest_defined":
            defined = True
            try:
                canonical_digest(_value(values[op["value"]]))
            except ReplayEncodingError:
                defined = False
            assert step["returns"] == defined, f"{where}: returns"
            assert_key(step["expected"], "outcome", "encoding_error", where)
            continue

        raise AssertionError(f"unknown canonical encoding operation {op['type']!r}")

    # Both outcomes really occurred: a runner that only ever saw `False` would
    # pass every inequality claim with a broken encoding.
    assert outcomes == {True, False}
