"""Generic keyed effect compare-and-swap authority (``#lzeffectcas``).

:class:`EffectCasCore` owns, per key:

* stable effect identity and the immutable epoch/plan identity;
* the authority token expected by the exact attempt;
* one in-flight attempt per key, fenced by the actor generation;
* latest-wins pending intent, explicit supersession, retry eligibility on a
  caller-supplied logical clock;
* exact attempt/receipt matching and a monotone ``applied_through`` frontier; and
* the cursor and receipt lineage of an ordered multi-step plan.

The application adapter (:class:`CompareAndApply`) owns a side-effect-free
``observe(key)`` and ONE atomic ``compare_and_apply(attempt)`` boundary, and maps
the backend result to exactly one :class:`EffectApply` outcome. It never retries,
picks a newer intent, clears retained work, or advances a frontier — those are the
core's decisions.

The core owns no thread, executor, timer, or I/O. :class:`EffectCas` and its
thread-safe/async flavors project the core's snapshot as one reactive reader and
invalidate it only for transitions that changed state, after releasing the core
boundary.

Heterogeneous plans are ordered, never atomic: each step's expected authority is
the previous step's returned authority, and a newer epoch stops the old plan
between steps while retaining the completed-step receipts.

Crash recovery: :meth:`EffectCasCore.durable_state` is the full-fidelity image and
:meth:`EffectCasCore.hydrate` restores it (refusing, never repairing, a corrupt
image); a recovering actor then calls :meth:`EffectCasCore.reconnect` with a newer
generation and replays the SAME effect identity.

Spec: ``lazily-spec/docs/durable-sinks.md`` § Generic effect compare-and-swap.
Reference: ``lazily-rs`` ``src/effect_cas_core.rs``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from .slot import Slot
from .thread_safe import ThreadSafeContext


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable


__all__ = [
    "AsyncEffectCas",
    "CompareAndApply",
    "EffectApply",
    "EffectApplyKind",
    "EffectAttempt",
    "EffectAttemptIdentity",
    "EffectCas",
    "EffectCasApplied",
    "EffectCasAppliedKind",
    "EffectCasChange",
    "EffectCasClaim",
    "EffectCasClaimKind",
    "EffectCasCore",
    "EffectCasDurableInflight",
    "EffectCasDurableKey",
    "EffectCasDurableState",
    "EffectCasFailure",
    "EffectCasFailureKind",
    "EffectCasHydrateError",
    "EffectCasKeyState",
    "EffectCasReconnect",
    "EffectCasReconnectKind",
    "EffectCasSnapshot",
    "EffectCasUpsert",
    "EffectCasUpsertKind",
    "EffectIdentity",
    "EffectIntent",
    "EffectPending",
    "EffectPlan",
    "EffectPlanError",
    "EffectReceipt",
    "EffectRetry",
    "EffectTerminal",
    "EffectTerminalKind",
    "ThreadSafeEffectCas",
]


# ---------------------------------------------------------------------------
# Identities, intents, attempts
# ---------------------------------------------------------------------------


class EffectIdentity(str):
    """A stable, non-empty effect identity. A crash never mints a new one for
    already-retained work."""

    __slots__ = ()

    def __new__(cls, value: str) -> EffectIdentity:
        if not isinstance(value, str) or not value:
            raise ValueError("an effect identity must be a non-empty string")
        return super().__new__(cls, value)

    def as_str(self) -> str:
        return str(self)


class EffectPlanError(ValueError):
    EMPTY = "an effect plan must contain at least one step"


@dataclass(frozen=True, slots=True)
class EffectPlan[P]:
    """An ordered, non-empty sequence of idempotent effect payloads."""

    steps: tuple[P, ...]

    def __post_init__(self) -> None:
        if not self.steps:
            raise EffectPlanError(EffectPlanError.EMPTY)

    @classmethod
    def of(cls, steps: Iterable[P]) -> EffectPlan[P]:
        return cls(tuple(steps))

    @classmethod
    def single(cls, payload: P) -> EffectPlan[P]:
        return cls((payload,))

    def __len__(self) -> int:
        return len(self.steps)


@dataclass(frozen=True, slots=True)
class EffectIntent[K, A, P]:
    """Immutable desired effect. One epoch names exactly one identity and plan."""

    identity: EffectIdentity
    epoch: int
    key: K
    expected: A
    plan: EffectPlan[P]


@dataclass(frozen=True, slots=True, order=True)
class EffectAttemptIdentity:
    """Exact identity of one adapter call."""

    effect: EffectIdentity
    generation: int
    epoch: int
    step: int
    attempt: int


@dataclass(frozen=True, slots=True)
class EffectReceipt[R, A]:
    """A successful step receipt, retained as lineage for later plan steps."""

    attempt: EffectAttemptIdentity
    receipt: R
    authority: A


@dataclass(frozen=True, slots=True)
class EffectAttempt[K, A, P, R]:
    """The exact compare-and-apply call handed to an application adapter."""

    identity: EffectAttemptIdentity
    key: K
    expected: A
    payload: P
    prior_receipts: tuple[EffectReceipt[R, A], ...]


class EffectApplyKind(StrEnum):
    APPLIED = "applied"
    SUPERSEDED = "superseded"
    RETRYABLE = "retryable"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class EffectApply:
    """The only outcomes an application adapter may return."""

    kind: EffectApplyKind
    receipt: Any = None
    authority: Any = None
    observed: Any = None
    error: Any = None

    @classmethod
    def applied(cls, receipt: Any, authority: Any) -> EffectApply:
        return cls(EffectApplyKind.APPLIED, receipt=receipt, authority=authority)

    @classmethod
    def superseded(cls, observed: Any) -> EffectApply:
        return cls(EffectApplyKind.SUPERSEDED, observed=observed)

    @classmethod
    def retryable(cls, error: Any) -> EffectApply:
        return cls(EffectApplyKind.RETRYABLE, error=error)

    @classmethod
    def rejected(cls, error: Any) -> EffectApply:
        return cls(EffectApplyKind.REJECTED, error=error)


class CompareAndApply[K, A, P, R](Protocol):
    """Narrow application-owned atomic authority boundary."""

    def observe(self, key: K) -> A:
        """Side-effect-free read of the backend's opaque authority token."""
        ...

    def compare_and_apply(self, attempt: EffectAttempt[K, A, P, R]) -> EffectApply:
        """Apply ``attempt.payload`` only if the backend authority equals
        ``attempt.expected``; a mismatch mutates nothing and returns
        ``SUPERSEDED``."""
        ...


# ---------------------------------------------------------------------------
# Retained state
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EffectPending[K, A, P, R]:
    intent: EffectIntent[K, A, P]
    next_step: int
    expected: A
    receipts: tuple[EffectReceipt[R, A], ...]
    next_attempt: int
    ready_at: int | None


@dataclass(frozen=True, slots=True)
class EffectRetry[E]:
    attempt: EffectAttemptIdentity
    error: E
    retry_at: int


class EffectTerminalKind(StrEnum):
    APPLIED = "applied"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class EffectTerminal:
    """The most recent terminal receipt of a key. ``observed`` is set only for a
    supersession that observed an authority; ``error`` only for a rejection."""

    kind: EffectTerminalKind
    identity: EffectIdentity
    epoch: int
    receipts: tuple[EffectReceipt[Any, Any], ...]
    observed: Any = None
    error: Any = None


@dataclass(frozen=True, slots=True)
class EffectCasKeyState:
    pending: EffectPending[Any, Any, Any, Any] | None
    inflight: EffectAttempt[Any, Any, Any, Any] | None
    retry: EffectRetry[Any] | None
    applied_through: int | None
    terminal: EffectTerminal | None


@dataclass(frozen=True, slots=True)
class EffectCasSnapshot:
    generation: int
    keys: dict[Any, EffectCasKeyState]


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


class EffectCasUpsertKind(StrEnum):
    ACCEPTED = "accepted"
    UNCHANGED = "unchanged"
    ALREADY_APPLIED = "already_applied"
    STALE_EPOCH = "stale_epoch"
    EPOCH_CONFLICT = "epoch_conflict"


@dataclass(frozen=True, slots=True)
class EffectCasUpsert:
    kind: EffectCasUpsertKind
    applied_through: int | None = None
    current: int | None = None


class EffectCasClaimKind(StrEnum):
    CLAIMED = "claimed"
    EMPTY = "empty"
    BUSY = "busy"
    WAITING = "waiting"
    STALE_GENERATION = "stale_generation"


@dataclass(frozen=True, slots=True)
class EffectCasClaim:
    kind: EffectCasClaimKind
    attempt: EffectAttempt[Any, Any, Any, Any] | None = None
    retry_at: int | None = None
    current: int | None = None


class EffectCasAppliedKind(StrEnum):
    STEP_APPLIED = "step_applied"
    COMPLETED = "completed"
    SUPERSEDED = "superseded"
    DUPLICATE = "duplicate"
    UNKNOWN_ATTEMPT = "unknown_attempt"
    STALE_GENERATION = "stale_generation"


@dataclass(frozen=True, slots=True)
class EffectCasApplied:
    kind: EffectCasAppliedKind
    next_step: int | None = None
    applied_through: int | None = None
    current: int | None = None


class EffectCasFailureKind(StrEnum):
    SUPERSEDED = "superseded"
    RETRYING = "retrying"
    REJECTED = "rejected"
    UNKNOWN_ATTEMPT = "unknown_attempt"
    STALE_GENERATION = "stale_generation"


@dataclass(frozen=True, slots=True)
class EffectCasFailure:
    kind: EffectCasFailureKind
    retry_at: int | None = None
    current: int | None = None


class EffectCasReconnectKind(StrEnum):
    ADVANCED = "advanced"
    UNCHANGED = "unchanged"
    STALE_GENERATION = "stale_generation"


@dataclass(frozen=True, slots=True)
class EffectCasReconnect:
    kind: EffectCasReconnectKind
    generation: int | None = None
    requeued: int | None = None
    superseded: int | None = None
    current: int | None = None


@dataclass(frozen=True, slots=True)
class EffectCasChange:
    """Whether a transition changed observable state."""

    state: bool = False


_NO_CHANGE = EffectCasChange()
_CHANGED = EffectCasChange(state=True)


# ---------------------------------------------------------------------------
# Durable image
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EffectCasDurableInflight:
    retained: EffectPending[Any, Any, Any, Any]
    attempt: EffectAttempt[Any, Any, Any, Any]


@dataclass(frozen=True, slots=True)
class EffectCasDurableKey:
    key: Any
    pending: EffectPending[Any, Any, Any, Any] | None
    inflight: EffectCasDurableInflight | None
    retry: EffectRetry[Any] | None
    applied_through: int | None
    terminal: EffectTerminal | None
    terminal_intent: EffectIntent[Any, Any, Any] | None


@dataclass(frozen=True, slots=True)
class EffectCasDurableState:
    """Full-fidelity image of an :class:`EffectCasCore` (``#lzeffectcasdurable``).

    Unlike :class:`EffectCasSnapshot` (a reader projection) it carries the
    retained intent behind an in-flight attempt and the last terminal intent, so
    it can restore the core. Keys are a sequence rather than a map so a
    non-string key survives a JSON round trip.
    """

    generation: int
    keys: tuple[EffectCasDurableKey, ...]


class EffectCasHydrateError(ValueError):
    """A durable image that cannot be hydrated. Hydration never repairs."""

    DUPLICATE_KEY = "durable effect-CAS image repeats a key"
    KEY_MISMATCH = "durable effect-CAS intent belongs to another key"
    CURSOR_OUT_OF_RANGE = "durable effect-CAS plan cursor is out of range"
    ATTEMPT_MISMATCH = "durable effect-CAS attempt does not match its retained cursor"
    FUTURE_GENERATION = "durable effect-CAS attempt is newer than the image generation"


# ---------------------------------------------------------------------------
# The core
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Inflight:
    retained: EffectPending[Any, Any, Any, Any]
    attempt: EffectAttempt[Any, Any, Any, Any]


@dataclass(slots=True)
class _Entry:
    pending: EffectPending[Any, Any, Any, Any] | None = None
    inflight: _Inflight | None = None
    retry: EffectRetry[Any] | None = None
    applied_through: int | None = None
    terminal: EffectTerminal | None = None
    terminal_intent: EffectIntent[Any, Any, Any] | None = None

    def key_state(self) -> EffectCasKeyState:
        return EffectCasKeyState(
            pending=self.pending,
            inflight=None if self.inflight is None else self.inflight.attempt,
            retry=self.retry,
            applied_through=self.applied_through,
            terminal=self.terminal,
        )


class EffectCasCore[K, A, P, R, E]:
    """Pure keyed effect-CAS orchestration authority."""

    __slots__ = ("_generation", "_keys")

    def __init__(self, generation: int) -> None:
        self._generation = generation
        self._keys: dict[K, _Entry] = {}

    @property
    def generation(self) -> int:
        return self._generation

    def _sorted_items(self) -> list[tuple[K, _Entry]]:
        items = list(self._keys.items())
        try:
            return sorted(items, key=lambda item: item[0])
        except TypeError:
            return items

    def snapshot(self) -> EffectCasSnapshot:
        return EffectCasSnapshot(
            generation=self._generation,
            keys={key: entry.key_state() for key, entry in self._sorted_items()},
        )

    def state(self, key: K) -> EffectCasKeyState | None:
        entry = self._keys.get(key)
        return None if entry is None else entry.key_state()

    def pending_keys(self, now: int) -> list[K]:
        """Keys whose retained intent is claimable at logical time ``now``."""
        return [
            key
            for key, entry in self._sorted_items()
            if entry.inflight is None
            and entry.pending is not None
            and (entry.pending.ready_at is None or entry.pending.ready_at <= now)
        ]

    # -- upsert ---------------------------------------------------------------

    def upsert_desired(
        self, intent: EffectIntent[K, A, P]
    ) -> tuple[EffectCasChange, EffectCasUpsert]:
        entry = self._keys.setdefault(intent.key, _Entry())
        if entry.applied_through is not None and intent.epoch <= entry.applied_through:
            return _NO_CHANGE, EffectCasUpsert(
                EffectCasUpsertKind.ALREADY_APPLIED,
                applied_through=entry.applied_through,
            )

        pending_intent = None if entry.pending is None else entry.pending.intent
        inflight_intent = (
            None if entry.inflight is None else entry.inflight.retained.intent
        )
        epochs = [
            candidate.epoch
            for candidate in (pending_intent, inflight_intent, entry.terminal_intent)
            if candidate is not None
        ]
        if epochs:
            current = max(epochs)
            if intent.epoch < current:
                return _NO_CHANGE, EffectCasUpsert(
                    EffectCasUpsertKind.STALE_EPOCH, current=current
                )
            if intent.epoch == current:
                same = intent in (
                    pending_intent,
                    inflight_intent,
                    entry.terminal_intent,
                )
                return _NO_CHANGE, EffectCasUpsert(
                    EffectCasUpsertKind.UNCHANGED
                    if same
                    else EffectCasUpsertKind.EPOCH_CONFLICT
                )

        superseded = entry.pending
        if superseded is not None:
            entry.terminal_intent = superseded.intent
            entry.terminal = EffectTerminal(
                EffectTerminalKind.SUPERSEDED,
                identity=superseded.intent.identity,
                epoch=superseded.intent.epoch,
                receipts=superseded.receipts,
            )
        entry.pending = EffectPending(
            intent=intent,
            next_step=0,
            expected=intent.expected,
            receipts=(),
            next_attempt=1,
            ready_at=None,
        )
        entry.retry = None
        return _CHANGED, EffectCasUpsert(EffectCasUpsertKind.ACCEPTED)

    # -- claim ----------------------------------------------------------------

    def claim(
        self, key: K, generation: int, now: int
    ) -> tuple[EffectCasChange, EffectCasClaim]:
        if generation != self._generation:
            return _NO_CHANGE, EffectCasClaim(
                EffectCasClaimKind.STALE_GENERATION, current=self._generation
            )
        entry = self._keys.get(key)
        if entry is None:
            return _NO_CHANGE, EffectCasClaim(EffectCasClaimKind.EMPTY)
        if entry.inflight is not None:
            return _NO_CHANGE, EffectCasClaim(EffectCasClaimKind.BUSY)
        pending = entry.pending
        if pending is None:
            return _NO_CHANGE, EffectCasClaim(EffectCasClaimKind.EMPTY)
        if pending.ready_at is not None and now < pending.ready_at:
            return _NO_CHANGE, EffectCasClaim(
                EffectCasClaimKind.WAITING, retry_at=pending.ready_at
            )

        entry.pending = None
        attempt = EffectAttempt(
            identity=EffectAttemptIdentity(
                effect=pending.intent.identity,
                generation=generation,
                epoch=pending.intent.epoch,
                step=pending.next_step,
                attempt=pending.next_attempt,
            ),
            key=pending.intent.key,
            expected=pending.expected,
            payload=pending.intent.plan.steps[pending.next_step],
            prior_receipts=pending.receipts,
        )
        entry.inflight = _Inflight(retained=pending, attempt=attempt)
        return _CHANGED, EffectCasClaim(EffectCasClaimKind.CLAIMED, attempt=attempt)

    # -- receipts ---------------------------------------------------------------

    def _matched(self, key: K, attempt: EffectAttemptIdentity) -> _Entry | None:
        entry = self._keys.get(key)
        if entry is None or entry.inflight is None:
            return None
        if entry.inflight.attempt.identity != attempt:
            return None
        return entry

    def record_applied(
        self, key: K, attempt: EffectAttemptIdentity, receipt: R, authority: A
    ) -> tuple[EffectCasChange, EffectCasApplied]:
        if attempt.generation != self._generation:
            return _NO_CHANGE, EffectCasApplied(
                EffectCasAppliedKind.STALE_GENERATION, current=self._generation
            )
        entry = self._keys.get(key)
        if entry is None:
            return _NO_CHANGE, EffectCasApplied(EffectCasAppliedKind.UNKNOWN_ATTEMPT)
        if entry.terminal is not None and any(
            known.attempt == attempt and known.receipt == receipt
            for known in entry.terminal.receipts
        ):
            return _NO_CHANGE, EffectCasApplied(EffectCasAppliedKind.DUPLICATE)
        if self._matched(key, attempt) is None or entry.inflight is None:
            return _NO_CHANGE, EffectCasApplied(EffectCasAppliedKind.UNKNOWN_ATTEMPT)

        inflight = entry.inflight
        entry.inflight = None
        retained = replace(
            inflight.retained,
            receipts=(
                *inflight.retained.receipts,
                EffectReceipt(attempt=attempt, receipt=receipt, authority=authority),
            ),
        )
        entry.retry = None

        if (
            entry.pending is not None
            and entry.pending.intent.epoch > retained.intent.epoch
        ):
            # A newer desired intent stops the old plan immediately.
            entry.terminal_intent = retained.intent
            entry.terminal = EffectTerminal(
                EffectTerminalKind.SUPERSEDED,
                identity=retained.intent.identity,
                epoch=retained.intent.epoch,
                receipts=retained.receipts,
                observed=authority,
            )
            return _CHANGED, EffectCasApplied(EffectCasAppliedKind.SUPERSEDED)

        next_step = retained.next_step + 1
        if next_step < len(retained.intent.plan):
            entry.pending = replace(
                retained,
                next_step=next_step,
                expected=authority,
                ready_at=None,
                next_attempt=1,
            )
            return _CHANGED, EffectCasApplied(
                EffectCasAppliedKind.STEP_APPLIED, next_step=next_step
            )

        epoch = retained.intent.epoch
        entry.applied_through = (
            epoch
            if entry.applied_through is None
            else max(entry.applied_through, epoch)
        )
        entry.terminal_intent = retained.intent
        entry.terminal = EffectTerminal(
            EffectTerminalKind.APPLIED,
            identity=retained.intent.identity,
            epoch=epoch,
            receipts=retained.receipts,
        )
        return _CHANGED, EffectCasApplied(
            EffectCasAppliedKind.COMPLETED, applied_through=entry.applied_through
        )

    def record_superseded(
        self, key: K, attempt: EffectAttemptIdentity, observed: A
    ) -> tuple[EffectCasChange, EffectCasFailure]:
        return self._finish_failed(key, attempt, EffectApply.superseded(observed), 0)

    def record_retryable(
        self, key: K, attempt: EffectAttemptIdentity, error: E, retry_at: int
    ) -> tuple[EffectCasChange, EffectCasFailure]:
        return self._finish_failed(key, attempt, EffectApply.retryable(error), retry_at)

    def record_rejected(
        self, key: K, attempt: EffectAttemptIdentity, error: E
    ) -> tuple[EffectCasChange, EffectCasFailure]:
        return self._finish_failed(key, attempt, EffectApply.rejected(error), 0)

    def record_outcome(
        self,
        key: K,
        attempt: EffectAttemptIdentity,
        outcome: EffectApply,
        retry_at: int,
    ) -> tuple[EffectCasChange, EffectCasApplied | EffectCasFailure]:
        """Route one adapter outcome. ``retry_at`` is used only for RETRYABLE."""
        if outcome.kind is EffectApplyKind.APPLIED:
            return self.record_applied(key, attempt, outcome.receipt, outcome.authority)
        return self._finish_failed(key, attempt, outcome, retry_at)

    def _finish_failed(
        self,
        key: K,
        attempt: EffectAttemptIdentity,
        outcome: EffectApply,
        retry_at: int,
    ) -> tuple[EffectCasChange, EffectCasFailure]:
        if attempt.generation != self._generation:
            return _NO_CHANGE, EffectCasFailure(
                EffectCasFailureKind.STALE_GENERATION, current=self._generation
            )
        entry = self._matched(key, attempt)
        if entry is None or entry.inflight is None:
            return _NO_CHANGE, EffectCasFailure(EffectCasFailureKind.UNKNOWN_ATTEMPT)

        inflight = entry.inflight
        entry.inflight = None
        retained = inflight.retained
        newer_pending = (
            entry.pending is not None
            and entry.pending.intent.epoch > retained.intent.epoch
        )
        identity = retained.intent.identity
        epoch = retained.intent.epoch

        if outcome.kind is EffectApplyKind.SUPERSEDED:
            entry.retry = None
            entry.terminal_intent = retained.intent
            entry.terminal = EffectTerminal(
                EffectTerminalKind.SUPERSEDED,
                identity=identity,
                epoch=epoch,
                receipts=retained.receipts,
                observed=outcome.observed,
            )
            return _CHANGED, EffectCasFailure(EffectCasFailureKind.SUPERSEDED)

        if outcome.kind is EffectApplyKind.RETRYABLE and not newer_pending:
            entry.retry = EffectRetry(
                attempt=attempt, error=outcome.error, retry_at=retry_at
            )
            entry.pending = replace(
                retained,
                next_attempt=retained.next_attempt + 1,
                ready_at=retry_at,
            )
            return _CHANGED, EffectCasFailure(
                EffectCasFailureKind.RETRYING, retry_at=retry_at
            )

        if outcome.kind is EffectApplyKind.RETRYABLE:
            entry.retry = None
            entry.terminal_intent = retained.intent
            entry.terminal = EffectTerminal(
                EffectTerminalKind.SUPERSEDED,
                identity=identity,
                epoch=epoch,
                receipts=retained.receipts,
            )
            return _CHANGED, EffectCasFailure(EffectCasFailureKind.SUPERSEDED)

        if outcome.kind is EffectApplyKind.REJECTED:
            entry.retry = None
            entry.terminal_intent = retained.intent
            entry.terminal = EffectTerminal(
                EffectTerminalKind.REJECTED,
                identity=identity,
                epoch=epoch,
                receipts=retained.receipts,
                error=outcome.error,
            )
            return _CHANGED, EffectCasFailure(EffectCasFailureKind.REJECTED)

        raise ValueError(f"unknown effect outcome {outcome.kind!r}")

    # -- reconnect --------------------------------------------------------------

    def reconnect(
        self, new_generation: int
    ) -> tuple[EffectCasChange, EffectCasReconnect]:
        """Fence older receipts and requeue every in-flight cursor with the same
        effect identity and completed-step lineage, unless a newer desired intent
        supersedes it."""
        if new_generation < self._generation:
            return _NO_CHANGE, EffectCasReconnect(
                EffectCasReconnectKind.STALE_GENERATION, current=self._generation
            )
        if new_generation == self._generation:
            return _NO_CHANGE, EffectCasReconnect(
                EffectCasReconnectKind.UNCHANGED, generation=self._generation
            )

        self._generation = new_generation
        requeued = 0
        superseded = 0
        for _key, entry in self._sorted_items():
            inflight = entry.inflight
            if inflight is None:
                continue
            entry.inflight = None
            retained = inflight.retained
            if (
                entry.pending is not None
                and entry.pending.intent.epoch > retained.intent.epoch
            ):
                entry.terminal_intent = retained.intent
                entry.terminal = EffectTerminal(
                    EffectTerminalKind.SUPERSEDED,
                    identity=retained.intent.identity,
                    epoch=retained.intent.epoch,
                    receipts=retained.receipts,
                )
                superseded += 1
            else:
                entry.pending = replace(
                    retained, next_attempt=retained.next_attempt + 1, ready_at=None
                )
                requeued += 1
        return _CHANGED, EffectCasReconnect(
            EffectCasReconnectKind.ADVANCED,
            generation=new_generation,
            requeued=requeued,
            superseded=superseded,
        )

    # -- durability -------------------------------------------------------------

    def durable_state(self) -> EffectCasDurableState:
        """Full-fidelity image for crash recovery and cross-process persistence."""
        return EffectCasDurableState(
            generation=self._generation,
            keys=tuple(
                EffectCasDurableKey(
                    key=key,
                    pending=entry.pending,
                    inflight=None
                    if entry.inflight is None
                    else EffectCasDurableInflight(
                        retained=entry.inflight.retained,
                        attempt=entry.inflight.attempt,
                    ),
                    retry=entry.retry,
                    applied_through=entry.applied_through,
                    terminal=entry.terminal,
                    terminal_intent=entry.terminal_intent,
                )
                for key, entry in self._sorted_items()
            ),
        )

    @classmethod
    def hydrate(cls, state: EffectCasDurableState) -> EffectCasCore[K, A, P, R, E]:
        """Restore a core from :meth:`durable_state` before the runtime goes live.

        The restored core is observably identical to the writer. A corrupt image
        raises :class:`EffectCasHydrateError`; nothing is repaired, so no adapter
        call ever runs on invented state.
        """
        keys: dict[Any, _Entry] = {}
        for durable in state.keys:
            if durable.pending is not None:
                _validate_retained(durable.key, durable.pending)
            if (
                durable.terminal_intent is not None
                and durable.terminal_intent.key != durable.key
            ):
                raise EffectCasHydrateError(EffectCasHydrateError.KEY_MISMATCH)
            inflight: _Inflight | None = None
            if durable.inflight is not None:
                retained = durable.inflight.retained
                attempt = durable.inflight.attempt
                _validate_retained(durable.key, retained)
                if attempt.identity.generation > state.generation:
                    raise EffectCasHydrateError(EffectCasHydrateError.FUTURE_GENERATION)
                if (
                    attempt.key != durable.key
                    or attempt.identity.effect != retained.intent.identity
                    or attempt.identity.epoch != retained.intent.epoch
                    or attempt.identity.step != retained.next_step
                    or attempt.identity.attempt != retained.next_attempt
                    or attempt.expected != retained.expected
                    or attempt.payload != retained.intent.plan.steps[retained.next_step]
                    or attempt.prior_receipts != retained.receipts
                ):
                    raise EffectCasHydrateError(EffectCasHydrateError.ATTEMPT_MISMATCH)
                inflight = _Inflight(retained=retained, attempt=attempt)
            if durable.key in keys:
                raise EffectCasHydrateError(EffectCasHydrateError.DUPLICATE_KEY)
            keys[durable.key] = _Entry(
                pending=durable.pending,
                inflight=inflight,
                retry=durable.retry,
                applied_through=durable.applied_through,
                terminal=durable.terminal,
                terminal_intent=durable.terminal_intent,
            )
        core: EffectCasCore[K, A, P, R, E] = cls(state.generation)
        core._keys = keys
        return core


def _validate_retained(key: Any, retained: EffectPending[Any, Any, Any, Any]) -> None:
    if retained.intent.key != key:
        raise EffectCasHydrateError(EffectCasHydrateError.KEY_MISMATCH)
    if retained.next_step >= len(retained.intent.plan):
        raise EffectCasHydrateError(EffectCasHydrateError.CURSOR_OUT_OF_RANGE)


# ---------------------------------------------------------------------------
# Reactive shells
# ---------------------------------------------------------------------------


class EffectCas[K, A, P, R, E]:
    """Single-threaded reactive keyed effect-CAS authority.

    One reader projects the core snapshot; it is invalidated only for a
    transition that changed state (a stale generation, unknown attempt,
    duplicate receipt, busy/empty/waiting claim, or refused upsert changes no
    reader version).
    """

    __slots__ = ("_core", "_state", "ctx")

    def __init__(self, ctx: dict, generation: int) -> None:
        self.ctx = ctx
        self._core: EffectCasCore[K, A, P, R, E] = EffectCasCore(generation)
        self._state: Slot[dict, dict, EffectCasSnapshot] = Slot(
            callable=lambda _view: self._run(self._core.snapshot)
        )

    def _run[T](self, operation: Callable[[], T]) -> T:
        return operation()

    def _apply(self, change: EffectCasChange) -> None:
        if change.state:
            self._state.reset(self.ctx)

    @property
    def generation(self) -> int:
        return self._run(lambda: self._core.generation)

    def snapshot(self, ctx: Any = None) -> EffectCasSnapshot:
        """Reactive read of the aggregate snapshot."""
        return self._state(self.ctx if ctx is None else ctx)

    def state_handle(self) -> Slot[dict, dict, EffectCasSnapshot]:
        return self._state

    def state_is_valid(self) -> bool:
        """Whether the snapshot reader is still cached (not invalidated)."""
        return self._state.is_in(self.ctx)

    def state(self, key: K) -> EffectCasKeyState | None:
        return self._run(lambda: self._core.state(key))

    def pending_keys(self, now: int) -> list[K]:
        return self._run(lambda: self._core.pending_keys(now))

    def durable_state(self) -> EffectCasDurableState:
        return self._run(self._core.durable_state)

    def upsert_desired(self, intent: EffectIntent[K, A, P]) -> EffectCasUpsert:
        change, outcome = self._run(lambda: self._core.upsert_desired(intent))
        self._apply(change)
        return outcome

    def claim(self, key: K, generation: int, now: int) -> EffectCasClaim:
        change, outcome = self._run(lambda: self._core.claim(key, generation, now))
        self._apply(change)
        return outcome

    def record_applied(
        self, key: K, attempt: EffectAttemptIdentity, receipt: R, authority: A
    ) -> EffectCasApplied:
        change, outcome = self._run(
            lambda: self._core.record_applied(key, attempt, receipt, authority)
        )
        self._apply(change)
        return outcome

    def record_superseded(
        self, key: K, attempt: EffectAttemptIdentity, observed: A
    ) -> EffectCasFailure:
        change, outcome = self._run(
            lambda: self._core.record_superseded(key, attempt, observed)
        )
        self._apply(change)
        return outcome

    def record_retryable(
        self, key: K, attempt: EffectAttemptIdentity, error: E, retry_at: int
    ) -> EffectCasFailure:
        change, outcome = self._run(
            lambda: self._core.record_retryable(key, attempt, error, retry_at)
        )
        self._apply(change)
        return outcome

    def record_rejected(
        self, key: K, attempt: EffectAttemptIdentity, error: E
    ) -> EffectCasFailure:
        change, outcome = self._run(
            lambda: self._core.record_rejected(key, attempt, error)
        )
        self._apply(change)
        return outcome

    def record_outcome(
        self,
        key: K,
        attempt: EffectAttemptIdentity,
        outcome: EffectApply,
        retry_at: int,
    ) -> EffectCasApplied | EffectCasFailure:
        change, result = self._run(
            lambda: self._core.record_outcome(key, attempt, outcome, retry_at)
        )
        self._apply(change)
        return result

    def reconnect(self, new_generation: int) -> EffectCasReconnect:
        change, outcome = self._run(lambda: self._core.reconnect(new_generation))
        self._apply(change)
        return outcome

    def run_once(
        self, key: K, now: int, adapter: CompareAndApply[K, A, P, R], retry_at: int
    ) -> EffectCasApplied | EffectCasFailure | EffectCasClaim:
        """Claim ``key`` and hand the exact attempt to ``adapter`` OUTSIDE the core
        boundary, then record its outcome. Returns the claim refusal when nothing
        was claimable. The adapter never decides policy."""
        claim = self.claim(key, self.generation, now)
        if claim.kind is not EffectCasClaimKind.CLAIMED or claim.attempt is None:
            return claim
        outcome = adapter.compare_and_apply(claim.attempt)
        return self.record_outcome(key, claim.attempt.identity, outcome, retry_at)


class ThreadSafeEffectCas[K, A, P, R, E](EffectCas[K, A, P, R, E]):
    """Effect-CAS serialized by a ``ThreadSafeContext``; invalidation runs after
    the lock is released."""

    __slots__ = ("_thread_safe",)

    def __init__(
        self, ctx: dict, generation: int, *, ts: ThreadSafeContext | None = None
    ) -> None:
        self._thread_safe = ts if ts is not None else ThreadSafeContext()
        super().__init__(ctx, generation)

    @property
    def thread_safe_context(self) -> ThreadSafeContext:
        return self._thread_safe

    def _run[T](self, operation: Callable[[], T]) -> T:
        with self._thread_safe.lock:
            return operation()


class AsyncEffectCas[K, A, P, R, E](EffectCas[K, A, P, R, E]):
    """Async-graph shell. Transitions stay synchronous; only the adapter call a
    driver awaits is async-coloured, and it runs outside the core boundary."""

    __slots__ = ()
