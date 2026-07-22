__all__ = [
    "BaseSlot",
    "DisposedError",
    "Slot",
    "resolve_identity",
    "slot",
    "slot_def",
    "slot_stack",
]

import warnings
from collections.abc import Callable
from typing import Any, TypeVar, cast

from mypy_extensions import mypyc_attr


C_in = TypeVar("C_in", contravariant=True)
C_ctx = TypeVar("C_ctx", bound=dict)
C_dict = TypeVar("C_dict", bound=dict)
T = TypeVar("T")


# Non-native: mypyc cannot compile a subclass of a builtin exception type, and
# this must stay catchable across the compiled/interpreted boundary.
@mypyc_attr(native_class=False)
class DisposedError(RuntimeError):
    """Raised when a disposed reactive node is read.

    Disposal is terminal and its contract is *errors on next recompute*
    (``lazily-spec/conformance/reactive-graph/read_after_dispose_is_an_error``):
    the node itself raises, and a surviving dependent that still names it raises
    when its own recompute reaches the read. A live dependent must therefore be
    *dirtied* by the disposal — see :func:`_dirty_disposed_dependents` — or it
    would keep serving its cached value forever and never reach the error.
    """


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
# Eager ``Computed`` recompute and ``Effect`` reruns fire from inside
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


def _dirty_disposed_dependents(roots: "set[Slot[Any, Any, Any]]", ctx: Any) -> None:
    """Dirty the cone that survives a disposal — and schedule nothing.

    Two invariants, both learned by regression (``lazily-rs`` 5db90d2,
    ``lazily-js`` 4d20670), live in this one function:

    1. **Detaching edges is not enough.** A dependent that still holds a cached
       value computed from the disposed node would keep serving it forever: the
       edge that would have invalidated it is exactly the edge disposal just
       removed. So every surviving transitive dependent has its cached value
       dropped here, which is what makes ``read_after_dispose`` reachable from a
       live reader rather than only from the disposed handle itself.

    2. **Effects reached by this walk must not be scheduled.** Disposal is not a
       publish. Running an effect during teardown re-enters a compute that reads
       the node being torn down, so teardown would stop being idempotent and the
       error would surface *inside* ``dispose`` instead of on the reader's next
       recompute. Effects are therefore only unlinked from their own dependents
       here; they stay subscribed and error on their next real rerun.

    Iterative, sharing the shape of :func:`_drain_resets` but deliberately not
    its work-stack: this walk must never reach :meth:`Effect._invalidate`.
    """
    stack: list[Slot[Any, Any, Any]] = list(roots)
    while stack:
        node = stack.pop()
        if node._disposed:
            continue
        pare = node._parents
        # Rebind before descending so a diamond is processed once and a cycle
        # terminates: the second visit finds an empty edge set.
        node._parents = None
        node._drop_cached(ctx)
        if pare:
            stack.extend(pare)


def _detach_from_dependencies(node: Any) -> None:
    """Remove ``node`` from the reverse edge set of everything it reads.

    This is the half of disposal the churn fixture measures: without it a
    source's dependent set grows by one per subscribe and never shrinks, so a
    workload with a constant live subscriber count still degrades without bound
    in both memory and propagation cost (``#lzspecedgeindex``).
    """
    deps = node._deps
    node._deps = None
    if not deps:
        return
    for dependency in deps:
        parents = dependency._parents
        if parents is not None:
            parents.discard(node)


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

    __slots__ = ("_deps", "_disposed", "_parents")

    _parents: "set[Slot[Any, Any, Any]] | None"
    _deps: "set[Any] | None"
    _disposed: bool

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
        # The forward direction: what this node read during its current run.
        # Lazily materialized for the same reason ``_parents`` is. Disposal
        # needs it — without a forward edge set there is no way to remove this
        # node from each dependency's ``_parents`` short of scanning the whole
        # graph, and leaving those edges behind is exactly the unbounded growth
        # ``churn_returns_to_baseline`` measures.
        self._deps = None
        self._disposed = False

    @property
    def disposed(self) -> bool:
        """Whether :meth:`dispose` has been called (terminal)."""
        return self._disposed

    def dependent_count(self) -> int:
        """How many nodes currently depend on this one — reverse edge degree.

        A count, never the collection: the edge sets are internal invalidation
        state, and handing them out would let a caller mutate the graph or hold
        a node alive. This is the observable ``#lzspecedgeindex`` is written
        against.
        """
        pare = self._parents
        return 0 if pare is None else len(pare)

    def dependency_count(self) -> int:
        """How many nodes this one currently reads — forward edge degree."""
        deps = self._deps
        return 0 if deps is None else len(deps)

    def dispose(self, ctx: C_in) -> None:
        """Tear down this slot: detach both edge directions, drop its cache, and
        dirty whatever still reads it. Terminal and idempotent.

        Takes the context for the same reason :meth:`__call__` and :meth:`reset`
        do — a plain :class:`Slot` is context-free by design, and its cached
        value lives in the caller's context mapping rather than on the object.

        Nothing stops a live reader from still naming this slot; that reader
        raises :class:`DisposedError` on its next recompute, which the dirtying
        below is what makes reachable.
        """
        if self._disposed:
            return
        self._disposed = True
        resolve = self.resolve_ctx
        if resolve is resolve_identity:
            resolved: Any = ctx
        else:
            resolved = resolve(ctx)
        resolved.pop(self, None)
        _detach_from_dependencies(self)
        pare = self._parents
        self._parents = None
        if pare:
            _dirty_disposed_dependents(pare, resolved)

    def _drop_cached(self, ctx: Any) -> None:
        """Drop this node's cached value during a disposal walk.

        Deliberately *not* :meth:`_invalidate`: that entry point reruns effects
        and re-enters the shared work-stack, and a disposal must schedule
        nothing (see :func:`_dirty_disposed_dependents`).
        """
        resolve = self.resolve_ctx
        if resolve is resolve_identity:
            resolved: Any = ctx
        else:
            resolved = resolve(ctx)
        resolved.pop(self, None)

    def __call__(self, ctx: C_in) -> T:
        if self._disposed:
            raise DisposedError(f"read of disposed slot {self!r}")
        if slot_stack:
            reader = slot_stack[-1]
            if self._parents is None:
                self._parents = set()
            self._parents.add(reader)
            # Symmetric forward edge. Both directions are recorded in the same
            # branch so they cannot drift: a reverse edge without its forward
            # partner is an edge disposal can never find.
            if reader._deps is None:
                reader._deps = set()
            reader._deps.add(self)

        resolve = self.resolve_ctx
        if resolve is resolve_identity:
            resolved: C_ctx = ctx  # type: ignore[assignment]
        else:
            resolved = resolve(ctx)

        if self in resolved:
            return resolved[self]

        try:
            slot_stack.append(self)
            # Forward edges describe the *current* run, so they are dropped
            # before the body and rebuilt by its reads. Mirrors the reverse
            # direction, which ``_invalidate`` rebinds for the same reason.
            self._deps = None
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
        if self._disposed:
            # A disposed node has no cache and no edges; it is also not a route
            # the wave may propagate through.
            return
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
    """Deprecated v1 alias — a :class:`Slot` initialized with the callable.

    ``slot`` was the v1 constructor for a derived reactive value. In v2 every
    derived cell is **guarded**: use :func:`~lazily.signal.computed` for a
    guarded derived value. :class:`Slot` itself remains as the internal,
    storage-sense primitive (the Python analog of ``lazily-rs``'s surviving
    storage-sense ``Slot``); construct it directly as ``Slot(callable=...)``
    when a raw storage node is genuinely needed.
    """

    __slots__ = ()

    def __init__(self, callable: Callable[[C_dict], T]) -> None:
        warnings.warn(
            "slot() is deprecated; use computed() for a guarded derived value "
            "(or Slot(callable=...) for a raw storage node)",
            DeprecationWarning,
            stacklevel=2,
        )
        super().__init__(callable=callable)


def slot_def[C_in, C_ctx: dict, T](
    resolve_ctx: Callable[[C_in], C_ctx],
) -> Callable[[Callable[[C_ctx], T]], Slot[C_in, C_ctx, T]]:
    """Decorator factory for a context-cached, storage-sense :class:`Slot` with a
    custom context resolver.

    The resolver variant of the storage-sense :class:`Slot` primitive (the Python
    analog of ``lazily-rs``'s surviving storage-sense ``Slot``). For a *guarded*
    derived value use :func:`~lazily.signal.computed_def` instead.
    """

    def outer(callable: Callable[[C_ctx], T]) -> Slot[C_in, C_ctx, T]:
        return Slot[C_in, C_ctx, T](callable=callable, resolve_ctx=resolve_ctx)

    return outer
