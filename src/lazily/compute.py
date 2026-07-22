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

:class:`Compute` is the **value-threaded** tracking surface, and it doubles as a
*dict-proxying* context view (carrying the recomputing node as ``node`` plus the
stable ``underlying`` dict), so an existing ``def f(ctx): ...ctx-as-dict...`` body
keeps working unchanged while a ``ctx.read(node)`` (and the ``name(ctx).value``
:class:`~lazily.slot.BoundHandle` path) attributes edges by value.

``#lzcellkernel`` residual — the ambient ``slot_stack`` bridge in
:mod:`lazily.slot` is **retained**, not deleted. Full removal is blocked by
pervasive bare-read call sites (``obj.value`` / ``obj.get()`` / ``obj.method()``
reads *inside* reactive bodies, in both the test suite and the feature modules)
that a value-threaded ``ctx``-carried surface cannot reach without rewriting each
call site. Every recompute driver therefore still pushes its node onto
``slot_stack`` for the duration of its body, and a bare read attributes to
``slot_stack[-1]``; an explicit ``Compute.read`` suspends the bridge
(:func:`_read_untracked`) so a value-threaded read never double-attributes. The
value-threaded machinery here is the migration target: once each bare-read site
is moved onto ``ctx.read`` / ``name(ctx).value``, the bridge can be dropped. The
thread-safe / async engines keep their own scoping and are untouched.
"""

from __future__ import annotations


__all__ = [
    "BoundHandle",
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
from .slot import Slot, _detach_from_dependencies, _register_edge, slot_stack
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


def _subscribe_reader(node: Any, reader: Any) -> None:
    """Register the recomputing ``reader`` as a dependent of ``node``.

    Delegates to ``node._subscribe`` when the node kind defines it (Cell /
    Computed / Slot — a lazy Computed additionally subscribes the reader to its
    backing memo), else falls back to a direct edge. A non-node value (a bare
    callable or literal read through the view) subscribes nothing.
    """
    sub = getattr(node, "_subscribe", None)
    if sub is not None:
        sub(reader)
    elif hasattr(node, "_parents"):
        _register_edge(node, reader)


class BoundHandle:
    """A reactive value handle bound to the reader that obtained it.

    Returned by ``name(ctx)`` when ``ctx`` is a compute view and the slot yields a
    reactive value node (a :class:`~lazily.cell.Cell` / a
    :class:`~lazily.signal.Computed`). It carries the recomputing node so that the
    subsequent ``.value`` / ``.get()`` / ``()`` read registers the dependency edge
    against that node — value-threaded, no ambient stack. Any other attribute or
    method is forwarded to the wrapped target unchanged, so the handle is
    transparent for ``.set`` / ``.value =`` writes (which do not track) and for
    ``.eager`` / ``.merge`` / ``.dispose`` / etc.

    Kept in this (interpreted) module because it needs a property setter and
    ``__getattr__``; :func:`lazily.slot._bound_handle` constructs it lazily.
    """

    __slots__ = ("_reader", "_target")

    def __init__(self, target: Any, reader: Any) -> None:
        self._target = target
        self._reader = reader

    @property
    def value(self) -> Any:
        self._target._subscribe(self._reader)
        return self._target.value

    @value.setter
    def value(self, v: Any) -> None:
        self._target.value = v

    def get(self) -> Any:
        self._target._subscribe(self._reader)
        return self._target.value

    def set(self, v: Any) -> None:
        self._target.set(v)

    def __call__(self) -> Any:
        self._target._subscribe(self._reader)
        return self._target.value

    def __getattr__(self, name: str) -> Any:
        # Only reached when normal lookup misses (not one of the tracked accessors
        # above and not a __slots__ member); forward to the wrapped target.
        return getattr(self._target, name)

    def __repr__(self) -> str:
        return f"<BoundHandle {self._target!r}>"


def _read_untracked(node: Any, ctx: Any) -> Any:
    """Read ``node``'s value forming **no** dependency edge.

    A value-threaded ``Compute.read`` has already registered the edge explicitly;
    the actual value read must not *also* attribute through the ambient bridge, so
    ``slot_stack`` is suspended for the duration. ``node(ctx)`` then recomputes a
    slot against the underlying dict with no ambient reader, so its own reads
    attribute to it (via the view its recompute mints), never to the caller.
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
    renamed because the surface **proxies the context dict**, so ``get`` is the
    dict's ``get(key)`` — the reactive read cannot reuse that name without
    collision). ``get_rc`` has no Python analog (there is no ``Rc`` handle to
    clone) and is intentionally omitted.
    """

    def read(self, node: Any) -> Any: ...
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

    @property
    def underlying(self) -> dict:
        """The stable underlying context dict."""
        return self.ctx

    def read(self, node: Any) -> Any:
        return _read_untracked(node, self.ctx)

    def set(self, cell: Any, value: Any) -> None:
        cell.set(value)

    # -- dict proxy: a body may treat the untracked surface as its ctx dict ---- #
    def __getitem__(self, key: Any) -> Any:
        return self.ctx[key]

    def __setitem__(self, key: Any, value: Any) -> None:
        self.ctx[key] = value

    def __delitem__(self, key: Any) -> None:
        del self.ctx[key]

    def __contains__(self, key: Any) -> bool:
        return key in self.ctx

    def __iter__(self) -> Any:
        return iter(self.ctx)

    def __len__(self) -> int:
        return len(self.ctx)

    def get(self, key: Any, default: Any = None) -> Any:
        return self.ctx.get(key, default)

    def keys(self) -> Any:
        return self.ctx.keys()

    def values(self) -> Any:
        return self.ctx.values()

    def items(self) -> Any:
        return self.ctx.items()

    def pop(self, key: Any, *default: Any) -> Any:
        return self.ctx.pop(key, *default)

    def setdefault(self, key: Any, default: Any = None) -> Any:
        return self.ctx.setdefault(key, default)

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

    @property
    def underlying(self) -> dict:
        """The stable underlying context dict.

        A cell/slot CONSTRUCTED inside this recompute must capture *this* dict,
        not the transient view — the view is retired when the recompute returns
        (``#lzcellkernel`` item 3). The construction paths read it via
        :func:`lazily.slot._ctx_base`.
        """
        return self._ctx

    def _guard(self) -> None:
        if not self._active:
            raise StaleComputeError(
                "read through a Compute view after its recompute ended; the view "
                "is non-escapable by contract (do not store it past the closure)"
            )

    def read(self, node: Any) -> Any:
        """Tracked read: subscribe ``self.node`` to ``node`` and return the value.

        Subscription is delegated to ``node._subscribe`` (the same path a
        :class:`~lazily.slot.BoundHandle` uses for ``name(ctx).value``), so every
        node kind subscribes correctly — in particular a **lazy**
        :class:`~lazily.signal.Computed` subscribes the reader to its backing memo
        as well (a lazy computed holds no settled value and never
        :meth:`~lazily.signal.Computed.touch`\\ es on its own; its live upstream
        edges are on the memo, the node an upstream change actually invalidates).
        Eager computeds and raw slots need only the direct edge — they propagate
        through their own handle.
        """
        self._guard()
        _subscribe_reader(node, self.node)
        return _read_untracked(node, self._ctx)

    # -- dict proxy: existing ``def f(ctx): ...ctx-as-dict...`` bodies keep ----- #
    # working unchanged, but reads *through the reactive surface* are tracked.
    def __getitem__(self, key: Any) -> Any:
        self._guard()
        return self._ctx[key]

    def __setitem__(self, key: Any, value: Any) -> None:
        self._guard()
        self._ctx[key] = value

    def __delitem__(self, key: Any) -> None:
        self._guard()
        del self._ctx[key]

    def __contains__(self, key: Any) -> bool:
        return key in self._ctx

    def __iter__(self) -> Any:
        return iter(self._ctx)

    def __len__(self) -> int:
        return len(self._ctx)

    def get(self, key: Any, default: Any = None) -> Any:
        """Dict ``get(key)`` on the proxied context (not a reactive read; use
        :meth:`read` to read a node)."""
        return self._ctx.get(key, default)

    def keys(self) -> Any:
        return self._ctx.keys()

    def values(self) -> Any:
        return self._ctx.values()

    def items(self) -> Any:
        return self._ctx.items()

    def pop(self, key: Any, *default: Any) -> Any:
        return self._ctx.pop(key, *default)

    def setdefault(self, key: Any, default: Any = None) -> Any:
        return self._ctx.setdefault(key, default)

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
    Tracking is entirely value-threaded; there is no ambient frame.
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
    :class:`Compute` and reads dependencies through it, so tracking is
    value-threaded. Since the base :class:`~lazily.effect.Effect` now *also* mints
    a :class:`Compute` for its body (the ambient stack is gone), the two paths
    coincide; this subclass is kept for the explicit ``tracked_effect`` /
    ``Compute``-typed-body API. Reuses the base invalidation machinery (``reset`` /
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
