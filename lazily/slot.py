from typing import Any, Callable, Generic, Optional, Protocol, TypeVar

from .types import LazilyCallable, ResolveCallable


__all__ = ["BaseSlot", "Slot", "resolve_identity", "slot", "slot_def", "slot_stack"]


C = TypeVar("C")
T = TypeVar("T")


def resolve_identity(ctx: C) -> dict:
    return ctx


class SlotSubscriber[T](Protocol):
    def __call__[**P](self, slot: "Slot", ctx: dict) -> Any: ...


class BaseSlot(Generic[C, T]):
    """
    Base class for a lazy slot Callable. Wraps a callable implementation field.
    Does not subscribe to Cells.
    """

    __slots__ = "callable"

    callable: LazilyCallable[C, T]

    def __init__(self, callable: Optional[LazilyCallable[dict, T]] = None) -> None:
        if callable is not None:
            self.callable = callable

    def __call__(self, ctx: C) -> T:
        if self in ctx:
            return ctx[self]
        else:
            ctx[self] = self.callable(ctx)
            return ctx[self]

    def __repr__(self) -> str:
        return f"<Slot {self.callable.__name__}>"

    def get(self, ctx: C) -> Optional[T]:
        return ctx.get(self)

    def reset(self, ctx: C) -> None:
        ctx.pop(self, None)

    def is_in(self, ctx: C) -> bool:
        return self in ctx


class Slot(BaseSlot[dict, T], Generic[C, T]):
    """
    Base class for a lazy slot Callable that subscribes to Cells.
    """

    __slots__ = "_subscribers", "resolve_ctx"

    callable: LazilyCallable[dict, T]

    def __init__(
        self,
        callable: Optional[LazilyCallable[dict, T]] = None,
        resolve_ctx: ResolveCallable[C] = resolve_identity,
    ) -> None:
        super().__init__(callable)
        self._subscribers = set()
        self.resolve_ctx = resolve_ctx

    def __call__(self, ctx: C) -> T:
        if len(slot_stack) > 0:
            parent_slot = slot_stack[-1]
            self.subscribe(lambda self, ctx: parent_slot.reset(ctx))

        ctx = self.resolve_ctx(ctx)
        if self in ctx:
            return ctx[self]
        else:
            try:
                slot_stack.append(self)
                ctx[self] = self.callable(ctx)
                self.touch(ctx)
            finally:
                slot_stack.pop()
            return ctx[self]

    def reset(self, ctx: C) -> None:
        ctx = self.resolve_ctx(ctx)
        super().reset(ctx)
        self.touch(ctx)
        self._subscribers.clear()

    def subscribe(self, subscriber: SlotSubscriber[T]) -> None:
        self._subscribers.add(subscriber)

    def touch(self, ctx: C) -> None:
        ctx = self.resolve_ctx(ctx)
        for subscriber in self._subscribers:
            subscriber(self, ctx)


slot_stack: list[Slot] = []


class slot(Slot[dict, T]):
    """
    A Slot that can be initialized with the callable as an argument.

    Usage:
    ```
    from lazily import slot


    @slot
    def hello(ctx: dict) -> str:
        return "Hello"


    @slot
    def world(ctx: dict) -> str:
        return "World"


    @slot
    def greeting(ctx: dict) -> str:
        print("Calculating greeting...")
        return f"{hello(ctx)} {world(ctx)}!"


    @slot
    def response(ctx: dict) -> str:
        return "How are you?"


    @slot
    def greeting_and_response(ctx: dict) -> str:
        print("Calculating greeting_and_response...")
        return f"{greeting(ctx)} {response(ctx)}"


    ctx = {}

    greeting(ctx)
    # Calculating greeting...
    # Hello World!

    greeting_and_response(ctx)
    # Calculating greeting and response...
    # Hello World! How are you?

    greeting(ctx)
    # Hello World!

    greeting_and_response(ctx)
    # Hello World! How are you?
    ```
    """

    def __init__(self, callable: LazilyCallable[dict, T]) -> None:
        super().__init__()
        self.callable = callable


def slot_def(
    resolve_ctx: Callable[[C], dict],
) -> Callable[[Callable[[dict], T]], Slot[C, T]]:
    """
    Defines a slot with a resolve_ctx argument extract a context from the argument.
    This is useful with http request libraries or graphql libraries.

    Usage:
    ```
    from lazily import slot_def


    @dataclass
    class CustomCtxResolver:
        ctx: dict


    def resolve_ctx(resolver: CustomCtxResolver | dict) -> dict:
        return resolver.ctx if isinstance(resolver, CustomCtxResolver) else resolver


    @slot_def(resolve_ctx)
    def hello(ctx: dict) -> str:
        return "Hello"


    @slot_def(resolve_ctx)
    def world(ctx: dict) -> str:
        return "World"


    @slot_def(resolve_ctx)
    def greeting(ctx: dict) -> str:
        print("Calculating greeting...")
        return f"{hello(ctx)} {world(ctx)}!"


    @slot_def(resolve_ctx)
    def response(ctx: dict) -> str:
        return "How are you?"


    @slot_def(resolve_ctx)
    def greeting_and_response(ctx: dict) -> str:
        print("Calculating greeting_and_response...")
        return f"{greeting(ctx)} {response(ctx)}"


    ctx = {}
    resolver = CustomCtxResolver(ctx)

    greeting(resolver)
    # Calculating greeting...
    # Hello World!

    greeting_and_response(ctx)
    # Calculating greeting and response...
    # Hello World! How are you?

    greeting(resolver)
    # Hello World!

    greeting_and_response(ctx)
    # Hello World! How are you?
    ```
    """

    def outer(callable: Callable[[dict], T]) -> Slot[C, T]:
        return Slot[C, T](callable, resolve_ctx)

    return outer
