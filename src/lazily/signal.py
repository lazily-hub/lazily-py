"""Eager derived value — the ``Signal`` member of the Slot → Cell → Signal family.

Where a :class:`~lazily.slot.Slot` is **lazy** (invalidation only marks it dirty;
the value is recomputed on the next read), a :class:`Signal` is **eager**: it
computes its value once at creation and recomputes immediately whenever a tracked
dependency changes. A :class:`Signal` is composed from existing primitives — a
memoized :class:`~lazily.slot.Slot` plus a puller :class:`~lazily.effect.Effect`
that re-pulls the slot when it is invalidated — and applies a PartialEq/memo
guard so an eager recompute that yields an equal value suppresses downstream
cascades. The puller is an Effect rather than an invalidation hook so that it is
*scheduled*: N writes inside one ``batch`` re-materialize the Signal once, at the
flush, not once per write.

This mirrors ``ctx.signal()`` in the Rust reference (`lazily-rs`) and the eager
Signal wire representation in ``lazily-spec``: on the wire a Signal is just the
ordinary backing slot node that stores its materialized value (no separate wire
type). The puller here is local execution state and is never serialized.
"""

from __future__ import annotations


__all__ = ["Signal", "signal", "signal_def"]

from typing import TYPE_CHECKING, Any, TypeVar

from .effect import Effect
from .slot import Slot, _drain_resets, _reset_work, mypyc_attr, slot, slot_stack


if TYPE_CHECKING:
    from collections.abc import Callable


C_in = TypeVar("C_in", contravariant=True)
C_ctx = TypeVar("C_ctx", bound=dict)
T = TypeVar("T")

# First-materialization sentinel: ``None`` is a legal signal value, so the
# initial pull is distinguished by identity rather than by comparing to None.
_UNSET: Any = object()


@mypyc_attr(allow_interpreted_subclasses=True)
class Signal[T]:
    """An eager derived value bound to a single context.

    The value is materialized at construction and kept fresh: when an upstream
    Cell or Slot it read changes, the Signal recomputes right away rather than
    waiting for the next read. Reading :attr:`value` inside a Slot/Signal
    computation registers a dependency, so downstream reactives invalidate when
    this Signal's value changes.

    Like every reactive in this library, a Signal exposes **no observer API**.
    See :class:`~lazily.cell.Cell` for the rationale.
    """

    __slots__ = (
        "_active",
        "_parents",
        "_puller",
        "_slot",
        "_value",
        "ctx",
    )

    _parents: set[Slot[Any, Any, Any]] | None
    _active: bool
    _puller: Effect
    _slot: Slot[dict, dict, T]
    _value: T
    ctx: dict

    def __init__(self, ctx: dict, callable: Callable[[dict], T]) -> None:
        self.ctx = ctx
        # Lazily materialized on first parent: an empty CPython ``set()`` is
        # ~216 B, so deferring it keeps quiescent signals cheap.
        self._parents = None
        self._active = True
        self._value = _UNSET
        self._slot = Slot(callable=callable)
        # The eager puller is an ordinary Effect over the backing memo — the
        # composition ``reactive-graph.md`` § "Signal eagerness" recommends.
        # Running it now materializes the value once (clause 1) and establishes
        # the dependency edges. Because the puller is an Effect it obeys
        # "effects are scheduled, not inline": N writes inside one ``batch``
        # coalesce into ONE re-materialization at the flush (clause 3). Pulling
        # from the backing slot's ``_invalidate`` instead — which is what this
        # class used to do — recomputes during invalidation, which is *earlier*
        # than the flush, and costs one compute per changed source.
        self._puller = Effect(self._pull)
        self._puller(ctx)

    def _pull(self, ctx: dict) -> None:
        """Puller-Effect body: re-materialize the backing memo into ``_value``."""
        new_value = self._slot(ctx)
        if self._value is _UNSET:
            self._value = new_value
            return None
        # Memo / PartialEq guard: an equal recompute suppresses the cascade.
        if new_value != self._value:
            self._value = new_value
            self.touch()
        return None

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

    def touch(self) -> None:
        # The auto-discovered parents are the only fan-out: they are reactive
        # edges, so rebind-then-clear (they re-establish on recompute) and push
        # them into the coalesced invalidation wave — no tuple alloc.
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

        Only the puller is disposed. The backing memo is untouched, so the value
        stays readable, stays correct (it reverts to recompute-on-read), and no
        longer re-materializes on write — ``reactive-graph.md`` clause 4.
        """
        self._active = False
        self._puller.dispose()


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
