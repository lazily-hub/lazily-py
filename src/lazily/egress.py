"""``EgressCell`` — the single-threaded flavor of the transport-agnostic reactive
egress family (``#lzegress``).

The delivery authority lives in the flavor-neutral
:class:`~lazily.egress_core.EgressCore`; this shell adds only the reactivity: the
four delivery-state projections (``pending``, ``inflight``, ``acked_through``,
``retry``) as memoized readers minted on *this* context, and one transport
attachment :class:`~lazily.effect.Effect` per incarnation.

**This is reactive projection, not a request/ACK protocol.** Enqueue invalidates
``pending``; the attachment Effect observes it, asks the core to claim eligible
envelopes and sends them; an acknowledgement invalidates ``inflight`` and re-runs
the same attachment when it reopens the bounded window. The acknowledgement is
domain delivery input to the core, not an acknowledgement of a projection
request. Every send-side decision — claim, ack, failure, retry, reconnect — is the
core's, so the Effect cannot duplicate authority.

**Generation fence.** :meth:`EgressCell.attach_transport` captures the generation
current when it attaches and supplies it to every transition. After
:meth:`EgressCell.reconnect` the old attachment stays attached but is inert at the
core fence; the replacement transport gets one new attachment, which replays the
retained pending projection under the new incarnation.

**No observers.** Like every reactive in this library the egress exposes no
listener registry; a consumer that wants to act on delivery state writes an
Effect over a reader handle.

Spec: ``lazily-spec/docs/transport-egress.md``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from .batch import batch
from .effect import Effect
from .egress_core import (
    EgressAck,
    EgressChange,
    EgressClaim,
    EgressClaimKind,
    EgressCore,
    EgressEnvelope,
    EgressFailure,
    EgressPolicy,
    EgressReconnect,
    EgressRetry,
    EgressRetryAction,
)
from .slot import Slot


if TYPE_CHECKING:
    from collections.abc import Callable


__all__ = ["EgressCell", "EgressTransport"]


class EgressTransport[T](Protocol):
    """The only transport seam egress owns. Framing and durability stay
    separate adapters around the envelope."""

    def send(self, envelope: EgressEnvelope[T]) -> bool:
        """``True`` means the transport accepted the send attempt. Domain
        delivery is proven later by :meth:`EgressCell.ack`."""
        ...


class EgressCell[T]:
    """A reactive egress: four delivery-state readers over one
    :class:`~lazily.egress_core.EgressCore`."""

    __slots__ = (
        "_acked_through",
        "_core",
        "_inflight",
        "_pending",
        "_retry",
        "ctx",
    )

    def __init__(
        self, ctx: dict, generation: int, policy: EgressPolicy | None = None
    ) -> None:
        self.ctx = ctx
        self._core: EgressCore[T] = EgressCore(generation, policy)
        self._pending: Slot[dict, dict, list[EgressEnvelope[T]]] = Slot(
            callable=lambda _view: self._run(self._core.pending)
        )
        self._inflight: Slot[dict, dict, list[EgressEnvelope[T]]] = Slot(
            callable=lambda _view: self._run(self._core.inflight)
        )
        self._acked_through: Slot[dict, dict, int | None] = Slot(
            callable=lambda _view: self._run(self._core.acked_through)
        )
        self._retry: Slot[dict, dict, EgressRetry | None] = Slot(
            callable=lambda _view: self._run(self._core.retry)
        )

    # -- Storage boundary ----------------------------------------------------

    def _run[R](self, operation: Callable[[], R]) -> R:
        """Run one core access under this flavor's storage boundary.

        The single-threaded flavor has none. The seam lets the thread-safe flavor
        hold its lock for the core access ONLY and invalidate with it released:
        a reader body takes the same boundary, so invalidating while holding it
        inverts the lock order against a concurrent reader.
        """
        return operation()

    def _apply(self, change: EgressChange) -> None:
        """Clear exactly the reader kinds ``change`` names, in ONE frontier walk.

        Called with the storage boundary released. A no-op or stale-generation
        transition clears nothing.
        """
        if change.is_empty():
            return
        roots: list[Slot[dict, dict, Any]] = []
        if change.pending:
            roots.append(self._pending)
        if change.inflight:
            roots.append(self._inflight)
        if change.acked_through:
            roots.append(self._acked_through)
        if change.retry:
            roots.append(self._retry)

        def resets() -> None:
            for root in roots:
                root.reset(self.ctx)

        batch(resets)

    # -- Non-reactive projections -------------------------------------------

    @property
    def policy(self) -> EgressPolicy:
        return self._core.policy

    @property
    def generation(self) -> int:
        return self._run(lambda: self._core.generation)

    @property
    def next_sequence(self) -> int:
        return self._run(lambda: self._core.next_sequence)

    # -- Mutators ------------------------------------------------------------

    def enqueue(self, payload: T) -> int:
        """Assign the next sequence to ``payload``; returns that sequence."""
        change, sequence = self._run(lambda: self._core.enqueue(payload))
        self._apply(change)
        return sequence

    def claim(self, generation: int) -> EgressClaim[T]:
        """Claim the oldest pending envelope for the attachment at ``generation``."""
        change, claim = self._run(lambda: self._core.claim(generation))
        self._apply(change)
        return claim

    def ack(self, generation: int, through: int) -> EgressAck:
        """Cumulatively acknowledge every sequence at or below ``through``."""
        change, ack = self._run(lambda: self._core.ack(generation, through))
        self._apply(change)
        return ack

    def fail(self, generation: int, sequence: int) -> EgressFailure:
        """Record a failed send of ``sequence``; parks or exhausts it."""
        change, failure = self._run(lambda: self._core.fail(generation, sequence))
        self._apply(change)
        return failure

    def retry_now(self, generation: int, sequence: int) -> EgressRetryAction:
        """Return a parked failed record to pending after its backoff elapsed."""
        change, action = self._run(lambda: self._core.retry_now(generation, sequence))
        self._apply(change)
        return action

    def reconnect(self, generation: int) -> EgressReconnect:
        """Fence the old attachment and replay unacknowledged records."""
        change, reconnect = self._run(lambda: self._core.reconnect(generation))
        self._apply(change)
        return reconnect

    # -- Reactive reads ------------------------------------------------------

    def pending(self, ctx: Any = None) -> list[EgressEnvelope[T]]:
        """Reactive read: envelopes awaiting a claim, oldest sequence first."""
        return self._pending(self.ctx if ctx is None else ctx)

    def inflight(self, ctx: Any = None) -> list[EgressEnvelope[T]]:
        """Reactive read: the unacknowledged window, in sequence order."""
        return self._inflight(self.ctx if ctx is None else ctx)

    def acked_through(self, ctx: Any = None) -> int | None:
        """Reactive read: the cumulative acknowledgement watermark."""
        return self._acked_through(self.ctx if ctx is None else ctx)

    def retry(self, ctx: Any = None) -> EgressRetry | None:
        """Reactive read: retry evidence for the most recent failed send."""
        return self._retry(self.ctx if ctx is None else ctx)

    # -- Reader handles ------------------------------------------------------

    def pending_handle(self) -> Slot[dict, dict, list[EgressEnvelope[T]]]:
        return self._pending

    def inflight_handle(self) -> Slot[dict, dict, list[EgressEnvelope[T]]]:
        return self._inflight

    def acked_through_handle(self) -> Slot[dict, dict, int | None]:
        return self._acked_through

    def retry_handle(self) -> Slot[dict, dict, EgressRetry | None]:
        return self._retry

    # -- Cache-validity probes ----------------------------------------------
    #
    # ``invalidates`` is a claim about the GRAPH, and only the shell can answer
    # it: whether a reader's memoized value is still cached. That is what makes a
    # fixture's ``invalidates: false`` falsifiable.

    def pending_is_valid(self) -> bool:
        return self._pending.is_in(self.ctx)

    def inflight_is_valid(self) -> bool:
        return self._inflight.is_in(self.ctx)

    def acked_through_is_valid(self) -> bool:
        return self._acked_through.is_in(self.ctx)

    def retry_is_valid(self) -> bool:
        return self._retry.is_in(self.ctx)

    # -- Transport attachment -----------------------------------------------

    def _drain_into(
        self, generation: int, send: Callable[[EgressEnvelope[T]], bool]
    ) -> None:
        """Claim and send until the core refuses a claim or a send fails.

        A failed send is parked by the core (never re-pended), so a failing
        transport cannot spin the attachment in a reactive retry loop.
        """
        while True:
            claim = self.claim(generation)
            if claim.kind is not EgressClaimKind.CLAIMED or claim.envelope is None:
                return
            if not send(claim.envelope):
                self.fail(generation, claim.envelope.sequence)
                return

    def attach_transport(self, transport: EgressTransport[T]) -> Effect:
        """Attach ONE transport Effect at the current producer generation.

        A reconnect does not move this Effect's captured generation, so the old
        attachment becomes inert at the core fence; attach one replacement for
        the new incarnation.
        """
        generation = self.generation

        def body(view: Any) -> None:
            self._drain_into(generation, transport.send)
            # Subscribe AFTER the claims. Claiming clears the very readers this
            # Effect depends on, and an invalidation that lands while the body
            # is running is suppressed (the body is already running), so reads
            # taken before the claims would leave the attachment subscribed to
            # nothing. `inflight` is a dependency even though the core stays the
            # authority: an acknowledgement reopens the window and must re-run
            # this same attachment.
            self._pending(view)
            self._inflight(view)

        attachment = Effect(body)
        attachment(self.ctx)
        return attachment
