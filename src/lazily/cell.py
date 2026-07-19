__all__ = ["Cell", "CellSlot", "cell", "cell_def"]

from collections.abc import Callable
from typing import Any, Protocol, TypeVar

from .batch import notify_change as _notify_change
from .slot import BaseSlot, Slot, _drain_resets, _reset_work, mypyc_attr, slot_stack


C_in = TypeVar("C_in", contravariant=True)
C_ctx = TypeVar("C_ctx", bound=dict)
T = TypeVar("T")


class CellSubscriber[T](Protocol):
    def __call__(self, ctx: dict[Any, Any], value: T, /) -> Any: ...


@mypyc_attr(allow_interpreted_subclasses=True)
class Cell[T]:
    """
    A subscribable that can be used with Slots.
    """

    __slots__ = ("_next_registration", "_parents", "_subscribers", "_value", "ctx")

    # Registration-keyed, insertion-ordered observer table: `token -> callback`.
    # The key is the *registration*, never the callback, so two subscribes of one
    # callable are two independent entries (lazily-spec reactive-graph.md,
    # "Every registration is independent"). A CPython dict preserves insertion
    # order, which is exactly the required firing order.
    _subscribers: dict[int, CellSubscriber[T]] | None
    _next_registration: int
    _parents: set[Slot[Any, Any, Any]] | None
    _value: T
    ctx: dict

    def __init__(self, ctx: dict, initial_value: T) -> None:
        self.ctx = ctx
        self._value = initial_value
        # Lazily materialized on first subscriber/parent: an empty CPython
        # ``dict()`` is ~64 B, so deferring it keeps quiescent leaf sources cheap.
        self._subscribers = None
        # Monotonic per cell and never rewound, so a token is unique for the
        # lifetime of the cell even across a full unsubscribe that releases the
        # table. A recycled token would let a spent disposer name a later
        # registration — the `b2de504` defect, one layer down.
        self._next_registration = 0
        # Auto-discovered parents (Slots/Effects reading this cell), tracked by
        # object identity. Stored separately from `_subscribers` (external
        # callables) because `functools.partial` objects do NOT deduplicate in a
        # set — identity-based parent tracking keeps the fan-out exactly-once.
        self._parents = None

    def __call__(self) -> T:
        return self.value

    @property
    def value(self) -> T:
        if slot_stack:
            # Track the running parent by identity so repeated reads during one
            # computation, and re-reads across reruns, do not grow the fan-out.
            if self._parents is None:
                self._parents = set()
            self._parents.add(slot_stack[-1])
        return self._value

    @value.setter
    def value(self, value: T) -> None:
        _value = self._value
        self._value = value
        if self._value != _value:
            # Coalesce-aware: inside a `batch`, defer the touch to the outermost
            # boundary so multiple writes produce one invalidation wave.
            _notify_change(self)

    def get(self) -> T:
        """Alias for the value property"""
        return self.value

    def set(self, value: T) -> None:
        """Alias for value= property setter"""
        self.value = value

    def subscribe(self, subscriber: CellSubscriber[T]) -> Callable[[], None]:
        """Register an external (non-reactive) change callback.

        External subscribers are called as ``subscriber(ctx, value)`` on
        :meth:`touch`. The auto-discovered reactive parents (Slots/Effects) are
        tracked separately by identity in :attr:`_parents`.

        Returns an idempotent disposer; call it to unsubscribe. Calling it more
        than once is a no-op, and a disposer that has already fired will never
        remove a *later* registration of an equal callable.

        Semantics — the normative observer contract of
        ``lazily-spec/docs/reactive-graph.md``, replayed in
        ``tests/test_reactive_graph_observer_conformance.py``:

        * **Every registration is independent** — the table is keyed by
          registration, not by callback, so the same callable subscribed twice
          is *two* registrations. Both are invoked per :meth:`touch`, and each
          disposer removes exactly one.
        * **Order is registration order** — observers fire in the sequence they
          were registered, stably across notifications. Order is a property of
          the registration, not of the callback: a callback removed and
          re-registered goes to the back.
        * **Subscribing during a notification is deferred** — the pass is
          bounded by the registrations captured before the first callback, so a
          subscriber added mid-pass first runs on the *next* one (and a
          self-feeding observer terminates).
        * **Unsubscribing during a notification takes effect immediately** — a
          registration disposed mid-pass is skipped even when the cursor has not
          reached it. Already-visited observers are unaffected; disposal is not
          retroactive.
        """
        subscribers = self._subscribers
        if subscribers is None:
            subscribers = self._subscribers = {}
        token = self._next_registration
        self._next_registration = token + 1
        subscribers[token] = subscriber

        disposed = False

        def unsubscribe() -> None:
            # Latch first: a spent disposer must never remove anything, which is
            # what keeps it off a later registration of an equal callable even
            # though the table itself is already registration-keyed.
            nonlocal disposed
            if disposed:
                return
            disposed = True
            subs = self._subscribers
            if subs is not None:
                # Popping mid-`touch` is the mechanism for immediate removal:
                # the notify loop re-reads the table per entry, so an unvisited
                # observer removed here is skipped rather than relocated.
                subs.pop(token, None)
                if not subs:
                    # Release the table, matching the lazy-materialization
                    # policy in __init__. `touch` tests truthiness, so an empty
                    # dict and None are already indistinguishable to callers.
                    self._subscribers = None

        return unsubscribe

    def touch(self) -> None:
        # External subscribers persist across touches (they are not reactive
        # edges). Snapshot the registration tokens — that bounds the pass to
        # the observers registered before it began (deferred subscribe) — but
        # re-read each callback from the live table before invoking it, so a
        # disposal from inside a callback takes effect immediately even for an
        # observer the loop has not yet reached. The auto-discovered parents are
        # reactive edges: rebind-then-clear (they re-establish on recompute)
        # and push them into the coalesced invalidation wave — no tuple alloc.
        subs = self._subscribers
        if subs:
            ctx = self.ctx
            value = self._value
            for token in tuple(subs):
                subscriber = subs.get(token)
                if subscriber is not None:
                    subscriber(ctx, value)
        pare = self._parents
        if pare:
            self._parents = None
            ctx = self.ctx
            for parent in pare:
                _reset_work.append((parent, ctx))
            _drain_resets()


def none_callable(_: dict) -> None:
    return None


def _none_as_t(_: dict) -> Any:
    return None


@mypyc_attr(allow_interpreted_subclasses=True)
class CellSlot[C_in, C_ctx: dict, T](BaseSlot[C_in, C_ctx, Cell[T]]):
    __slots__ = ()

    def __init__(
        self,
        callable: Callable[[C_ctx], T] = _none_as_t,
        resolve_ctx: Callable[[C_in], C_ctx] | None = None,
    ) -> None:
        super().__init__(
            callable=lambda ctx: Cell(ctx, callable(ctx)), resolve_ctx=resolve_ctx
        )


def cell[C_ctx: dict, T](
    callable: Callable[[C_ctx], T] = _none_as_t,
) -> CellSlot[C_ctx, C_ctx, T]:
    """
    Decorator for creating a slot that returns a Cell.

    Note: this is intentionally a function (not a class) so type checkers
    correctly treat @cell as transforming the function type from T to Cell[T].
    """
    return CellSlot(callable=callable)


def cell_def[C_in, C_ctx: dict, T](
    resolve_ctx: Callable[[C_in], C_ctx],
) -> Callable[[Callable[[C_ctx], T]], CellSlot[C_in, C_ctx, T]]:
    def outer(callable: Callable[[C_ctx], T]) -> CellSlot[C_in, C_ctx, T]:
        return CellSlot[C_in, C_ctx, T](callable=callable, resolve_ctx=resolve_ctx)

    return outer
