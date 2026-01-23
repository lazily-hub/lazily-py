from collections.abc import Callable
from typing import Any, Protocol, TypeVar

from .slot import BaseSlot, slot_stack


__all__ = ["Cell", "CellSlot", "cell", "cell_def"]

C_in = TypeVar("C_in", contravariant=True)
C_ctx = TypeVar("C_ctx", bound=dict)
T = TypeVar("T")


class CellSubscriber[T](Protocol):
    def __call__(self, ctx: dict, value: T) -> Any: ...


class Cell[T]:
    """
    A subscribable that can be used with Slots.
    """

    __slots__ = ("_subscribers", "_value", "ctx", "name")

    _subscribers: set[CellSubscriber[T]]

    def __init__(self, ctx: dict, initial_value: T) -> None:
        self.ctx = ctx
        self._value = initial_value
        self._subscribers = set()

    def __call__(self) -> T:
        return self.value

    @property
    def value(self) -> T:
        if len(slot_stack) > 0:
            callable = slot_stack[-1]
            self.subscribe(lambda _ctx, _value: callable.reset(self.ctx))
        return self._value

    @value.setter
    def value(self, value: T) -> None:
        _value = self._value
        self._value = value
        if self._value != _value:
            self.touch()

    def get(self) -> T:
        """Alias for the value property"""
        return self.value

    def set(self, value: T) -> None:
        """Alias for value= property setter"""
        self.value = value

    def subscribe(self, subscriber: CellSubscriber[T]) -> None:
        self._subscribers.add(subscriber)

    def touch(self) -> None:
        for subscriber in self._subscribers:
            subscriber(self.ctx, self._value)


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
        super().__init__(callable=lambda ctx: Cell(ctx, callable(ctx)), resolve_ctx=resolve_ctx)


def cell[C_ctx:dict, T](callable: Callable[[C_ctx], T] = _none_as_t) -> CellSlot[C_ctx, C_ctx, T]:
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
        return CellSlot[C_in, C_ctx, T](
            callable=callable,
            resolve_ctx=resolve_ctx
        )

    return outer
