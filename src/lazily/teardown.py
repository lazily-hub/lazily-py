"""Teardown scopes — grouped disposal for the synchronous reactive graph.

The Python counterpart of ``lazily-rs``'s ``Context::scope() -> TeardownScope``
and of ``lazily-spec/conformance/reactive-graph`` (``#lzspecedgeindex``).

**Why a context manager.** ``lazily-rs`` ties a scope's lifetime to a value's
``Drop``; Python has no such guarantee, and ``__del__`` is explicitly not one.
The construct Python already uses for "this block owns these resources, release
them on the way out" is ``with``, so that is the spelling here::

    with teardown_scope(ctx) as conn:
        topic = conn.cell(0)
        doubled = conn.computed(lambda c: topic.value * 2)
        conn.effect(lambda c: print(doubled(c)))
    # every node above is disposed here, in reverse creation order

The imperative form the fixtures use — a scope opened and closed by separate
ops — is the same object without the ``with``: :meth:`TeardownScope.close`.
``__exit__`` is a thin wrapper over it rather than a second code path.

**What a scope owns.** Only nodes *created through it*. Grouping bounds
teardown, not visibility: a scoped node reads parent-owned and sibling-owned
nodes freely, and a node outside the scope may read one inside it (and will
raise :class:`~lazily.slot.DisposedError` on its next recompute once the scope
ends — teardown is not reference counting).

**Reverse creation order.** Graph state after a teardown is order-independent,
so the ordering is not about the edges: effect *cleanups* are side effects, and
running a dependent's cleanup after the thing it read has already gone would
observe a half-torn-down graph. Disposing dependents first means a scope never
transiently dangles inside itself. ``lazily-formal``'s
``disposeScope_eq_disposeAll`` is the proof that this is observationally the
fold of the individual disposals.
"""

from __future__ import annotations


__all__ = ["TeardownScope", "dispose_node", "teardown_scope"]

import warnings
from typing import TYPE_CHECKING, Any

from .cell import Cell
from .effect import Effect
from .slot import Slot


if TYPE_CHECKING:
    from collections.abc import Callable


def dispose_node(node: Any, ctx: dict) -> None:
    """Dispose whatever kind of reactive ``node`` is.

    The kind is read off the node rather than remembered by the caller, so a
    scope stores one reference per member and no tag. Mirrors ``lazily-rs``'s
    ``Context::dispose_id``.
    """
    if isinstance(node, Effect):
        # Already holds the context it ran against.
        node.dispose()
    elif isinstance(node, Cell):
        node.dispose()
    else:
        node.dispose(ctx)


class TeardownScope:
    """A group of reactive nodes disposed together. See the module docstring."""

    __slots__ = ("_armed", "_owned", "ctx")

    def __init__(self, ctx: dict) -> None:
        self.ctx = ctx
        self._owned: list[Any] = []
        self._armed = True

    # -- membership ------------------------------------------------------ #

    def source[T](self, initial_value: T) -> Cell[T]:
        """Create a source cell owned by this scope."""
        return self.adopt(Cell(self.ctx, initial_value))

    def cell[T](self, initial_value: T) -> Cell[T]:
        """Deprecated v1 alias for :meth:`source`."""
        warnings.warn(
            "cell() is deprecated; use source() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.adopt(Cell(self.ctx, initial_value))

    def computed[T](self, callable: Callable[[dict], T]) -> Slot[dict, dict, T]:
        """Create a lazily-computed storage slot owned by this scope."""
        return self.adopt(Slot(callable))

    def effect(self, body: Callable[[dict], Any | None]) -> Effect:
        """Register an effect owned by this scope and run its body once.

        The immediate first run matches :func:`lazily.effect.effect` followed by
        invoking the handle: an effect that has never run has tracked nothing
        and would observe no publish.
        """
        handle = self.adopt(Effect(body))
        handle(self.ctx)
        return handle

    def adopt[N](self, node: N) -> N:
        """Take ownership of an existing node.

        The escape hatch for a node built by a factory the scope does not wrap.
        Ownership is not exclusive — adopting a node into two scopes means the
        second teardown finds it already disposed, which is a no-op.
        """
        self._owned.append(node)
        return node

    # -- lifetime -------------------------------------------------------- #

    def __len__(self) -> int:
        """How many nodes this scope currently owns."""
        return len(self._owned)

    @property
    def armed(self) -> bool:
        """Whether closing this scope will dispose its members."""
        return self._armed

    def disarm(self) -> None:
        """Cancel this scope's teardown; closing it afterwards disposes nothing.

        The nodes are untouched — they simply revert to being owned by nothing,
        the state every unscoped node is already in. The same sense as defusing
        a scope guard, and the reason a scope is not a reference count: disarming
        cannot resurrect anything and cannot leak anything that was not already
        reachable.
        """
        self._armed = False
        self._owned = []

    def close(self) -> None:
        """Dispose every member, in reverse creation order. Idempotent."""
        if not self._armed:
            return
        owned = self._owned
        self._owned = []
        ctx = self.ctx
        for node in reversed(owned):
            dispose_node(node, ctx)

    def __enter__(self) -> TeardownScope:
        return self

    def __exit__(self, *_exc: Any) -> bool:
        self.close()
        return False


def teardown_scope(ctx: dict) -> TeardownScope:
    """Open a teardown scope over ``ctx``. See :class:`TeardownScope`."""
    return TeardownScope(ctx)
