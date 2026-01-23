from collections.abc import Callable
from typing import Any, Protocol, TypeVar, cast


__all__ = ["BaseSlot", "Slot", "resolve_identity", "slot", "slot_def", "slot_stack"]

C_in = TypeVar("C_in", contravariant=True)
C_ctx = TypeVar("C_ctx", bound=dict)
C_dict = TypeVar("C_dict", bound=dict)
T = TypeVar("T")


def resolve_identity[C_ctx: dict](ctx: C_ctx) -> C_ctx:
    return ctx


class SlotSubscriber(Protocol):
    def __call__(self, slot: "Slot[Any, Any, Any]", ctx: dict) -> Any: ...


class BaseSlot[C_in, C_ctx: dict, T]:
    """
    Base class for a lazy slot Callable. Wraps a callable implementation field.
    Does not subscribe to Cells.
    """

    __slots__ = ("callable", "resolve_ctx")

    callable: Callable[[C_ctx], T]
    resolve_ctx: Callable[[C_in], C_ctx]

    def __init__(
        self,
        callable: Callable[[C_ctx], T] | None = None,
        resolve_ctx: Callable[[C_in], C_ctx] | None = None,
    ) -> None:
        if callable is not None:
            self.callable = callable
        self.resolve_ctx = (
            resolve_ctx
            if resolve_ctx is not None
            else cast("Callable[[C_in], C_ctx]", resolve_identity)
        )

    def __call__(self, ctx: C_in) -> T:
        resolved = self.resolve_ctx(ctx)
        if self in resolved:
            return resolved[self]
        resolved[self] = self.callable(resolved)
        return resolved[self]

    def __repr__(self) -> str:
        return f"<Slot {self.callable.__name__}>"

    def get(self, ctx: C_in) -> T | None:
        resolved = self.resolve_ctx(ctx)
        return resolved.get(self)

    def reset(self, ctx: C_in) -> None:
        resolved = self.resolve_ctx(ctx)
        resolved.pop(self, None)

    def is_in(self, ctx: C_in) -> bool:
        resolved = self.resolve_ctx(ctx)
        return self in resolved


class Slot[C_in, C_ctx: dict, T](BaseSlot[C_in, C_ctx, T]):
    """
    Base class for a lazy slot Callable that subscribes to Cells.
    """

    __slots__ = ("_subscribers",)

    _subscribers: set[SlotSubscriber]

    def __init__(
        self,
        callable: Callable[[C_ctx], T] | None = None,
        resolve_ctx: Callable[[C_in], C_ctx] | None = None,
    ) -> None:
        super().__init__(callable=callable, resolve_ctx=resolve_ctx)
        self._subscribers = set()

    def __call__(self, ctx: C_in) -> T:
        if len(slot_stack) > 0:
            parent_slot = slot_stack[-1]
            self.subscribe(
                lambda _, resolved_ctx: parent_slot.reset(resolved_ctx)
            )

        resolved = self.resolve_ctx(ctx)

        if self in resolved:
            return resolved[self]

        try:
            slot_stack.append(self)
            resolved[self] = self.callable(resolved)
            self.touch(resolved)
        finally:
            slot_stack.pop()

        return resolved[self]

    def reset(self, ctx: C_in) -> None:
        resolved = self.resolve_ctx(ctx)
        super().reset(ctx)
        self.touch(resolved)
        self._subscribers.clear()

    def subscribe(self, subscriber: SlotSubscriber) -> None:
        self._subscribers.add(subscriber)

    def touch(self, ctx: C_ctx) -> None:
        for subscriber in self._subscribers:
            subscriber(self, ctx)


slot_stack: list[Slot[Any, Any, Any]] = []


class slot[C_dict: dict, T](Slot[C_dict, C_dict, T]):
    """
    A Slot that can be initialized with the callable as an argument.
    """

    def __init__(self, callable: Callable[[C_dict], T]) -> None:
        super().__init__(callable=callable)


def slot_def[C_in, C_ctx: dict, T](
    resolve_ctx: Callable[[C_in], C_ctx],
) -> Callable[[Callable[[C_ctx], T]], Slot[C_in, C_ctx, T]]:
    def outer(callable: Callable[[C_ctx], T]) -> Slot[C_in, C_ctx, T]:
        return Slot[C_in, C_ctx, T](callable=callable, resolve_ctx=resolve_ctx)

    return outer
