__all__ = ["Cell", "CellSlot", "cell", "cell_def"]

from collections.abc import Callable
from typing import Any, Protocol, TypeVar

from .batch import notify_change as _notify_change
from .slot import BaseSlot, Slot, slot_stack


C_in = TypeVar("C_in", contravariant=True)
C_ctx = TypeVar("C_ctx", bound=dict)
T = TypeVar("T")


class CellSubscriber[T](Protocol):
    def __call__(self, ctx: dict[Any, Any], value: T, /) -> Any: ...


class Cell[T]:
    """
    A subscribable that can be used with Slots.
    """

    __slots__ = ("_parents", "_subscribers", "_value", "ctx", "name")

    _subscribers: set[CellSubscriber[T]]
    _parents: set[Slot[Any, Any, Any]]

    def __init__(self, ctx: dict, initial_value: T) -> None:
        self.ctx = ctx
        self._value = initial_value
        self._subscribers = set()
        # Auto-discovered parents (Slots/Effects reading this cell), tracked by
        # object identity. Stored separately from `_subscribers` (external
        # callables) because `functools.partial` objects do NOT deduplicate in a
        # set — identity-based parent tracking keeps the fan-out exactly-once.
        self._parents = set()

    def __call__(self) -> T:
        return self.value

    @property
    def value(self) -> T:
        if len(slot_stack) > 0:
            # Track the running parent by identity so repeated reads during one
            # computation, and re-reads across reruns, do not grow the fan-out.
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
        self._subscribers.add(subscriber)

    def touch(self) -> None:
        # Iterate snapshots: a parent/subscriber may re-subscribe (re-establish
        # a dependency) while being notified.
        for subscriber in tuple(self._subscribers):
            subscriber(self.ctx, self._value)
        for parent in tuple(self._parents):
            parent.reset(self.ctx)


def none_callable(_: dict) -> None:
    return None


def _none_as_t(_: dict) -> Any:
    return None


class CellSlot[C_in, C_ctx: dict, T](BaseSlot[C_in, C_ctx, Cell[T]]):
    __slots__ = [
        "_subscribers",
    ]

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
