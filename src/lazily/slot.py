__all__ = ["BaseSlot", "Slot", "resolve_identity", "slot", "slot_def", "slot_stack"]

from collections.abc import Callable
from typing import Any, TypeVar, cast

from mypy_extensions import mypyc_attr


C_in = TypeVar("C_in", contravariant=True)
C_ctx = TypeVar("C_ctx", bound=dict)
C_dict = TypeVar("C_dict", bound=dict)
T = TypeVar("T")


def resolve_identity[C_ctx: dict](ctx: C_ctx) -> C_ctx:
    return ctx


def _callable_of(slot_obj: Any) -> Callable[[Any], Any]:
    """Resolve the slot's callable with MRO-aware semantics.

    ``slot_obj`` is deliberately typed ``Any`` so mypyc emits a generic
    (MRO-aware) attribute read instead of a native struct read. This matters
    for interpreted subclasses that override ``callable`` as a *method* without
    assigning the instance attribute (e.g.
    ``class HttpClient(Slot[...]): def callable(self, ctx): ...``): a native
    struct read would miss the method (the slot is unset) and raise
    ``AttributeError``, whereas the generic read finds the method through the
    MRO. The cost is off the hot path — cached reads return before the
    callable is ever touched, and ordinary native slots keep their fast native
    attribute *write* in ``__init__``; only the (cache-miss) read is generic.
    """
    return slot_obj.callable


# ---------------------------------------------------------------------------
# Iterative invalidation engine
#
# The invalidation wave (Cell.touch / Slot.reset / Signal.touch) used to recurse
# one CPython frame per graph level, so a deep cascade could blow the 1000-frame
# stack and a batch performed N separate recursive walks for N changed cells.
# It is now driven by an explicit module-level work-stack:
#
#   * entry points (touch / reset) push the downstream parents onto
#     ``_reset_work`` and call ``_drain_resets``.
#   * ``_drain_resets`` pops nodes, calls ``node._invalidate(ctx)`` (which clears
#     that one node's cache + captures its downstream + re-establishes nothing),
#     and pushes the captured downstream back onto the stack.
#
# Eager ``Signal`` recompute and ``Effect`` reruns fire from inside
# ``_invalidate`` and push their own downstream onto the SAME stack, so a deep
# eager-signal chain no longer nests one CPython frame per level either.
#
# Coalescing: ``Slot._invalidate`` rebinds its downstream edges to None BEFORE
# propagating, so a node reached through several changed sources is only
# non-trivially processed once per wave (later passes find empty edges and do
# no work). The batch flush additionally funnels every changed-cell root through
# ONE drain (``_suspend_drain`` / ``_resume_drain``), and effects are deduped by
# identity in ``enqueue_effect`` — so the "a dependent reached through many
# changed cells in one batch appears at most once" invariant holds.
# ---------------------------------------------------------------------------

_reset_work: list[tuple["Slot[Any, Any, Any]", Any]] = []
_reset_active: bool = False


def _drain_resets() -> None:
    """Process the invalidation work-stack until empty.

    Re-entrant-safe: if a drain is already running (eager recompute / effect
    rerun pushed more work), the nested call returns immediately and the outer
    loop picks up the new items.
    """
    global _reset_active
    if _reset_active:
        return
    _reset_active = True
    try:
        work = _reset_work
        while work:
            node, node_ctx = work.pop()
            node._invalidate(node_ctx)
    finally:
        _reset_active = False


def _suspend_drain() -> None:
    """Mark a drain as active so pushes accumulate without being consumed.

    Used by the batch flush so all changed-cell roots push their downstream
    into ONE coalesced wave, drained once by :func:`_resume_drain`.
    """
    global _reset_active
    _reset_active = True


def _resume_drain() -> None:
    """End a coalesced-push region and drain the accumulated wave once."""
    global _reset_active
    _reset_active = False
    _drain_resets()


@mypyc_attr(allow_interpreted_subclasses=True)
class BaseSlot[C_in, C_ctx: dict, T]:
    """
    Base class for a lazy slot Callable. Wraps a callable implementation field.
    Does not track Cell dependencies.
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
        resolve = self.resolve_ctx
        if resolve is resolve_identity:
            resolved: C_ctx = ctx  # type: ignore[assignment]
        else:
            resolved = resolve(ctx)
        if self in resolved:
            return resolved[self]
        resolved[self] = _callable_of(self)(resolved)
        return resolved[self]

    def __repr__(self) -> str:
        return f"<Slot {_callable_of(self)}>"

    def get(self, ctx: C_in) -> T | None:
        resolve = self.resolve_ctx
        if resolve is resolve_identity:
            resolved: C_ctx = ctx  # type: ignore[assignment]
        else:
            resolved = resolve(ctx)
        return resolved.get(self)

    def reset(self, ctx: C_in) -> None:
        resolve = self.resolve_ctx
        if resolve is resolve_identity:
            resolved: C_ctx = ctx  # type: ignore[assignment]
        else:
            resolved = resolve(ctx)
        resolved.pop(self, None)

    def is_in(self, ctx: C_in) -> bool:
        resolve = self.resolve_ctx
        if resolve is resolve_identity:
            resolved: C_ctx = ctx  # type: ignore[assignment]
        else:
            resolved = resolve(ctx)
        return self in resolved


@mypyc_attr(allow_interpreted_subclasses=True)
class Slot[C_in, C_ctx: dict, T](BaseSlot[C_in, C_ctx, T]):
    """Base class for a lazy slot Callable that tracks Cell dependencies.

    Like every reactive in this library, a Slot exposes **no observer API**.
    See :class:`~lazily.cell.Cell` for the rationale.
    """

    __slots__ = ("_parents",)

    _parents: "set[Slot[Any, Any, Any]] | None"

    def __init__(
        self,
        callable: Callable[[C_ctx], T] | None = None,
        resolve_ctx: Callable[[C_in], C_ctx] | None = None,
    ) -> None:
        super().__init__(callable=callable, resolve_ctx=resolve_ctx)
        # Auto-discovered parents (Slots/Effects reading this slot), tracked by
        # object identity so repeated reads during one computation do not grow
        # the fan-out. ``functools.partial`` objects do NOT deduplicate in a set,
        # so identity-based parent tracking replaces per-read ``partial``
        # allocation (which previously leaked without bound).
        self._parents = None

    def __call__(self, ctx: C_in) -> T:
        if slot_stack:
            if self._parents is None:
                self._parents = set()
            self._parents.add(slot_stack[-1])

        resolve = self.resolve_ctx
        if resolve is resolve_identity:
            resolved: C_ctx = ctx  # type: ignore[assignment]
        else:
            resolved = resolve(ctx)

        if self in resolved:
            return resolved[self]

        try:
            slot_stack.append(self)
            resolved[self] = _callable_of(self)(resolved)
        finally:
            slot_stack.pop()

        return resolved[self]

    def reset(self, ctx: C_in) -> None:
        # Push self onto the iterative work-stack; the drain clears the cache,
        # snapshots + clears the downstream edges, and propagates to parents in
        # one coalesced wave (see ``_drain_resets``).
        _reset_work.append((self, ctx))
        _drain_resets()

    def _invalidate(self, ctx: Any) -> None:
        # Clear THIS node's cache + capture its downstream edges. Re-entrancy
        # safe: the edges are rebound to None BEFORE propagating, so a parent
        # that re-enters reset finds an empty set. The wave's visited guard
        # (``_drain_resets``) makes a second pass a no-op.
        resolve = self.resolve_ctx
        if resolve is resolve_identity:
            resolved = ctx
        else:
            resolved = resolve(ctx)
        resolved.pop(self, None)
        pare = self._parents
        self._parents = None
        if pare:
            for parent in pare:
                _reset_work.append((parent, resolved))

    def touch(self, ctx: C_ctx) -> None:
        # The auto-discovered parents are the only fan-out: they are reactive
        # edges, so rebind-then-clear (they re-establish on recompute) and push
        # them into the coalesced invalidation wave — no tuple alloc.
        pare = self._parents
        if pare:
            self._parents = None
            for parent in pare:
                _reset_work.append((parent, ctx))
            _drain_resets()


slot_stack: list[Slot[Any, Any, Any]] = []


class slot[C_dict: dict, T](Slot[C_dict, C_dict, T]):
    """
    A Slot that can be initialized with the callable as an argument.
    """

    __slots__ = ()

    def __init__(self, callable: Callable[[C_dict], T]) -> None:
        super().__init__(callable=callable)


def slot_def[C_in, C_ctx: dict, T](
    resolve_ctx: Callable[[C_in], C_ctx],
) -> Callable[[Callable[[C_ctx], T]], Slot[C_in, C_ctx, T]]:
    def outer(callable: Callable[[C_ctx], T]) -> Slot[C_in, C_ctx, T]:
        return Slot[C_in, C_ctx, T](callable=callable, resolve_ctx=resolve_ctx)

    return outer
