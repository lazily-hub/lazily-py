"""Replay-equivalence proof for a reactive graph (``#lzpyreplayproof``).

A durable-execution engine re-runs a workflow from an ordered event log and
expects the same decisions in the same order. :mod:`lazily.workflow` makes the
*reachable* non-determinism raise; this module is the other half of the gate —
it makes replay equivalence **provable** rather than assumed.

The discipline is taken from ``tsift``, whose cached excerpts are trustworthy
because every one records its span plus a body hash and *revalidates against the
source bytes* before it is returned: a stale body deterministically suppresses
the cached answer instead of returning a plausible-looking one. The same rule
holds here, with the event log as the source bytes:

* :class:`ReplayLog` is an ordered, strictly-increasing event sequence with a
  digest over its canonical bytes.
* :class:`ReplayFingerprint` records, per checkpoint, a digest of every observed
  cell value — **and the digest of the log that produced it**.
* :meth:`ReplayHarness.verify` revalidates that binding first. A fingerprint
  recorded against a different log raises :class:`ReplayLogMismatchError` and is
  never compared, so a stale fingerprint cannot pass by coincidence and is never
  misreported as a value divergence.
* A value that does differ raises :class:`ReplayDivergenceError` naming the
  first diverging event and the exact cell label, because a fingerprint that
  only covers the final state tells you the graph is wrong but not where.

The machinery this proves already exists: :mod:`lazily.reliable_sync` retains an
ordered, ack-truncated log (:meth:`~lazily.reliable_sync.DurableOutbox.replay_from`
is literally a replay source — see :func:`replay_log_from_outbox`) and
:mod:`lazily.latest_durable_projection` is a deterministic state machine over
one. What was missing is the stated contract, which is this:

    Given the same :class:`ReplayLog`, a rebuilt graph observes the same values
    at every checkpoint. Any deviation is a defect in the graph, not a
    tolerance — the harness fails, loudly, at the first event where it appears.

**Hashing.** BLAKE2b-256 from :mod:`hashlib`, not BLAKE3: lazily-py has no
runtime dependencies and BLAKE3 is not in the standard library. The property
being relied on is collision resistance over canonical bytes, which BLAKE2b has;
digests are not wire-compatible with tsift's and are not meant to be.

Example::

    class Counter:
        def __init__(self) -> None:
            self.ctx: dict = {}
            self.total = 0

        def apply(self, event: ReplayEvent) -> None:
            self.total += event.payload

        def observe(self) -> dict[str, object]:
            return {"total": self.total}


    log = ReplayLog.from_records([("add", 1), ("add", 2), ("add", 3)])
    harness = ReplayHarness(Counter)

    fingerprint = harness.record(log)  # pin it, or commit `to_wire()`
    harness.verify(log, fingerprint)  # raises if replay diverges
    harness.prove(log)  # record + re-replay in one call
"""

from __future__ import annotations


__all__ = [
    "ReplayCheckpoint",
    "ReplayDivergence",
    "ReplayDivergenceError",
    "ReplayEncodingError",
    "ReplayEvent",
    "ReplayFingerprint",
    "ReplayGraph",
    "ReplayHarness",
    "ReplayLog",
    "ReplayLogMismatchError",
    "ReplayProofError",
    "canonical_bytes",
    "canonical_digest",
    "replay_log_from_outbox",
]

import dataclasses
import hashlib
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from .workflow import deterministic_scope


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator


_DIGEST_SIZE = 32
_WIRE_SCHEMA_VERSION = 1
_PREVIEW_LIMIT = 120

#: The checkpoint sequence number for the state before any event was applied.
INITIAL_SEQ = -1


# -- errors -------------------------------------------------------------------


class ReplayProofError(RuntimeError):
    """A replay-equivalence proof could not be completed as stated."""


class ReplayEncodingError(ReplayProofError):
    """A value has no canonical byte encoding, so it cannot be fingerprinted.

    Raised instead of falling back to :func:`repr`, which embeds object
    addresses and would report a *false* divergence on every replay — the exact
    failure mode this module exists to make impossible.
    """


class ReplayLogMismatchError(ReplayProofError):
    """The fingerprint was recorded against a different event log.

    The tsift rule: revalidate the recorded hash against the source bytes and
    deterministically suppress the cached answer when they disagree. A stale
    fingerprint is never compared, so it can neither pass by coincidence nor be
    misreported as a value divergence.
    """

    def __init__(self, *, expected_digest: str, actual_digest: str) -> None:
        self.expected_digest = expected_digest
        self.actual_digest = actual_digest
        super().__init__(
            "fingerprint was recorded against a different event log "
            f"(fingerprint log_digest={expected_digest}, replayed log "
            f"digest={actual_digest}); re-record the fingerprint against this log"
        )


class ReplayDivergenceError(ReplayProofError):
    """A replayed graph observed a different value than the fingerprint."""

    def __init__(self, divergences: Sequence[ReplayDivergence]) -> None:
        if not divergences:  # pragma: no cover - guarded by the caller
            msg = "ReplayDivergenceError requires at least one divergence"
            raise ValueError(msg)
        self.divergences: tuple[ReplayDivergence, ...] = tuple(divergences)
        first = self.divergences[0]
        extra = len(self.divergences) - 1
        tail = f" (+{extra} more)" if extra else ""
        super().__init__(f"replay diverged from the fingerprint: {first}{tail}")

    @property
    def first(self) -> ReplayDivergence:
        """The earliest divergence, which is the one worth reading."""
        return self.divergences[0]


# -- canonical encoding -------------------------------------------------------


def _frame(tag: bytes, body: bytes) -> bytes:
    return b"%s%d:%s" % (tag, len(body), body)


def _canonical(value: Any, path: str) -> bytes:
    # Ordered by subtype: bool is an int, and StrEnum/IntEnum are str/int.
    if value is None:
        return b"n0:"
    if value is True:
        return b"b1:1"
    if value is False:
        return b"b1:0"
    if isinstance(value, Enum):
        return _frame(
            b"e",
            _frame(b"s", type(value).__qualname__.encode())
            + _canonical(value.value, f"{path}.value"),
        )
    if isinstance(value, int):
        return _frame(b"i", repr(int(value)).encode())
    if isinstance(value, float):
        # ``float.hex`` is exact and round-trips; ``repr`` is shortest-round-trip
        # and would fold distinct bit patterns of NaN together.
        return _frame(b"f", float(value).hex().encode())
    if isinstance(value, str):
        return _frame(b"s", value.encode())
    if isinstance(value, bytes | bytearray | memoryview):
        return _frame(b"y", bytes(value))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        body = _frame(b"s", type(value).__qualname__.encode())
        for f in dataclasses.fields(value):
            body += _frame(b"s", f.name.encode()) + _canonical(
                getattr(value, f.name), f"{path}.{f.name}"
            )
        return _frame(b"d", body)
    if isinstance(value, Mapping):
        entries = [
            _canonical(key, f"{path}[key]") + _canonical(item, f"{path}[{key!r}]")
            for key, item in value.items()
        ]
        # Sort by encoded bytes: insertion order is not part of the value, and
        # mixed-type keys are not mutually comparable.
        return _frame(b"m", b"".join(sorted(entries)))
    if isinstance(value, Set):
        return _frame(
            b"t",
            b"".join(sorted(_canonical(item, f"{path}{{}}") for item in value)),
        )
    if isinstance(value, Sequence):
        return _frame(
            b"l",
            b"".join(
                _canonical(item, f"{path}[{index}]") for index, item in enumerate(value)
            ),
        )
    msg = (
        f"{path}: {type(value).__module__}.{type(value).__qualname__} has no "
        "canonical encoding; observe a plain value, a dataclass, or a "
        "mapping/sequence/set of them instead"
    )
    raise ReplayEncodingError(msg)


def canonical_bytes(value: Any) -> bytes:
    """Encode ``value`` to type-tagged, order-stable bytes.

    Mapping and set members are ordered by their own encoded bytes, so dict
    insertion order and set iteration order do not change the result. Every
    frame is length-prefixed and type-tagged, so ``"1"``, ``1``, ``1.0`` and
    ``True`` encode differently and no concatenation of members can be confused
    for another. Anything without a defined encoding raises
    :class:`ReplayEncodingError` rather than degrading to :func:`repr`.
    """
    return _canonical(value, "value")


def canonical_digest(value: Any) -> str:
    """The BLAKE2b-256 hex digest of :func:`canonical_bytes` of ``value``."""
    return hashlib.blake2b(canonical_bytes(value), digest_size=_DIGEST_SIZE).hexdigest()


def _preview(value: Any) -> str:
    try:
        text = repr(value)
    except Exception:  # pragma: no cover - a hostile __repr__
        return f"<unreprable {type(value).__qualname__}>"
    if len(text) > _PREVIEW_LIMIT:
        return text[: _PREVIEW_LIMIT - 1] + "…"
    return text


# -- the log ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReplayEvent:
    """One entry of an ordered event log."""

    seq: int
    name: str
    payload: Any = None

    def __post_init__(self) -> None:
        if self.seq < 0:
            msg = f"event seq must be non-negative, got {self.seq}"
            raise ValueError(msg)
        if not self.name:
            msg = "event name must be non-empty"
            raise ValueError(msg)


@dataclass(frozen=True)
class ReplayLog:
    """An ordered event log with a digest over its canonical bytes.

    Sequence numbers must strictly increase; they do not have to be contiguous,
    because an ack-truncated durable outbox replays real epochs.
    """

    events: tuple[ReplayEvent, ...]
    digest: str = field(init=False, default="", compare=False, repr=False)

    def __post_init__(self) -> None:
        previous: int | None = None
        for event in self.events:
            if previous is not None and event.seq <= previous:
                msg = (
                    "event log must be strictly increasing in seq, got "
                    f"{event.seq} after {previous}"
                )
                raise ValueError(msg)
            previous = event.seq
        object.__setattr__(self, "digest", canonical_digest(self.events))

    @classmethod
    def of(cls, *events: ReplayEvent) -> ReplayLog:
        """A log from already-numbered events."""
        return cls(events=tuple(events))

    @classmethod
    def from_records(cls, records: Iterable[tuple[str, Any]]) -> ReplayLog:
        """A log from ``(name, payload)`` pairs, numbered ``0..n-1``."""
        return cls(
            events=tuple(
                ReplayEvent(seq=index, name=name, payload=payload)
                for index, (name, payload) in enumerate(records)
            )
        )

    def __len__(self) -> int:
        return len(self.events)

    def __iter__(self) -> Iterator[ReplayEvent]:
        return iter(self.events)

    def __getitem__(self, index: int) -> ReplayEvent:
        return self.events[index]


@runtime_checkable
class ReplayOutbox(Protocol):
    """The one :class:`~lazily.reliable_sync.DurableOutbox` operation needed."""

    def replay_from(self, cursor: int) -> list[tuple[int, Any]]: ...


def replay_log_from_outbox(
    outbox: ReplayOutbox, *, cursor: int = 0, name: str = "frame"
) -> ReplayLog:
    """A :class:`ReplayLog` from a reliable-sync outbox's retained frames.

    :meth:`~lazily.reliable_sync.DurableOutbox.replay_from` is already the
    replay source a reconnect drains; this makes it the fingerprinted one too.
    Outbox epochs become event seqs, so a truncated prefix is visible in the log
    digest rather than silently shifting every event.
    """
    return ReplayLog(
        events=tuple(
            ReplayEvent(seq=epoch, name=name, payload=message)
            for epoch, message in outbox.replay_from(cursor)
        )
    )


# -- the fingerprint ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReplayCheckpoint:
    """Per-cell digests observed after applying events through ``seq``.

    ``seq`` is :data:`INITIAL_SEQ` for the state before any event was applied.
    """

    seq: int
    cells: tuple[tuple[str, str], ...]

    @classmethod
    def of(cls, seq: int, observed: Mapping[str, Any]) -> ReplayCheckpoint:
        for label in observed:
            if not isinstance(label, str):
                msg = f"observed cell labels must be str, got {type(label)!r}"
                raise TypeError(msg)
        return cls(
            seq=seq,
            cells=tuple(
                sorted(
                    (label, canonical_digest(value))
                    for label, value in observed.items()
                )
            ),
        )

    def as_dict(self) -> dict[str, str]:
        """The label → digest mapping."""
        return dict(self.cells)


@dataclass(frozen=True)
class ReplayFingerprint:
    """A recorded, log-bound observation of a replayed graph."""

    log_digest: str
    stride: int
    checkpoints: tuple[ReplayCheckpoint, ...]
    digest: str = field(init=False, default="", compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.stride < 1:
            msg = f"stride must be >= 1, got {self.stride}"
            raise ValueError(msg)
        if not self.checkpoints:
            msg = "a fingerprint needs at least the initial checkpoint"
            raise ValueError(msg)
        object.__setattr__(
            self,
            "digest",
            canonical_digest((self.log_digest, self.stride, self.checkpoints)),
        )

    @property
    def final(self) -> ReplayCheckpoint:
        """The last checkpoint — the end state of the replay."""
        return self.checkpoints[-1]

    def to_wire(self) -> dict[str, Any]:
        """A JSON-safe form, so a fingerprint can be committed next to a test."""
        return {
            "schema_version": _WIRE_SCHEMA_VERSION,
            "log_digest": self.log_digest,
            "stride": self.stride,
            "checkpoints": [
                {"seq": checkpoint.seq, "cells": checkpoint.as_dict()}
                for checkpoint in self.checkpoints
            ],
        }

    @classmethod
    def from_wire(cls, wire: Mapping[str, Any]) -> ReplayFingerprint:
        """Rebuild from :meth:`to_wire`, rejecting an unknown schema version."""
        version = wire.get("schema_version")
        if version != _WIRE_SCHEMA_VERSION:
            msg = (
                f"unsupported replay fingerprint schema_version {version!r}, "
                f"expected {_WIRE_SCHEMA_VERSION}"
            )
            raise ReplayProofError(msg)
        return cls(
            log_digest=str(wire["log_digest"]),
            stride=int(wire["stride"]),
            checkpoints=tuple(
                ReplayCheckpoint(
                    seq=int(checkpoint["seq"]),
                    cells=tuple(
                        sorted((str(k), str(v)) for k, v in checkpoint["cells"].items())
                    ),
                )
                for checkpoint in wire["checkpoints"]
            ),
        )


@dataclass(frozen=True, slots=True)
class ReplayDivergence:
    """One cell that did not replay to its recorded digest."""

    seq: int
    label: str
    kind: str
    expected: str | None
    actual: str | None
    preview: str | None = None

    def __str__(self) -> str:
        where = "initial state" if self.seq == INITIAL_SEQ else f"event seq={self.seq}"
        if self.kind == "missing":
            return f"{where}: cell {self.label!r} was not observed on replay"
        if self.kind == "unexpected":
            return f"{where}: cell {self.label!r} appeared on replay but is not in the fingerprint"
        preview = f", observed {self.preview}" if self.preview else ""
        return (
            f"{where}: cell {self.label!r} expected {self.expected} "
            f"but replayed {self.actual}{preview}"
        )


# -- the graph under proof ----------------------------------------------------


@runtime_checkable
class ReplayGraph(Protocol):
    """What the harness needs from the graph it rebuilds.

    ``apply`` advances the graph by exactly one event; ``observe`` returns the
    cell values the fingerprint covers, keyed by a stable label.
    """

    def apply(self, event: ReplayEvent) -> None: ...

    def observe(self) -> Mapping[str, Any]: ...


class ReplayHarness:
    """Rebuild a graph from an event log and prove it replays identically.

    ``build`` is called once per replay and must return a *fresh* graph — a
    harness that reuses one instance proves nothing, since the state it would
    compare against is the state it already has.

    ``stride`` checkpoints every ``stride``-th event (the initial state and the
    final state are always checkpointed). It is recorded in the fingerprint, so
    a fingerprint cannot be compared against a replay that sampled differently.

    ``deterministic=True`` wraps every replay in
    :func:`~lazily.workflow.deterministic_scope`, so a wall-clock or randomness
    read raises :class:`~lazily.workflow.NonDeterminismError` at the line that
    reached it instead of showing up as a divergence one checkpoint later. Read
    that function's docstring for what it does and does not intercept — it is a
    guard, not a sandbox.
    """

    __slots__ = ("_build", "_deterministic", "_stride")

    def __init__(
        self,
        build: Callable[[], ReplayGraph],
        *,
        stride: int = 1,
        deterministic: bool = False,
    ) -> None:
        if stride < 1:
            msg = f"stride must be >= 1, got {stride}"
            raise ValueError(msg)
        self._build = build
        self._stride = stride
        self._deterministic = deterministic

    @property
    def stride(self) -> int:
        return self._stride

    def record(self, log: ReplayLog) -> ReplayFingerprint:
        """Replay ``log`` once and record what the graph observed."""
        return self._replay(log)[0]

    def check(
        self, log: ReplayLog, fingerprint: ReplayFingerprint
    ) -> tuple[ReplayDivergence, ...]:
        """Replay ``log`` and return the divergences from ``fingerprint``.

        Non-raising for value divergence, so a caller can report all of them.
        Still raises :class:`ReplayLogMismatchError` for a fingerprint recorded
        against a different log, and :class:`ReplayProofError` for one recorded
        at a different stride: comparing either would answer a question nobody
        asked.
        """
        replayed, observed = self._replay(log)
        self._revalidate(fingerprint, replayed)
        return self._compare(fingerprint, replayed, observed)

    def verify(
        self, log: ReplayLog, fingerprint: ReplayFingerprint
    ) -> ReplayFingerprint:
        """Replay ``log`` and raise unless it matches ``fingerprint`` exactly.

        Returns the freshly recorded fingerprint, which equals ``fingerprint``.
        """
        replayed, observed = self._replay(log)
        self._revalidate(fingerprint, replayed)
        divergences = self._compare(fingerprint, replayed, observed)
        if divergences:
            raise ReplayDivergenceError(divergences)
        return replayed

    def prove(self, log: ReplayLog, *, replays: int = 2) -> ReplayFingerprint:
        """Record ``log`` and re-replay it, raising on any divergence.

        This is the self-check: no external fingerprint is needed to catch a
        graph that is not a pure function of its log, because two replays of the
        same log in the same process already disagree.
        """
        if replays < 2:
            msg = f"prove needs at least 2 replays to compare, got {replays}"
            raise ValueError(msg)
        fingerprint = self.record(log)
        for _ in range(replays - 1):
            self.verify(log, fingerprint)
        return fingerprint

    # -- internals ------------------------------------------------------------

    @staticmethod
    def _revalidate(
        fingerprint: ReplayFingerprint, replayed: ReplayFingerprint
    ) -> None:
        """Bind the fingerprint to these exact log bytes before comparing."""
        if fingerprint.log_digest != replayed.log_digest:
            raise ReplayLogMismatchError(
                expected_digest=fingerprint.log_digest,
                actual_digest=replayed.log_digest,
            )
        if fingerprint.stride != replayed.stride:
            msg = (
                f"fingerprint was recorded at stride {fingerprint.stride} but this "
                f"harness samples at stride {replayed.stride}; re-record it"
            )
            raise ReplayProofError(msg)

    def _replay(
        self, log: ReplayLog
    ) -> tuple[ReplayFingerprint, list[Mapping[str, Any]]]:
        if self._deterministic:
            with deterministic_scope():
                checkpoints, observed = self._drive(log)
        else:
            checkpoints, observed = self._drive(log)
        fingerprint = ReplayFingerprint(
            log_digest=log.digest, stride=self._stride, checkpoints=tuple(checkpoints)
        )
        return fingerprint, observed

    def _drive(
        self, log: ReplayLog
    ) -> tuple[list[ReplayCheckpoint], list[Mapping[str, Any]]]:
        graph = self._build()
        sample = dict(graph.observe())
        checkpoints = [ReplayCheckpoint.of(INITIAL_SEQ, sample)]
        observed = [sample]
        total = len(log)
        for index, event in enumerate(log):
            graph.apply(event)
            if (index + 1) % self._stride == 0 or index + 1 == total:
                sample = dict(graph.observe())
                checkpoints.append(ReplayCheckpoint.of(event.seq, sample))
                observed.append(sample)
        return checkpoints, observed

    @staticmethod
    def _compare(
        expected: ReplayFingerprint,
        actual: ReplayFingerprint,
        observed: Sequence[Mapping[str, Any]],
    ) -> tuple[ReplayDivergence, ...]:
        divergences: list[ReplayDivergence] = []
        for index, (want, got) in enumerate(
            zip(expected.checkpoints, actual.checkpoints, strict=False)
        ):
            want_cells = want.as_dict()
            got_cells = got.as_dict()
            for label in sorted(want_cells.keys() | got_cells.keys()):
                want_digest = want_cells.get(label)
                got_digest = got_cells.get(label)
                if want_digest == got_digest:
                    continue
                if got_digest is None:
                    kind = "missing"
                elif want_digest is None:
                    kind = "unexpected"
                else:
                    kind = "value"
                sampled = observed[index] if index < len(observed) else {}
                divergences.append(
                    ReplayDivergence(
                        seq=want.seq,
                        label=label,
                        kind=kind,
                        expected=want_digest,
                        actual=got_digest,
                        preview=(
                            _preview(sampled[label]) if label in sampled else None
                        ),
                    )
                )
            if divergences:
                # The first diverging checkpoint is the actionable one; later
                # ones are almost always the same defect carried forward.
                break
        if len(expected.checkpoints) != len(actual.checkpoints) and not divergences:
            # Same log digest and stride, so this cannot come from sampling —
            # it means ``observe`` or ``apply`` changed the checkpoint count.
            msg = (
                f"fingerprint has {len(expected.checkpoints)} checkpoints but the "
                f"replay produced {len(actual.checkpoints)} for the same log"
            )
            raise ReplayProofError(msg)
        return tuple(divergences)
