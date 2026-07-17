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

    __slots__ = ("_parents", "_subscribers", "_value", "ctx")

    _subscribers: set[CellSubscriber[T]] | None
    _parents: set[Slot[Any, Any, Any]] | None
    _value: T
    ctx: dict

    def __init__(self, ctx: dict, initial_value: T) -> None:
        self.ctx = ctx
        self._value = initial_value
        # Lazily materialized on first subscriber/parent: an empty CPython
        # ``set()`` is ~216 B, so deferring it keeps quiescent leaf sources cheap.
        self._subscribers = None
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

    def subscribe(self, subscriber: CellSubscriber[T]) -> None:
        """Register an external (non-reactive) change callback.

        External subscribers are called as ``subscriber(ctx, value)`` on
        :meth:`touch`. The auto-discovered reactive parents (Slots/Effects) are
        tracked separately by identity in :attr:`_parents`.
        """
        if self._subscribers is None:
            self._subscribers = set()
        self._subscribers.add(subscriber)

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
