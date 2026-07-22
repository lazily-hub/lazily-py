"""The fortified compute view — value-threaded dependency tracking.

The Python mirror of ``lazily-rs``'s ``Compute`` / ``ComputeOps`` (``#lzcellkernel``;
``lazily-spec/cell-model.md`` § "Dependency tracking (the fortified compute
view)"). Dependency tracking is **value-threaded, not ambient**: the identity of
the node being recomputed — *which* node a read must attribute to — is carried
into the compute function **as a value**, through a per-recompute
:class:`Compute` view, rather than read out of a module-global "current node"
stack.

Why value-threading (normative, per the spec): an ambient carrier is *clobbered
across suspension*. An ``async`` compute that reads a dependency after an
``await`` would attribute it to whatever else ran on the executor. A value
threaded through the closure is *captured*, so it survives suspension. Python
*does* provide a suspension-surviving ambient carrier (``contextvars``), and a
binding MAY use it (the spec permits it) — but the lazily **family choice is
uniform value-threading**, so we thread the value here to match ``lazily-rs`` and
the JS/Zig bindings that have no ambient carrier at all. (A ``contextvars``-based
variant would replace :class:`Compute`'s explicit ``node`` field with a
``ContextVar`` set for the recompute's duration; it is deliberately *not* used.)

Two surfaces implement the same compute-time operation subset
(:class:`ComputeOps` — the Python analog of ``lazily-rs``'s ``ComputeOps``
trait):

* :class:`Compute` — the **tracked** surface handed to a compute/effect closure.
  A read through it registers a dependency edge against *its* node
  (value-threaded). It is the **sole** tracking surface.
* :class:`Context` — the **untracked** surface (the owning context). A read
  through it registers no edge; it is the explicit untracked escape, reached
  from a :class:`Compute` via :meth:`Compute.untracked`.

Fortification (as far as Python allows):

* **Sole tracking surface** — a tracked read is available only through
  :class:`Compute`; :meth:`Compute.untracked` is the one explicit escape.
* **Non-escapable** — Python cannot enforce this by lifetime the way
  ``lazily-rs`` does (``!Send`` + a borrow that cannot outlive the recompute).
  It is enforced instead by **convention plus a runtime guard**: the view is
  *closed* when its recompute ends, and any read on a closed view raises
  :class:`StaleComputeError` — so a view stored and replayed later cannot
  silently register an edge against the wrong (already-finished) node.
* **Edge-attribution invariant** — because the node is a *value field* of the
  view, every edge a recompute registers has that node as its dependent, by
  construction (``lazily-rs`` proves this as
  ``registerReads_dependent_is_recomputing_node``).

The legacy ambient ``slot_stack`` in :mod:`lazily.slot` is **retained as a
narrow compatibility bridge** for the existing ``f(ctx_dict)`` closures that read
cells through the no-argument ``.value`` getter — exactly as ``lazily-rs`` kept
its thread-local frame as a bridge for the ``Fn(&Self)`` reactive-graph closures.
Any closure that reads through a :class:`Compute` (never through ``.value``)
tracks purely by value-threading and pushes nothing onto ``slot_stack``.
"""

from __future__ import annotations


__all__ = [
    "Compute",
    "ComputeEffect",
    "ComputeOps",
    "Context",
    "StaleComputeError",
    "eval_tracked",
    "tracked_effect",
]

from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from .batch import batch as _batch
from .cell import Cell, _none_as_t, source
from .effect import Effect
from .signal import Computed, computed, computed_ripple_when
from .slot import Slot, _detach_from_dependencies, slot_stack
from .teardown import dispose_node


if TYPE_CHECKING:
    from collections.abc import Callable


# Non-native: this must stay catchable across the compiled/interpreted boundary,
# and (like :class:`~lazily.slot.DisposedError`) subclasses a builtin exception.
class StaleComputeError(RuntimeError):
    """Raised when a :class:`Compute` view is read after its recompute ended.

    The view is non-escapable **by contract**; Python cannot forbid storing it
    the way ``lazily-rs``'s lifetime + ``!Send`` bound does, so this runtime
    guard is what stops a stored-and-replayed view from registering a dependency
    edge against a node that is no longer the one being recomputed.
    """


_MISSING: Any = object()


def _register_edge(dep: Any, node: Any) -> None:
    """Register the symmetric dependency edge ``dep -> node`` (value-threaded).

    ``node`` (the recomputing node) becomes a dependent of ``dep``; ``dep`` is
    recorded in ``node``'s forward-edge set when ``node`` keeps one (a
    :class:`~lazily.slot.Slot` / :class:`~lazily.effect.Effect` does, so its
    disposal can find and detach the edge). Both directions are written in one
    place, exactly as the ambient ``slot_stack`` path does — a reverse edge
    without its forward partner is one disposal can never find.

    The attribution target is ``node``, the *value* threaded through the compute
    view — **never** ``slot_stack[-1]``. That is the whole point.
    """
    parents = dep._parents
    if parents is None:
        parents = set()
        dep._parents = parents
    parents.add(node)
    node_deps = getattr(node, "_deps", _MISSING)
    if node_deps is not _MISSING:
        if node_deps is None:
            node_deps = set()
            node._deps = node_deps
        node_deps.add(dep)


def _read_untracked(node: Any, ctx: Any) -> Any:
    """Read ``node``'s value forming **no** dependency edge.

    The ambient ``slot_stack`` is suspended for the duration so that, even if an
    outer legacy recompute has a frame pushed, this read attributes to nothing —
    the read is genuinely untracked, which is what makes
    :meth:`Compute.untracked` an escape rather than a second tracking path.
    """
    saved = list(slot_stack)
    slot_stack.clear()
    try:
        if isinstance(node, (Cell, Computed)):
            return node.get()
        if isinstance(node, Slot):
            return node(ctx)
        if callable(node):
            return node(ctx)
        return node
    finally:
        slot_stack[:] = saved


@runtime_checkable
class ComputeOps(Protocol):
    """The compute-time subset of the context API (``lazily-rs`` ``ComputeOps``).

    Implemented by exactly two types: :class:`Context` (the untracked read
    surface) and :class:`Compute` (the per-recompute tracked view). A handle
    method or callback written against ``ComputeOps`` therefore stays generic
    over "context or compute view", the Python spelling of ``lazily-rs`` keeping
    handle methods generic over ``<C: ComputeOps>``.

    ``read`` is the tracked/untracked value read (``lazily-rs`` ``ComputeOps::get``;
    renamed because Python's ``dict.get`` — the context is a dict — owns ``get``
    for key lookup, so the reactive read cannot reuse that name on the underlying
    context without collision). ``get`` is provided as a thin alias for parity
    with the Rust vocabulary. ``get_rc`` has no Python analog (there is no ``Rc``
    handle to clone) and is intentionally omitted.
    """

    def read(self, node: Any) -> Any: ...
    def get(self, node: Any) -> Any: ...
    def set(self, cell: Any, value: Any) -> None: ...
    def source(self, callable: Any = ...) -> Any: ...
    def computed(self, callable: Any) -> Any: ...
    def computed_ripple_when(self, callable: Any, changed: Any) -> Any: ...
    def slot(self, callable: Any) -> Any: ...
    def effect(self, body: Any) -> Any: ...
    def batch(self, run: Any) -> Any: ...
    def dispose(self, node: Any) -> None: ...
    def untracked(self) -> Any: ...


class Context:
    """The **untracked** compute-time surface — the owning context.

    Wraps the context dict. Every operation is untracked: :meth:`read` forms no
    dependency edge. Reached from a :class:`Compute` via
    :meth:`Compute.untracked`, it is the explicit escape the fortification
    contract requires (a normal read cannot silently *miss* tracking, and an
    untracked read cannot silently *gain* it).
    """

    __slots__ = ("ctx",)

    def __init__(self, ctx: dict) -> None:
        self.ctx = ctx

    def read(self, node: Any) -> Any:
        return _read_untracked(node, self.ctx)

    def get(self, node: Any) -> Any:
        return _read_untracked(node, self.ctx)

    def set(self, cell: Any, value: Any) -> None:
        cell.set(value)

    def source(self, callable: Callable[[Any], Any] = _none_as_t) -> Any:
        return source(callable)(self.ctx)

    def computed(self, callable: Callable[[Any], Any]) -> Any:
        return computed(self.ctx, callable)

    def computed_ripple_when(
        self,
        callable: Callable[[Any], Any],
        changed: Callable[[Any, Any], bool],
    ) -> Any:
        return computed_ripple_when(self.ctx, callable, changed)

    def slot(self, callable: Callable[[Any], Any]) -> Any:
        return Slot(callable=callable)(self.ctx)

    def effect(self, body: Callable[[Compute], Any]) -> ComputeEffect:
        handle = ComputeEffect(body)
        handle(self.ctx)
        return handle

    def batch(self, run: Callable[[], Any]) -> Any:
        return _batch(run)

    def dispose(self, node: Any) -> None:
        dispose_node(node, self.ctx)

    def untracked(self) -> Context:
        return self


class Compute:
    """The **tracked**, fortified compute view for one recompute of ``node``.

    Handed to a compute/effect closure in place of the raw context. A read
    through it (:meth:`read` / :meth:`get`) registers a dependency edge against
    ``node`` — the value threaded into the view — so the closure's dependencies
    are attributed to the correct node **by construction**, with no ambient
    "current node".

    The view is **closed** when its recompute ends (:meth:`_close`); a read on a
    closed view raises :class:`StaleComputeError`. That runtime guard is the
    Python stand-in for ``lazily-rs``'s compile-time non-escapability.
    """

    __slots__ = ("_active", "_ctx", "node")

    def __init__(self, ctx: dict, node: Any) -> None:
        self._ctx = ctx
        self.node = node
        self._active = True

    def _guard(self) -> None:
        if not self._active:
            raise StaleComputeError(
                "read through a Compute view after its recompute ended; the view "
                "is non-escapable by contract (do not store it past the closure)"
            )

    def read(self, node: Any) -> Any:
        """Tracked read: register ``node -> self.node`` and return the value.

        A **lazy** :class:`~lazily.signal.Computed` needs a second edge. A lazy
        computed holds no settled value and never :meth:`~lazily.signal.Computed.touch`\\ es
        on its own — only its *eager* form does, from the puller. Its live
        upstream edges live on the **backing memo** (``_slot``), which is the node
        an upstream change actually invalidates. Registering only ``node ->
        self.node`` (the handle) would leave ``self.node`` subscribed to a node
        that never propagates, so an upstream change would never reach the reader
        (the ``lazy computed`` / ``chained lazy`` MISS the ambient ``.value`` path
        catches by pushing the reader onto ``slot_stack`` through the memo read).
        We therefore also subscribe ``self.node`` to the backing memo, so
        ``upstream -> memo -> self.node`` propagates. Eager computeds and raw
        slots need no second edge — they propagate through their own handle.
        """
        self._guard()
        _register_edge(node, self.node)
        if isinstance(node, Computed) and not node._eager:
            _register_edge(node._slot, self.node)
        return _read_untracked(node, self._ctx)

    def get(self, node: Any) -> Any:
        """Alias for :meth:`read` (``lazily-rs`` ``ComputeOps::get`` spelling)."""
        return self.read(node)

    def set(self, cell: Any, value: Any) -> None:
        self._guard()
        cell.set(value)

    def source(self, callable: Callable[[Any], Any] = _none_as_t) -> Any:
        self._guard()
        return source(callable)(self._ctx)

    def computed(self, callable: Callable[[Any], Any]) -> Any:
        self._guard()
        return computed(self._ctx, callable)

    def computed_ripple_when(
        self,
        callable: Callable[[Any], Any],
        changed: Callable[[Any, Any], bool],
    ) -> Any:
        self._guard()
        return computed_ripple_when(self._ctx, callable, changed)

    def slot(self, callable: Callable[[Any], Any]) -> Any:
        self._guard()
        return Slot(callable=callable)(self._ctx)

    def effect(self, body: Callable[[Compute], Any]) -> ComputeEffect:
        self._guard()
        handle = ComputeEffect(body)
        handle(self._ctx)
        return handle

    def batch(self, run: Callable[[], Any]) -> Any:
        self._guard()
        return _batch(run)

    def dispose(self, node: Any) -> None:
        self._guard()
        dispose_node(node, self._ctx)

    def untracked(self) -> Context:
        """The explicit untracked escape — the owning :class:`Context`."""
        return Context(self._ctx)

    def _close(self) -> None:
        self._active = False


def eval_tracked(ctx: dict, node: Any, fn: Callable[[Compute], Any]) -> Any:
    """Run ``fn`` as a value-threaded recompute of ``node``.

    Mints a fresh :class:`Compute` bound to ``node``, runs ``fn(compute)``, then
    closes the view. Forward edges are **re-bound per recompute**
    (:func:`~lazily.slot._detach_from_dependencies` drops the deps of the
    previous run so a conditional read that took the other branch this time does
    not retain the branch it skipped), matching the dynamic-dependency contract.
    No ``slot_stack`` frame is pushed: tracking is entirely value-threaded.
    """
    _detach_from_dependencies(node)
    view = Compute(ctx, node)
    try:
        return fn(view)
    finally:
        view._close()


class ComputeEffect(Effect):
    """A value-threaded effect: reruns on change, tracks through a :class:`Compute`.

    The fortified counterpart of :class:`~lazily.effect.Effect`. Its body takes a
    :class:`Compute` (not the raw ctx dict) and reads dependencies through it, so
    tracking is value-threaded and **pushes nothing onto ``slot_stack``** — the
    ambient bridge is bypassed entirely on this path. Reuses the base
    :class:`~lazily.effect.Effect` invalidation machinery (``reset`` /
    ``_invalidate`` / the shared work-stack), so a change to a tracked dependency
    reruns the body exactly as for an ordinary effect.
    """

    __slots__ = ()

    def __init__(self, body: Callable[[Compute], Any]) -> None:
        # ``_body`` is declared ``Callable[[dict], ...]`` on the compiled base for
        # the ambient path; a :class:`ComputeEffect` invokes it with a
        # :class:`Compute` instead, so the cast bridges the two closure shapes.
        super().__init__(cast("Callable[[dict], Any]", body))

    def __call__(self, ctx: dict) -> None:
        if self._disposed:
            return
        self._ctx = ctx
        # cleanup-before-body, mirroring Effect.__call__.
        self._run_cleanup()
        self._running = True
        # Rebind forward edges: drop the previous run's deps (and our reverse
        # link in each) so this run's `read`s re-establish an exact edge set.
        _detach_from_dependencies(self)
        view = Compute(ctx, self)
        try:
            self._cleanup = cast("Callable[[Compute], Any]", self._body)(view)
        finally:
            view._close()
            self._running = False


def tracked_effect(body: Callable[[Compute], Any]) -> ComputeEffect:
    """Create a value-threaded :class:`ComputeEffect`.

    ``body(compute) -> cleanup | None`` runs when the returned effect is first
    invoked with a context and reruns whenever a dependency read through the
    :class:`Compute` changes. The value-threaded analog of
    :func:`lazily.effect.effect`.
    """
    return ComputeEffect(body)
