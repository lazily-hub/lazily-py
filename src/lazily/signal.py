"""Eager derived value — the ``Signal`` member of the Slot → Cell → Signal family.

Where a :class:`~lazily.slot.Slot` is **lazy** (invalidation only marks it dirty;
the value is recomputed on the next read), a :class:`Signal` is **eager**: it
computes its value once at creation and recomputes immediately whenever a tracked
dependency changes. A :class:`Signal` is composed from existing primitives — a
memoized :class:`~lazily.slot.Slot` plus a puller that re-pulls the slot on
invalidation — and applies a PartialEq/memo guard so an eager recompute that
yields an equal value suppresses downstream cascades.

This mirrors ``ctx.signal()`` in the Rust reference (`lazily-rs`) and the eager
Signal wire representation in ``lazily-spec``: on the wire a Signal is just the
ordinary backing slot node that stores its materialized value (no separate wire
type). The puller here is local execution state and is never serialized.
"""

from __future__ import annotations


__all__ = ["Signal", "signal", "signal_def"]

from typing import TYPE_CHECKING, Any, TypeVar

from .slot import Slot, _drain_resets, _reset_work, mypyc_attr, slot, slot_stack


if TYPE_CHECKING:
    from collections.abc import Callable


C_in = TypeVar("C_in", contravariant=True)
C_ctx = TypeVar("C_ctx", bound=dict)
T = TypeVar("T")


class _SignalSlot[C_in, C_ctx: dict, T](Slot[C_in, C_ctx, T]):
    """Backing memoized slot whose ``reset`` eagerly re-pulls its owning Signal."""

    __slots__ = ("_signal",)

    _signal: Signal[T] | None

    def __init__(
        self,
        callable: Callable[[C_ctx], T],
        resolve_ctx: Callable[[C_in], C_ctx] | None = None,
    ) -> None:
        super().__init__(callable=callable, resolve_ctx=resolve_ctx)
        self._signal = None

    def reset(self, ctx: C_in) -> None:
        super().reset(ctx)

    def _invalidate(self, ctx: Any) -> None:
        super()._invalidate(ctx)
        sig = self._signal
        if sig is not None and sig.is_active():
            sig._eager_recompute()


@mypyc_attr(allow_interpreted_subclasses=True)
class Signal[T]:
    """An eager derived value bound to a single context.

    The value is materialized at construction and kept fresh: when an upstream
    Cell or Slot it read changes, the Signal recomputes right away rather than
    waiting for the next read. Reading :attr:`value` inside a Slot/Signal
    computation registers a dependency, so downstream reactives invalidate when
    this Signal's value changes.
    """

    __slots__ = (
        "_active",
        "_parents",
        "_recomputing",
        "_slot",
        "_subscribers",
        "_value",
        "ctx",
    )

    _subscribers: set[Callable[[dict, T], Any]] | None
    _parents: set[Slot[Any, Any, Any]] | None
    _active: bool
    _recomputing: bool
    _slot: _SignalSlot[dict, dict, T]
    _value: T
    ctx: dict

    def __init__(self, ctx: dict, callable: Callable[[dict], T]) -> None:
        self.ctx = ctx
        # Lazily materialized on first subscriber/parent: an empty CPython
        # ``set()`` is ~216 B, so deferring it keeps quiescent signals cheap.
        self._subscribers = None
        self._parents = None
        self._active = True
        self._recomputing = False
        self._slot = _SignalSlot(callable)
        self._slot._signal = self
        # Eager activation: compute once now so there is no intermediate unset
        # value, and so dependency edges are established immediately.
        self._value = self._slot(ctx)

    def _eager_recompute(self) -> None:
        if not self._active or self._recomputing:
            return
        self._recomputing = True
        try:
            new_value = self._slot(self.ctx)
        finally:
            self._recomputing = False
        # Memo / PartialEq guard: an equal recompute suppresses the cascade.
        if new_value != self._value:
            self._value = new_value
            self.touch()

    @property
    def value(self) -> T:
        """The current materialized value; auto-subscribes the reading slot."""
        if slot_stack:
            # Identity-based parent tracking (mirrors Cell/Slot): avoids a
            # per-read ``functools.partial`` allocation that does not deduplicate
            # in a set and would otherwise grow without bound.
            if self._parents is None:
                self._parents = set()
            self._parents.add(slot_stack[-1])
        if not self._active:
            # Disposed: the eager puller is gone, so behave lazily and recompute
            # on read via the backing slot.
            return self._slot(self.ctx)
        return self._value

    def __call__(self) -> T:
        return self.value

    def get(self) -> T:
        """Alias for the :attr:`value` getter."""
        return self.value

    def subscribe(self, subscriber: Callable[[dict, T], Any]) -> Callable[[], None]:
        """Register an external (non-reactive) change callback.

        External subscribers are called as ``subscriber(ctx, value)`` on
        :meth:`touch` — including the :meth:`_eager_recompute` that follows a
        dependency change, subject to the memo guard. The auto-discovered
        reactive parents are tracked separately by identity in :attr:`_parents`.

        Returns an idempotent disposer; call it to unsubscribe. Calling it more
        than once is a no-op, and a disposer that has already fired will never
        remove a *later* registration of an equal callable.

        Semantics match :meth:`lazily.Cell.subscribe` exactly (see
        ``tests/test_signal_observer.py``):

        * **Dedup** — storage is a ``set``, so registration is by equality: the
          same callable subscribed twice is one registration, invoked once per
          :meth:`touch` and removed by a single disposal.
        * **Order is unspecified** — a ``set`` does not preserve registration
          order. Do not depend on the dispatch sequence.
        * **Snapshot dispatch** — :meth:`touch` iterates a snapshot, so a
          subscriber added during a notification first runs on the *next* one,
          and one removed mid-notification still runs in the current pass.
        """
        subscribers = self._subscribers
        if subscribers is None:
            subscribers = self._subscribers = set()
        subscribers.add(subscriber)

        disposed = False

        def unsubscribe() -> None:
            # The `disposed` latch is what makes this safe to call twice AND
            # safe to hold past a re-subscribe: a bare `discard` would silently
            # remove a later registration of an equal callable.
            nonlocal disposed
            if disposed:
                return
            disposed = True
            subs = self._subscribers
            if subs is not None:
                subs.discard(subscriber)
                if not subs:
                    # Release the ~216 B set, matching the lazy-materialization
                    # policy in __init__. `touch` tests truthiness, so an empty
                    # set and None are already indistinguishable to callers.
                    self._subscribers = None

        return unsubscribe

    def touch(self) -> None:
        # External subscribers persist across touches (they are not reactive
        # edges), so iterate a snapshot. The auto-discovered parents are
        # reactive edges: rebind-then-clear (they re-establish on recompute)
        # and push them into the coalesced invalidation wave — no tuple alloc.
        subs = self._subscribers
        if subs:
            for subscriber in tuple(subs):
                subscriber(self.ctx, self._value)
        pare = self._parents
        if pare:
            self._parents = None
            ctx = self.ctx
            for parent in pare:
                _reset_work.append((parent, ctx))
            _drain_resets()

    def is_active(self) -> bool:
        """Whether the eager puller is still installed."""
        return self._active

    def dispose(self) -> None:
        """Remove the eager puller.

        The value remains readable but reverts to lazy behavior: it will only be
        recomputed on the next explicit read of the backing slot, not eagerly.
        """
        self._active = False
        self._slot._signal = None


def signal[T](callable: Callable[[dict], T]) -> Slot[dict, dict, Signal[T]]:
    """Decorator: turn a context function into an eager-Signal factory.

    The returned factory is itself context-cached (one Signal per context), so
    ``my_signal(ctx)`` returns the same eager Signal on repeated calls::

        @signal
        def doubled(ctx: dict) -> int:
            return n(ctx).value * 2


        s = doubled(ctx)  # eager: computed now
        s.value  # always current
    """
    return slot(lambda ctx: Signal(ctx, callable))


def signal_def[C_in, T](
    resolve_ctx: Callable[[C_in], dict],
) -> Callable[[Callable[[dict], T]], Slot[C_in, dict, Signal[T]]]:
    """Decorator factory: like :func:`signal`, with a custom context resolver."""

    def outer(callable: Callable[[dict], T]) -> Slot[C_in, dict, Signal[T]]:
        return Slot(
            callable=lambda ctx: Signal(ctx, callable),
            resolve_ctx=resolve_ctx,
        )

    return outer
