"""Egress-family shells for the thread-safe and async execution flavors (``#lzegress``).

Same shape as :mod:`lazily.ingress_flavors`: the delivery authority is already
independent of the graph (:class:`~lazily.egress_core.EgressCore`) and
:class:`~lazily.egress.EgressCell` separates *mutating the core* from *clearing
the readers* through the ``_run`` storage-boundary seam. A flavor is the choice of
that boundary plus, for async, the colour of the transport attachment. The four
reader kinds and every transition are shared verbatim — all three flavors obey ONE
contract and replay ONE corpus.

**Thread-safe.** :class:`ThreadSafeEgressCell` holds the
:class:`~lazily.thread_safe.ThreadSafeContext` lock for the core access only and
invalidates with it released, through one ``batch`` (one frontier walk per
transition). Delivery authority stays in the shared core; the attachment is still
one Effect per incarnation.

**Async.** :class:`AsyncEgressCell` keeps the delivery-state readers synchronous —
whether an envelope is claimable is immediate state-machine work with nothing to
await. Only the transport attachment is async-coloured:
:meth:`AsyncEgressCell.attach_transport_async` takes an ``async def send``
transport and drives it from an ``asyncio`` task scheduled by the attachment
Effect.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any, Protocol

from .effect import Effect
from .egress import EgressCell
from .egress_core import EgressClaimKind
from .thread_safe import ThreadSafeContext


if TYPE_CHECKING:
    from collections.abc import Callable

    from .egress_core import EgressEnvelope, EgressPolicy


__all__ = [
    "AsyncEgressAttachment",
    "AsyncEgressCell",
    "AsyncEgressTransport",
    "ThreadSafeEgressCell",
]


class ThreadSafeEgressCell[T](EgressCell[T]):
    """A ``Send + Sync``-equivalent egress serialized by a ``ThreadSafeContext``."""

    __slots__ = ("_thread_safe",)

    def __init__(
        self,
        ctx: dict,
        generation: int,
        policy: EgressPolicy | None = None,
        *,
        ts: ThreadSafeContext | None = None,
    ) -> None:
        self._thread_safe = ts if ts is not None else ThreadSafeContext()
        super().__init__(ctx, generation, policy)

    @property
    def thread_safe_context(self) -> ThreadSafeContext:
        """The context whose lock serializes this egress."""
        return self._thread_safe

    def _run[R](self, operation: Callable[[], R]) -> R:
        # The storage boundary only; `_apply` runs after the lock is released.
        with self._thread_safe.lock:
            return operation()


class AsyncEgressTransport[T](Protocol):
    """An async-coloured transport seam."""

    async def send(self, envelope: EgressEnvelope[T]) -> bool:
        """``True`` means the transport accepted the send attempt."""
        ...


class AsyncEgressAttachment[T]:
    """One async transport attachment: an Effect that observes ``pending`` /
    ``inflight`` and an ``asyncio`` task that claims and awaits sends.

    At most one drain task runs per attachment; an invalidation that arrives
    while it runs asks it for one more pass instead of starting a second one.
    """

    __slots__ = (
        "_again",
        "_cell",
        "_disposed",
        "_effect",
        "_generation",
        "_guard",
        "_loop",
        "_scheduled",
        "_task",
        "_transport",
    )

    def __init__(
        self,
        cell: AsyncEgressCell[T],
        transport: AsyncEgressTransport[T],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._cell = cell
        self._transport = transport
        self._loop = loop
        self._generation = cell.generation
        self._guard = threading.Lock()
        self._scheduled = False
        self._again = False
        self._disposed = False
        self._task: asyncio.Task[None] | None = None
        self._effect = Effect(self._observe)
        self._effect(cell.ctx)

    @property
    def generation(self) -> int:
        """The incarnation this attachment captured."""
        return self._generation

    @property
    def effect(self) -> Effect:
        return self._effect

    def _observe(self, view: Any) -> None:
        # Synchronous delivery-state reads; only the send below is awaited.
        has_pending = bool(self._cell.pending_handle()(view))
        self._cell.inflight_handle()(view)
        if has_pending:
            self._request()

    def _request(self) -> None:
        with self._guard:
            if self._disposed:
                return
            if self._scheduled or self._task is not None:
                self._again = True
                return
            self._scheduled = True
        self._loop.call_soon_threadsafe(self._spawn)

    def _spawn(self) -> None:
        with self._guard:
            self._scheduled = False
            if self._disposed:
                return
            self._task = self._loop.create_task(self._drain())

    async def _drain(self) -> None:
        try:
            while True:
                with self._guard:
                    self._again = False
                await self._pass()
                with self._guard:
                    if self._disposed or not self._again:
                        return
        finally:
            with self._guard:
                self._task = None

    async def _pass(self) -> None:
        while not self._disposed:
            claim = self._cell.claim(self._generation)
            if claim.kind is not EgressClaimKind.CLAIMED or claim.envelope is None:
                return
            if not await self._transport.send(claim.envelope):
                self._cell.fail(self._generation, claim.envelope.sequence)
                return

    async def settle(self) -> None:
        """Await until no drain task is scheduled or running."""
        while True:
            with self._guard:
                task = self._task
                scheduled = self._scheduled
            if task is not None:
                await task
            elif scheduled:
                await asyncio.sleep(0)
            else:
                return

    def dispose(self) -> None:
        """Detach: no further claims are made by this attachment."""
        with self._guard:
            self._disposed = True
        self._effect.dispose()


class AsyncEgressCell[T](EgressCell[T]):
    """The async-graph egress. Delivery-state readers stay synchronous; only the
    transport attachment is async-coloured."""

    __slots__ = ()

    def attach_transport_async(
        self, transport: AsyncEgressTransport[T]
    ) -> AsyncEgressAttachment[T]:
        """Attach one async transport at the current producer generation.

        Must be called from inside a running event loop, which the attachment
        schedules its drain task on.
        """
        return AsyncEgressAttachment(self, transport, asyncio.get_running_loop())
