"""Async reactive context — the coordination layer over the async primitives.

The Python counterpart of ``lazily-spec/docs/async.md``. This is the eighth
binding's async surface; the closest structural sibling is ``lazily-dart``'s
``AsyncContext``, because both languages are garbage-collected and object-shaped.

**What this module is.** A *thin coordination wrapper*, in the same idiom as
:class:`~lazily.thread_safe.ThreadSafeContext`. The reactive state machines
already live in :mod:`lazily.async_slot` (``Empty``/``Computing``/``Resolved``/
``Error`` with revision-tracked stale-completion discard and one-in-flight-per-
revision deduplication) and :mod:`lazily.async_effect` (queued reruns,
cleanup-before-body, terminal disposal). :class:`AsyncContext` adds only what a
single primitive cannot own by itself:

* the **dependency graph** between handles (dependency -> dependents), so an
  input write invalidates the transitive cone,
* the **synchronous batch boundary** (``batch``), so writes coalesce into one
  invalidation pass and async reruns fire only after the outermost batch exits,
* **executor scheduling** of effect reruns (a rerun is a task on the loop, never
  inline within ``set_cell``/``batch``), and
* **context disposal**, which cancels in-flight computations and awaits every
  active cleanup future.

**What this module deliberately is not.** It is *not* a node arena. ``lazily-rs``
models its async context as a table of nodes addressed by integer handles, so
its context necessarily carries ``dispose_slot``/``dispose_cell``, teardown
scopes, and degree introspection. In ``lazily-py`` every reactive primitive is a
real object that owns its own state, disposal is handle-side
(:meth:`AsyncEffectHandle.dispose_async`, matching :meth:`lazily.effect.Effect.dispose`),
and the synchronous surface has no ``Context`` object at all. Adding an arena
here would be a foreign architecture, so the context owns edges and scheduling
only.

**Dependency tracking is compute-context based, never ambient.** ``async.md``
§ "Dependency tracking" forbids a thread-local tracking stack because it does not
survive suspension across ``await``. Python makes this especially sharp: calling
an ``async def`` returns a coroutine without executing a single line of its body,
so an ambient "currently-computing" global would capture nothing at all. Reads
therefore go through :class:`AsyncComputeContext`, which registers the edge
*before* the awaited read.
"""

from __future__ import annotations


__all__ = [
    "AsyncCellHandle",
    "AsyncComputeContext",
    "AsyncContext",
    "AsyncContextDisposedError",
    "AsyncEffectHandle",
    "AsyncSlotHandle",
]

import asyncio
from typing import TYPE_CHECKING, Any

from .async_effect import AsyncEffect, EffectState
from .async_slot import AsyncSlot, SlotState


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


_MISSING: Any = object()


class AsyncContextDisposedError(RuntimeError):
    """Raised when an async read is attempted on a disposed context."""


class AsyncCellHandle[T]:
    """A mutable input cell on the async graph — the *synchronous* input layer.

    Writes are synchronous and invalidate the dependent cone (or queue it, inside
    a :meth:`AsyncContext.batch`). Reads through :meth:`get` are non-reactive;
    an async compute or effect body registers a dependency edge by reading via
    :meth:`AsyncComputeContext.get_cell`, per ``async.md`` § "Dependency
    tracking".
    """

    __slots__ = ("_ctx", "_value")

    def __init__(self, ctx: AsyncContext, value: T) -> None:
        self._ctx = ctx
        self._value = value

    def get(self) -> T:
        """Read the current value (synchronous, non-reactive)."""
        return self._value

    @property
    def peek(self) -> T:
        """Alias of :meth:`get`, for parity with the other bindings."""
        return self._value

    def set(self, value: T) -> None:
        """Write a new value. Guarded by ``!=`` (the same PartialEq guard the
        synchronous :class:`~lazily.cell.Cell` uses), so an equal write is a
        no-op and never invalidates."""
        if value != self._value:
            self._value = value
            self._ctx._invalidate_dependents(self)

    def __repr__(self) -> str:
        return f"AsyncCellHandle({self._value!r})"


class AsyncSlotHandle[T]:
    """A computed async slot: a lazily-computed, memoized, awaited derived value.

    The lifecycle is *not* reimplemented here — it is delegated wholesale to
    :class:`~lazily.async_slot.AsyncSlot`, which supplies the four-state machine,
    revision-tracked stale-completion discard (conformance point 2), the
    ``get_async`` re-resolve loop across both benign race windows (point 4), and
    one-in-flight-computation-per-revision deduplication. This handle adds the
    two things the bare slot has no way to know about: dependency edges into the
    owning context, and an optional equality memo guard.
    """

    __slots__ = ("_compute", "_ctx", "_dependencies", "_eq", "_memo", "_slot")

    def __init__(
        self,
        ctx: AsyncContext,
        compute: Callable[[AsyncComputeContext], Awaitable[T]],
        *,
        eq: Callable[[T, T], bool] | None = None,
    ) -> None:
        self._ctx = ctx
        self._compute = compute
        self._eq = eq
        self._memo: Any = _MISSING
        self._dependencies: set[object] = set()
        self._slot: AsyncSlot[T] = AsyncSlot(self._run)

    # -- lifecycle delegation ------------------------------------------- #

    @property
    def state(self) -> SlotState:
        """The delegated :class:`~lazily.async_slot.SlotState`."""
        return self._slot.state

    @property
    def revision(self) -> int:
        """The delegated revision counter."""
        return self._slot.revision

    def get(self) -> T | None:
        """Synchronous cached read: the value when ``Resolved``, else ``None``.
        The warm fast path; never spawns a computation."""
        return self._slot.get()

    async def get_async(self) -> T:
        """Await the slot's value, attaching to the in-flight computation for the
        current revision rather than spawning a duplicate.

        The delegated read is **shielded**. :class:`AsyncSlot` hands every
        concurrent waiter the *same* :class:`asyncio.Future`, and cancelling a
        task that is awaiting a future cancels that future — so without the
        shield, one caller abandoning its ``get_async`` would cancel the shared
        in-flight computation out from under the remaining waiters. Cancellation
        property 1 requires the opposite: dropping one waiter is safe.
        """
        if self._ctx._disposed:
            raise AsyncContextDisposedError("AsyncContext disposed")
        return await asyncio.shield(self._slot.get_async())

    def invalidate(self) -> None:
        """Mark the slot stale and propagate through the dependent cone."""
        self._slot.invalidate()
        self._ctx._invalidate_dependents(self)

    def hard_clear(self) -> None:
        """Reset to ``Empty`` and bump the revision, discarding any in-flight
        completion (explicit cancellation, conformance point 3)."""
        self._slot.hard_clear()

    # -- internal -------------------------------------------------------- #

    async def _run(self) -> T:
        """The compute body handed to the delegated :class:`AsyncSlot`.

        Detaches the previous dependency set before re-tracking (``async.md``:
        "on rerun, stale dependencies are removed and new ones registered"), runs
        the user compute against a fresh :class:`AsyncComputeContext`, and applies
        the memo guard.
        """
        if self._ctx._disposed:
            # Disposal is checked *inside* the compute, not only at the
            # ``get_async`` entry: the re-resolve loop lives in the delegated
            # :class:`AsyncSlot`, so a waiter that is mid-loop when the context
            # is disposed would otherwise respawn and publish a value from a
            # disposed graph (cancellation property 4).
            raise AsyncContextDisposedError("AsyncContext disposed")
        self._detach()
        prior = self._memo
        value = await self._compute(AsyncComputeContext(self._ctx, self))
        eq = self._eq
        if eq is not None and prior is not _MISSING and eq(prior, value):
            # Memo equality suppression: keep the previously published value
            # (identity preserved), so nothing downstream sees a new object.
            return prior
        self._memo = value
        return value

    def _detach(self) -> None:
        for dep in self._dependencies:
            edges = self._ctx._dependents.get(dep)
            if edges is not None:
                edges.discard(self)
        self._dependencies.clear()

    def _track(self, dependency: object) -> None:
        if dependency not in self._dependencies:
            self._dependencies.add(dependency)
            self._ctx._dependents.setdefault(dependency, set()).add(self)


class AsyncEffectHandle:
    """An async effect: a side-effecting observer that reruns when a tracked
    dependency invalidates, with an optional (sync or async) cleanup.

    The scheduling lifecycle is delegated to
    :class:`~lazily.async_effect.AsyncEffect`, which supplies queue-never-fire
    invalidation, cleanup-before-body ordering, and terminal disposal
    (conformance points 5 and 6). This handle adds dependency edges and the
    executor hop: a rerun is dispatched as an :mod:`asyncio` task, so it never
    runs inline within ``set_cell`` or a ``batch`` callback.
    """

    __slots__ = ("_body", "_ctx", "_dependencies", "_disposed", "_effect", "_task")

    def __init__(
        self,
        ctx: AsyncContext,
        body: Callable[[AsyncComputeContext], Awaitable[Any]],
    ) -> None:
        self._ctx = ctx
        self._body = body
        self._disposed = False
        self._dependencies: set[object] = set()
        self._effect = AsyncEffect(self._run)
        self._task: asyncio.Task[None] | None = None

    @property
    def state(self) -> EffectState:
        """The delegated :class:`~lazily.async_effect.EffectState`."""
        return self._effect.state

    async def dispose_async(self) -> None:
        """Remove pending reruns, await the in-flight body and cleanup, detach
        dependency edges, and go terminal."""
        self._disposed = True
        self._ctx._effects.discard(self)
        task = self._task
        self._task = None
        if task is not None and not task.done():
            await task
        await self._effect.dispose()
        self._detach()

    async def settle(self) -> None:
        """Await the currently scheduled rerun (if any).

        The spec requires reruns to be *scheduled*, not inline, which means a
        synchronous ``set_cell`` returns before the effect body has run. Tests
        and callers that need the post-invalidation steady state await this."""
        while True:
            task = self._task
            if task is None or task.done():
                return
            await task

    # -- internal -------------------------------------------------------- #

    def _schedule(self) -> None:
        """Queue a rerun and ensure a flush task is pending on the executor."""
        if self._disposed:
            return
        self._effect.invalidate()
        if self._task is not None and not self._task.done():
            # A flush is already in flight; its loop picks up the new queued
            # rerun (after the current cleanup completes).
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop: the rerun stays queued and fires on the next
            # settle()/flush from inside a loop.
            self._task = None
            return
        self._task = loop.create_task(self._effect.flush())

    async def _run(self) -> Callable[[], Awaitable[None]] | None:
        if self._disposed or self._ctx._disposed:
            return None
        self._detach()
        result = await self._body(AsyncComputeContext(self._ctx, self))
        if result is None:
            return None
        if callable(result):
            return _as_async_cleanup(result)
        return None

    def _detach(self) -> None:
        for dep in self._dependencies:
            edges = self._ctx._dependents.get(dep)
            if edges is not None:
                edges.discard(self)
        self._dependencies.clear()

    def _track(self, dependency: object) -> None:
        if dependency not in self._dependencies:
            self._dependencies.add(dependency)
            self._ctx._dependents.setdefault(dependency, set()).add(self)


def _as_async_cleanup(fn: Callable[[], Any]) -> Callable[[], Awaitable[None]]:
    """Normalize a sync-or-async cleanup callable into an awaitable one."""

    async def _cleanup() -> None:
        result = fn()
        if asyncio.iscoroutine(result):
            await result

    return _cleanup


class AsyncComputeContext:
    """The reader handed to an async compute or effect body.

    Every read through this object registers the dependency edge **before** the
    awaited read, so an invalidation arriving while the computation is suspended
    supersedes it rather than letting it publish stale data. This replaces the
    ambient tracking stack the synchronous graph uses, which ``async.md`` forbids
    on the async surface — and which would in any case capture nothing in Python,
    where calling an ``async def`` executes none of its body.
    """

    __slots__ = ("_ctx", "_node")

    def __init__(
        self, ctx: AsyncContext, node: AsyncSlotHandle[Any] | AsyncEffectHandle
    ) -> None:
        self._ctx = ctx
        self._node = node

    def get_cell[T](self, cell: AsyncCellHandle[T]) -> T:
        """Read a cell synchronously, recording it as a dependency."""
        self._node._track(cell)
        return cell.get()

    async def get_async[T](self, slot: AsyncSlotHandle[T]) -> T:
        """Await a slot's value, recording the edge *before* the awaited read."""
        self._node._track(slot)
        return await slot.get_async()

    def get[T](self, slot: AsyncSlotHandle[T]) -> T | None:
        """Non-blocking cached read of a slot, recording it as a dependency."""
        self._node._track(slot)
        return slot.get()


class AsyncContext:
    """The async reactive surface: a dependency graph, a batch boundary, and an
    executor for effect reruns.

    A distinct graph from the synchronous primitives — not an overload of them —
    because futures introduce in-flight state, cancellation, stale completion,
    and dependency tracking across suspension points that the synchronous graph
    has no notion of. Only *resolved* slot values ever cross IPC/FFI, as ordinary
    cell payloads: this is compute, not protocol.
    """

    __slots__ = (
        "_batch_queue",
        "_dependents",
        "_depth",
        "_disposed",
        "_effects",
        "_slots",
    )

    def __init__(self) -> None:
        self._dependents: dict[object, set[Any]] = {}
        self._effects: set[AsyncEffectHandle] = set()
        self._slots: set[AsyncSlotHandle[Any]] = set()
        self._disposed = False
        self._depth = 0
        self._batch_queue: list[object] = []

    @property
    def disposed(self) -> bool:
        return self._disposed

    # -- handles --------------------------------------------------------- #

    def cell[T](self, value: T) -> AsyncCellHandle[T]:
        """Create a mutable input cell."""
        return AsyncCellHandle(self, value)

    def get_cell[T](self, handle: AsyncCellHandle[T]) -> T:
        """Read a cell's value (synchronous)."""
        return handle.get()

    def set_cell[T](self, handle: AsyncCellHandle[T], value: T) -> None:
        """Update a cell and invalidate dependents. Inside a :meth:`batch`, the
        invalidation is queued and fires once at the outermost boundary."""
        handle.set(value)

    def computed_async[T](
        self, compute: Callable[[AsyncComputeContext], Awaitable[T]]
    ) -> AsyncSlotHandle[T]:
        """Create an async computed slot."""
        slot: AsyncSlotHandle[T] = AsyncSlotHandle(self, compute)
        self._slots.add(slot)
        return slot

    def memo_async[T](
        self,
        compute: Callable[[AsyncComputeContext], Awaitable[T]],
        eq: Callable[[T, T], bool],
    ) -> AsyncSlotHandle[T]:
        """Like :meth:`computed_async` with an equality memo guard: a recompute
        yielding an equal value republishes the *previous* value object."""
        slot: AsyncSlotHandle[T] = AsyncSlotHandle(self, compute, eq=eq)
        self._slots.add(slot)
        return slot

    def get[T](self, handle: AsyncSlotHandle[T]) -> T | None:
        """Synchronous cached read of a slot (the warm fast path)."""
        return handle.get()

    async def get_async[T](self, handle: AsyncSlotHandle[T]) -> T:
        """Await a slot's value."""
        return await handle.get_async()

    def effect_async(
        self, body: Callable[[AsyncComputeContext], Awaitable[Any]]
    ) -> AsyncEffectHandle:
        """Create an async effect. The body may return a sync or async cleanup
        callable, which completes before the next body starts. The initial run is
        scheduled on the executor, not run inline."""
        handle = AsyncEffectHandle(self, body)
        self._effects.add(handle)
        handle._schedule()
        return handle

    async def dispose_async_effect(self, handle: AsyncEffectHandle) -> None:
        """Dispose an async effect and await its cleanup."""
        await handle.dispose_async()

    # -- batch ----------------------------------------------------------- #

    def batch[R](self, run: Callable[[], R]) -> R:
        """A **synchronous** batch boundary. Cell writes inside ``run`` queue
        invalidation roots; the queued roots propagate once at the outermost
        exit, so a dependent reached through several changed cells is invalidated
        once (the coalesced frontier). Async reruns are scheduled at that exit
        and execute afterwards on the loop, never inside ``run``."""
        self._depth += 1
        try:
            return run()
        finally:
            self._depth -= 1
            if self._depth == 0:
                queue = self._batch_queue
                self._batch_queue = []
                seen: set[int] = set()
                for dep in queue:
                    if id(dep) in seen:
                        continue
                    seen.add(id(dep))
                    self._invalidate_dependents(dep)

    # -- graph ----------------------------------------------------------- #

    def _invalidate_dependents(self, dependency: object) -> None:
        """Propagate an invalidation through the transitive dependent cone.

        Slots are marked stale (their next ``get_async`` respawns against the new
        revision); effects queue a rerun on the executor. A ``visited`` set makes
        the walk terminate on a cyclic or diamond-shaped graph, and means a
        dependent reached through several changed sources is touched once.
        """
        if self._disposed:
            return
        if self._depth > 0:
            self._batch_queue.append(dependency)
            return
        visited: set[int] = {id(dependency)}
        frontier: list[object] = [dependency]
        while frontier:
            node = frontier.pop()
            for dependent in list(self._dependents.get(node, ())):
                if id(dependent) in visited:
                    continue
                visited.add(id(dependent))
                if isinstance(dependent, AsyncEffectHandle):
                    dependent._schedule()
                else:
                    dependent._slot.invalidate()
                    frontier.append(dependent)

    # -- disposal -------------------------------------------------------- #

    async def dispose_async(self) -> None:
        """Dispose the context: cancel every in-flight computation and await
        every active cleanup future before returning (conformance point 4).
        Subsequent cell writes and async reads are inert."""
        self._disposed = True
        effects = list(self._effects)
        if effects:
            await asyncio.gather(*(e.dispose_async() for e in effects))
        for slot in self._slots:
            slot.hard_clear()
        self._dependents.clear()
        self._effects.clear()
        self._slots.clear()
