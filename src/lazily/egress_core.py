"""Transport-agnostic reactive egress core (``#lzegress``).

The send-side mirror of :mod:`lazily.ingress_core`. :class:`EgressCore` owns
delivery authority and nothing else:

* monotone sequence assignment;
* the pending queue and the bounded unacknowledged window;
* the monotone cumulative acknowledgement watermark;
* retry attempts, bounded exponential backoff, and exhaustion; and
* the producer-generation fence.

It owns no graph handles and performs no I/O. Every transition returns the
:class:`EgressChange` naming exactly which reader kinds it invalidated, so the
reactive shells (:mod:`lazily.egress`) clear precisely that set — a no-op or a
stale-generation transition invalidates nothing.

``RelayCell`` / ``Outbox`` stay the conflation/backpressure layer ahead of
:meth:`EgressCore.enqueue`, and ``DurableOutbox`` / ``SpillStore`` stay the
persistence layer; egress does not duplicate them.

Spec: ``lazily-spec/docs/transport-egress.md``. Reference: ``lazily-rs``
``src/egress_core.rs``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


__all__ = [
    "EgressAck",
    "EgressAckKind",
    "EgressChange",
    "EgressClaim",
    "EgressClaimKind",
    "EgressConfigError",
    "EgressCore",
    "EgressEnvelope",
    "EgressFailure",
    "EgressFailureKind",
    "EgressPolicy",
    "EgressReconnect",
    "EgressReconnectKind",
    "EgressRetry",
    "EgressRetryAction",
    "EgressRetryActionKind",
]


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    """Egress bounds. ``retry_budget`` counts retries AFTER the first attempt."""

    inflight_limit: int = 64
    retry_budget: int = 3
    retry_base: int = 10
    retry_ceiling: int = 1_000


class EgressConfigError(ValueError):
    """A policy the core refuses to run under."""

    ZERO_INFLIGHT_LIMIT = "inflight_limit must be at least 1"
    ZERO_RETRY_BASE = "retry_base must be at least 1"
    RETRY_CEILING_BELOW_BASE = "retry_ceiling must be at least retry_base"


@dataclass(frozen=True, slots=True)
class EgressEnvelope[T]:
    """One send attempt. ``(generation, sequence)`` is the transport idempotency
    identity."""

    generation: int
    sequence: int
    attempt: int
    payload: T


@dataclass(frozen=True, slots=True)
class EgressRetry:
    """Retry evidence for the most recent failed send."""

    sequence: int
    attempt: int
    backoff: int
    exhausted: bool


class EgressClaimKind(StrEnum):
    CLAIMED = "claimed"
    EMPTY = "empty"
    WINDOW_FULL = "window_full"
    STALE_GENERATION = "stale_generation"


@dataclass(frozen=True, slots=True)
class EgressClaim[T]:
    kind: EgressClaimKind
    envelope: EgressEnvelope[T] | None = None
    current: int | None = None


class EgressAckKind(StrEnum):
    ADVANCED = "advanced"
    UNCHANGED = "unchanged"
    BEYOND_SENT = "beyond_sent"
    STALE_GENERATION = "stale_generation"


@dataclass(frozen=True, slots=True)
class EgressAck:
    kind: EgressAckKind
    through: int | None = None
    current: int | None = None
    next_sequence: int | None = None


class EgressFailureKind(StrEnum):
    RETRYING = "retrying"
    EXHAUSTED = "exhausted"
    UNKNOWN_SEQUENCE = "unknown_sequence"
    STALE_GENERATION = "stale_generation"


@dataclass(frozen=True, slots=True)
class EgressFailure:
    kind: EgressFailureKind
    retry: EgressRetry | None = None
    current: int | None = None


class EgressRetryActionKind(StrEnum):
    SCHEDULED = "scheduled"
    UNKNOWN_SEQUENCE = "unknown_sequence"
    STALE_GENERATION = "stale_generation"


@dataclass(frozen=True, slots=True)
class EgressRetryAction:
    kind: EgressRetryActionKind
    sequence: int | None = None
    current: int | None = None


class EgressReconnectKind(StrEnum):
    ADVANCED = "advanced"
    UNCHANGED = "unchanged"
    STALE_GENERATION = "stale_generation"


@dataclass(frozen=True, slots=True)
class EgressReconnect:
    kind: EgressReconnectKind
    generation: int | None = None
    replayed: int | None = None
    current: int | None = None


@dataclass(frozen=True, slots=True)
class EgressChange:
    """Reader-kind invalidations emitted by one transition."""

    pending: bool = False
    inflight: bool = False
    acked_through: bool = False
    retry: bool = False

    def is_empty(self) -> bool:
        return not (self.pending or self.inflight or self.acked_through or self.retry)


_NO_CHANGE = EgressChange()


@dataclass(slots=True)
class _Record[T]:
    generation: int
    sequence: int
    attempt: int
    payload: T

    def envelope(self) -> EgressEnvelope[T]:
        return EgressEnvelope(
            self.generation, self.sequence, self.attempt, self.payload
        )


class EgressCore[T]:
    """Pure egress delivery authority."""

    __slots__ = (
        "_acked_through",
        "_failed",
        "_generation",
        "_inflight",
        "_next_sequence",
        "_pending",
        "_policy",
        "_retry",
    )

    def __init__(self, generation: int, policy: EgressPolicy | None = None) -> None:
        policy = policy if policy is not None else EgressPolicy()
        if policy.inflight_limit <= 0:
            raise EgressConfigError(EgressConfigError.ZERO_INFLIGHT_LIMIT)
        if policy.retry_base <= 0:
            raise EgressConfigError(EgressConfigError.ZERO_RETRY_BASE)
        if policy.retry_ceiling < policy.retry_base:
            raise EgressConfigError(EgressConfigError.RETRY_CEILING_BELOW_BASE)
        self._policy = policy
        self._generation = generation
        self._next_sequence = 0
        self._acked_through: int | None = None
        # Oldest sequence first; re-inserted records keep sequence order.
        self._pending: list[_Record[T]] = []
        # sequence -> record. Kept ordered by sequence on read.
        self._inflight: dict[int, _Record[T]] = {}
        # Failed records parked until `retry_now` (never re-sent reactively).
        self._failed: dict[int, _Record[T]] = {}
        self._retry: EgressRetry | None = None

    # -- Projections ---------------------------------------------------------

    @property
    def policy(self) -> EgressPolicy:
        return self._policy

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def next_sequence(self) -> int:
        return self._next_sequence

    def pending(self) -> list[EgressEnvelope[T]]:
        return [record.envelope() for record in self._pending]

    def inflight(self) -> list[EgressEnvelope[T]]:
        return [self._inflight[seq].envelope() for seq in sorted(self._inflight)]

    def acked_through(self) -> int | None:
        return self._acked_through

    def retry(self) -> EgressRetry | None:
        return self._retry

    # -- Transitions ---------------------------------------------------------

    def enqueue(self, payload: T) -> tuple[EgressChange, int]:
        """Assign the next sequence and place ``payload`` in the pending projection."""
        sequence = self._next_sequence
        self._next_sequence += 1
        self._pending.append(_Record(self._generation, sequence, 0, payload))
        return EgressChange(pending=True), sequence

    def claim(self, generation: int) -> tuple[EgressChange, EgressClaim[T]]:
        """Move the oldest pending record into the unacknowledged window.

        ``generation`` is the one the caller's transport attachment captured; an
        attachment from a superseded incarnation cannot claim.
        """
        if generation != self._generation:
            return _NO_CHANGE, EgressClaim(
                EgressClaimKind.STALE_GENERATION, current=self._generation
            )
        if len(self._inflight) >= self._policy.inflight_limit:
            return _NO_CHANGE, EgressClaim(EgressClaimKind.WINDOW_FULL)
        if not self._pending:
            return _NO_CHANGE, EgressClaim(EgressClaimKind.EMPTY)
        record = self._pending.pop(0)
        record.generation = self._generation
        record.attempt += 1
        envelope = record.envelope()
        self._inflight[record.sequence] = record
        retry_changed = (
            self._retry is not None and self._retry.sequence == record.sequence
        )
        if retry_changed:
            self._retry = None
        return (
            EgressChange(pending=True, inflight=True, retry=retry_changed),
            EgressClaim(EgressClaimKind.CLAIMED, envelope=envelope),
        )

    def ack(self, generation: int, through: int) -> tuple[EgressChange, EgressAck]:
        """Advance the cumulative watermark and prune acknowledged records.

        An ACK at or past ``next_sequence`` is refused (``BEYOND_SENT``) and
        changes nothing.
        """
        if generation != self._generation:
            return _NO_CHANGE, EgressAck(
                EgressAckKind.STALE_GENERATION, current=self._generation
            )
        if through >= self._next_sequence:
            # Refused: nothing at or past `next_sequence` was ever assigned, so
            # acknowledging it would advance the watermark over sequences that
            # do not exist yet and silently drop them when they are enqueued.
            return _NO_CHANGE, EgressAck(
                EgressAckKind.BEYOND_SENT, next_sequence=self._next_sequence
            )
        if self._acked_through is not None and through <= self._acked_through:
            return _NO_CHANGE, EgressAck(
                EgressAckKind.UNCHANGED, through=self._acked_through
            )
        inflight_before = len(self._inflight)
        pending_before = len(self._pending)
        self._acked_through = through
        self._inflight = {s: r for s, r in self._inflight.items() if s > through}
        self._failed = {s: r for s, r in self._failed.items() if s > through}
        self._pending = [r for r in self._pending if r.sequence > through]
        retry_changed = self._retry is not None and self._retry.sequence <= through
        if retry_changed:
            self._retry = None
        return (
            EgressChange(
                pending=len(self._pending) != pending_before,
                inflight=len(self._inflight) != inflight_before,
                acked_through=True,
                retry=retry_changed,
            ),
            EgressAck(EgressAckKind.ADVANCED, through=through),
        )

    def fail(
        self, generation: int, sequence: int
    ) -> tuple[EgressChange, EgressFailure]:
        """Park a failed in-flight record while budget remains; exhaust it otherwise."""
        if generation != self._generation:
            return _NO_CHANGE, EgressFailure(
                EgressFailureKind.STALE_GENERATION, current=self._generation
            )
        record = self._inflight.pop(sequence, None)
        if record is None:
            return _NO_CHANGE, EgressFailure(EgressFailureKind.UNKNOWN_SEQUENCE)
        exhausted = record.attempt > self._policy.retry_budget
        retry = EgressRetry(
            sequence=sequence,
            attempt=record.attempt,
            backoff=self.backoff(record.attempt),
            exhausted=exhausted,
        )
        self._retry = retry
        if not exhausted:
            self._failed[sequence] = record
        return (
            EgressChange(inflight=True, retry=True),
            EgressFailure(
                EgressFailureKind.EXHAUSTED
                if exhausted
                else EgressFailureKind.RETRYING,
                retry=retry,
            ),
        )

    def retry_now(
        self, generation: int, sequence: int
    ) -> tuple[EgressChange, EgressRetryAction]:
        """Return a parked failed record to pending once its backoff has elapsed.

        Parking is explicit so a failed send cannot spin the attachment Effect in
        an immediate reactive retry loop; an exhausted record is terminal.
        """
        if generation != self._generation:
            return _NO_CHANGE, EgressRetryAction(
                EgressRetryActionKind.STALE_GENERATION, current=self._generation
            )
        record = self._failed.pop(sequence, None)
        if record is None:
            return _NO_CHANGE, EgressRetryAction(EgressRetryActionKind.UNKNOWN_SEQUENCE)
        self._insert_pending_ordered(record)
        retry_changed = self._retry is not None and self._retry.sequence == sequence
        if retry_changed:
            self._retry = None
        return (
            EgressChange(pending=True, retry=retry_changed),
            EgressRetryAction(EgressRetryActionKind.SCHEDULED, sequence=sequence),
        )

    def reconnect(self, generation: int) -> tuple[EgressChange, EgressReconnect]:
        """Fence the previous attachment and replay every unacknowledged record.

        ``generation`` must strictly advance. ``next_sequence`` and the ACK
        watermark survive; in-flight and parked records move back to pending in
        sequence order, and every pending envelope is rewritten to the new
        incarnation.
        """
        if generation < self._generation:
            return _NO_CHANGE, EgressReconnect(
                EgressReconnectKind.STALE_GENERATION, current=self._generation
            )
        if generation == self._generation:
            return _NO_CHANGE, EgressReconnect(
                EgressReconnectKind.UNCHANGED, generation=self._generation
            )
        self._generation = generation
        replayed = len(self._inflight) + len(self._failed)
        for source in (self._inflight, self._failed):
            for sequence in sorted(source):
                record = source[sequence]
                record.generation = generation
                self._insert_pending_ordered(record)
        self._inflight = {}
        self._failed = {}
        for record in self._pending:
            record.generation = generation
        retry_changed = self._retry is not None
        self._retry = None
        # Every pending envelope was rewritten to the new generation, so the
        # `pending` projection changed whenever anything is pending — not only
        # when something was replayed into it.
        pending_changed = replayed > 0 or bool(self._pending)
        return (
            EgressChange(
                pending=pending_changed, inflight=replayed > 0, retry=retry_changed
            ),
            EgressReconnect(
                EgressReconnectKind.ADVANCED, generation=generation, replayed=replayed
            ),
        )

    # -- Helpers -------------------------------------------------------------

    def backoff(self, attempt: int) -> int:
        """``min(retry_ceiling, retry_base * 2^(attempt - 1))``."""
        shift = min(max(attempt - 1, 0), 63)
        return min(self._policy.retry_base * (1 << shift), self._policy.retry_ceiling)

    def _insert_pending_ordered(self, record: _Record[T]) -> None:
        for index, existing in enumerate(self._pending):
            if existing.sequence > record.sequence:
                self._pending.insert(index, record)
                return
        self._pending.append(record)
