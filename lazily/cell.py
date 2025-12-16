from typing import Any, Callable, Generic, Protocol, TypeVar

from .slot import BaseSlot, Slot, slot_stack


__all__ = ["Cell", "cell", "cell_def"]

C_in = TypeVar("C_in", contravariant=True)
C_ctx = TypeVar("C_ctx", bound=dict)
T = TypeVar("T")


class CellSubscriber[T](Protocol):
    def __call__(self, ctx: dict, value: T) -> Any: ...


class Cell(Generic[T]):
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
            self.subscribe(lambda ctx, value: callable.reset(self.ctx))
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


def none_callable(ctx: dict) -> None:
    return None


def _none_as_t(ctx: dict) -> Any:
    return None


class cell(BaseSlot[dict, dict, Cell[T]]):
    """
    Decorator for creating a slot that returns a Cell.
    """

    def __init__(self, callable: Callable[[dict], T] | None = None) -> None:
        if callable is None:
            callable = _none_as_t
        super().__init__(callable=lambda ctx: Cell(ctx, callable(ctx)))


def cell_def(
    resolve_ctx: Callable[[C_in], dict],
) -> Callable[[Callable[[dict], T]], Slot[C_in, dict, Cell[T]]]:
    def outer(callable: Callable[[dict], T]) -> Slot[C_in, dict, Cell[T]]:
        return Slot[C_in, dict, Cell[T]](
            callable=lambda ctx: Cell(ctx, callable(ctx)),
            resolve_ctx=resolve_ctx,
        )

    return outer
