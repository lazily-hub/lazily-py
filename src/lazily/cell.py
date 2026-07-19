__all__ = ["Cell", "CellSlot", "cell", "cell_def"]

from collections.abc import Callable
from typing import Any, TypeVar

from .batch import notify_change as _notify_change
from .slot import BaseSlot, Slot, _drain_resets, _reset_work, mypyc_attr, slot_stack


C_in = TypeVar("C_in", contravariant=True)
C_ctx = TypeVar("C_ctx", bound=dict)
T = TypeVar("T")


@mypyc_attr(allow_interpreted_subclasses=True)
class Cell[T]:
    """A reactive leaf source: a mutable value other reactives depend on.

    A Cell has **no observer API**. Observation in this graph is a declared
    dependency edge, not a registered callback: read the cell from a
    :class:`~lazily.slot.Slot`, :class:`~lazily.signal.Signal`, or
    :class:`~lazily.effect.Effect` and that reader becomes a dependent, which is
    what makes batching and glitch-freedom hold. Where a caller genuinely needs
    a stream of *every* transition rather than the settled value, that is a
    :class:`~lazily.queue.Topic`.
    """

    __slots__ = ("_parents", "_value", "ctx")

    _parents: set[Slot[Any, Any, Any]] | None
    _value: T
    ctx: dict

    def __init__(self, ctx: dict, initial_value: T) -> None:
        self.ctx = ctx
        self._value = initial_value
        # Auto-discovered parents (Slots/Signals/Effects reading this cell),
        # tracked by object identity so repeated reads never grow the fan-out.
        # Lazily materialized: an empty CPython ``set()`` is ~216 B, so
        # deferring it keeps quiescent leaf sources cheap.
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
