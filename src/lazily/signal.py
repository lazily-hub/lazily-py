"""The ``Computed`` cell — a derived reactive value, and its eager form.

Part of the Cell kernel (``#lzcellkernel``, see
``lazily-spec/docs/reactive-graph.md`` and
``tasks/software/lazily-cell-kernel-design.md``). The kernel has two value
kinds:

``Source`` (:class:`~lazily.cell.Cell`) — value comes from *outside* (``set`` /
``merge``); :class:`Computed` — value comes from *upstream*, via a compute
function. ``Effect`` stays outside the hierarchy (a sink, no value).

A :class:`Computed` is **lazy by default** and **guarded**: invalidation only
marks it dirty and the value is recomputed on the next read, and an equal
recompute suppresses the downstream cascade (the ``PartialEq`` equality guard,
matching TC39 ``Signal.Computed``). Calling :meth:`~Computed.eager` makes it
**eager** — it materializes now and recomputes immediately whenever a tracked
dependency changes. **Every cell is guarded; there is no unguarded derived
mode** — ``computed`` *is* the guarded derived constructor (the former ``memo``
constructor is retired because ``computed`` now folds that role in).

The eager construction is ``computed(ctx, f).eager()``: eagerness is graph state
(an ``_eager`` bit plus an ``_eager_by`` side table holding the puller), not a
distinct node kind. The puller is an ordinary :class:`~lazily.effect.Effect`
over the backing memo, so it is *scheduled*: N writes inside one ``batch``
re-materialize the computed **once**, at the flush, not once per write
(``reactive-graph.md`` clause 3). Because the only way to make a computed eager
is to attach a scheduled ``Effect``, the ``#lzsignaleager`` per-write puller — an
``onInvalidate`` hook that recomputes during the invalidation wave — is
structurally unrepresentable here.

This module keeps its ``signal.py`` filename (it is on the mypyc compile list in
the Makefile) though its subject is now the ``Computed`` cell.

On the wire an eager computed is just the ordinary backing memo node that stores
its materialized value (no separate wire type). The puller is local execution
state and is never serialized.
"""

from __future__ import annotations


__all__ = [
    "Computed",
    "computed",
    "computed_def",
    "computed_ripple_when",
]

from typing import TYPE_CHECKING, Any, TypeVar

from .effect import Effect
from .slot import (
    _AMBIENT_DISABLED,
    Slot,
    _ctx_base,
    _drain_resets,
    _register_edge,
    _reset_work,
    mypyc_attr,
    slot_stack,
)


if TYPE_CHECKING:
    from collections.abc import Callable


C_in = TypeVar("C_in", contravariant=True)
C_ctx = TypeVar("C_ctx", bound=dict)
T = TypeVar("T")

# First-materialization sentinel: ``None`` is a legal computed value, so the
# initial pull is distinguished by identity rather than by comparing to None.
_UNSET: Any = object()

# ``eager_by`` side table (``reactive-graph.md`` §9.3.3): the ``_eager`` bit on
# the Computed answers "am I eager?" for free (making ``eager()`` idempotent with
# no lookup); this table holds "which effect drives me" for exactly the rare
# eager computeds, and nothing for lazy ones. Owner-keyed by the Computed object.
# Python object identity is stable for the object's lifetime and a disposed
# computed is not recycled onto a live one (unlike Rust's ``SlotId``), so the
# generation-tag hazard of §9.3.5 does not arise; the strong reference is
# released on ``lazy()`` / ``dispose`` so a torn-down computed is collectable.
_eager_by: dict[Computed[Any], Effect] = {}


@mypyc_attr(allow_interpreted_subclasses=True)
class Computed[T]:
    """A derived reactive value bound to a single context — lazy by default.

    The value is computed from upstream. Reading :attr:`value` inside a
    Slot/Computed/Effect computation registers a dependency, so downstream
    reactives invalidate when this value changes.

    **Lazy** (the default): the value is recomputed on the next read after an
    upstream change. **Eager** (after :meth:`eager`): the value is materialized
    now and re-materialized eagerly — through a scheduled puller Effect — on
    every upstream change, with an equality guard that suppresses downstream
    cascades on an equal recompute.

    Like every reactive in this library, a Computed exposes **no observer API**.
    See :class:`~lazily.cell.Cell` for the rationale. Where a caller needs a
    stream of *every* transition rather than the settled value, that is a
    :class:`~lazily.queue.Topic`.
    """

    __slots__ = (
        "_changed",
        "_eager",
        "_parents",
        "_puller",
        "_slot",
        "_value",
        "ctx",
    )

    _parents: set[Slot[Any, Any, Any]] | None
    _eager: bool
    _puller: Effect | None
    _slot: Slot[dict, dict, T]
    _value: T
    # Propagate predicate ``changed(old, new)`` -> ``True`` to propagate, ``False``
    # to suppress. ``None`` means the default natural-equality guard (propagate iff
    # ``old != new``), so ``computed(f)`` and ``computed_ripple_when(f, !=)``
    # take the same path. MUST be pure in ``(old, new)`` — see
    # :func:`computed_ripple_when`.
    _changed: Callable[[T, T], bool] | None
    ctx: dict

    def __init__(
        self,
        ctx: Any,
        callable: Callable[[dict], T],
        changed: Callable[[T, T], bool] | None = None,
    ) -> None:
        # ``ctx`` may be a per-recompute :class:`~lazily.compute.Compute` view
        # (a computed constructed inside a compute body); capture the stable
        # underlying dict, never the transient wrapper (``#lzcellkernel`` item 3).
        self.ctx = _ctx_base(ctx)
        # Lazily materialized on first parent: an empty CPython ``set()`` is
        # ~216 B, so deferring it keeps quiescent computeds cheap.
        self._parents = None
        self._eager = False
        self._puller = None
        self._value = _UNSET
        self._changed = changed
        self._slot = Slot(callable=callable)

    # -- eager / lazy: the eager construction (§9.3.1) ----------------------

    def eager(self) -> Computed[T]:
        """Make this computed **eager**, and return **this same** computed.

        Idempotent — a second ``eager`` is a no-op — so ``c.eager().eager()``
        never attaches two pullers (which would double the eager compute, the
        ``#lzsignaleager`` cost class from the other direction). Attaches a
        scheduled puller :class:`~lazily.effect.Effect` over the backing memo and
        records it in the ``_eager_by`` side table, then materializes the value
        once (clause 1) and establishes the dependency edges. Because the puller
        is an Effect it obeys *effects are scheduled, not inline*: N writes inside
        one ``batch`` coalesce into ONE re-materialization at the flush (clause
        3).

        Returns the same handle with graph state mutated — ``g = c.eager()``
        gives ``g is c``, both eager; it is not builder-style ``with(...)``.
        """
        if self._eager:
            return self
        self._eager = True
        puller = Effect(self._pull)
        self._puller = puller
        _eager_by[self] = puller
        puller(self.ctx)
        return self

    def lazy(self) -> None:
        """Reverse of :meth:`eager`: stop eager recomputation, dispose the puller.

        The value remains readable and reverts to lazy (recomputed on the next
        read of the backing memo). No-op if the computed is not eager. Clears the
        ``_eager`` bit and the ``_eager_by`` entry (``reactive-graph.md`` clause
        4), so no puller is stranded.
        """
        if not self._eager:
            return
        self._eager = False
        puller = _eager_by.pop(self, None)
        self._puller = None
        if puller is not None:
            puller.dispose()

    def is_eager(self) -> bool:
        """Whether this computed is currently eager (has an active puller)."""
        return self._eager

    def _pull(self, ctx: Any) -> None:
        """Puller-Effect body: re-materialize the backing memo into ``_value``.

        ``ctx`` is the puller Effect's per-recompute :class:`~lazily.compute.Compute`
        view (not a raw dict); reading the backing memo through it subscribes the
        puller to the memo, so an upstream change reruns the puller.

        Propagation is gated by :attr:`_changed`: the default (``None``) is the
        natural-equality guard (propagate iff ``old != new``); a custom predicate
        (from :func:`computed_ripple_when`) propagates iff ``changed(old,
        new)`` is ``True``. A **suppressed** recompute leaves ``_value`` on the
        last *propagated* value (it is not overwritten), so the stored ``old`` for
        the next comparison is always the last value that actually propagated —
        matching the Rust ``slot_with_equals`` engine.
        """
        new_value = self._slot(ctx)
        if self._value is _UNSET:
            self._value = new_value
            return None
        changed = self._changed
        if changed is None:
            # PartialEq guard: an equal recompute suppresses the cascade.
            propagate = new_value != self._value
        else:
            # Custom propagate guard: ``changed(old, new)`` gates the cascade.
            propagate = changed(self._value, new_value)
        if propagate:
            self._value = new_value
            self.touch()
        return None

    def _subscribe(self, reader: Any) -> None:
        """Register the recomputing ``reader`` as a dependent of this computed.

        Called by a :class:`~lazily.slot.BoundHandle` on a tracked read
        (``name(ctx).value``). A **lazy** computed holds no settled value and
        never :meth:`touch`\\ es on its own — its live upstream edges are on the
        backing memo (``_slot``), the node an upstream change actually
        invalidates. So the reader is subscribed to the memo as well, or an
        upstream change would never reach it. An eager computed propagates through
        its own handle and needs only the direct edge.
        """
        _register_edge(self, reader)
        if not self._eager:
            _register_edge(self._slot, reader)

    @property
    def value(self) -> T:
        """The current value.

        Eager: returns the materialized value. Lazy: recomputes through the
        backing memo on read. A value-threaded read subscribes via
        :meth:`_subscribe` (see ``BoundHandle`` / ``Compute.read``); a **bare**
        read still tracks through the ambient bridge (``slot_stack[-1]``,
        ``#lzcellkernel`` residual).
        """
        if slot_stack and not _AMBIENT_DISABLED:
            self._subscribe(slot_stack[-1])
        if not self._eager:
            # Lazy: recompute on read via the backing memo. The memo pushes itself
            # onto the ambient bridge, so its own reads attribute to it.
            return self._slot(self.ctx)
        return self._value

    def __call__(self) -> T:
        return self.value

    def get(self) -> T:
        """Alias for the :attr:`value` getter."""
        return self.value

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

    def dispose(self) -> None:
        """Tear down the eager puller (if any); value reverts to lazy.

        Disposing an eager computed tears down its puller
        (``reactive-graph.md`` clause 4); the backing memo is untouched, so the
        value stays readable, stays correct, and no longer re-materializes on
        write.
        """
        self.lazy()


def computed[T](ctx: Any, callable: Callable[[dict], T]) -> Computed[T]:
    """Create a lazy, guarded :class:`Computed` bound to ``ctx``.

    The canonical derived-value constructor of the Cell kernel and **guarded by
    default** — an equal recompute suppresses the downstream cascade (matching
    TC39 ``Signal.Computed``). Every cell is guarded; there is no unguarded
    derived mode. Call :meth:`~Computed.eager` for the eager form::

        n = source(lambda c: 1)
        doubled = computed(ctx, lambda c: n(c).value * 2).eager()
        doubled.value  # 4, kept fresh eagerly

    Note the ``(ctx, callable)`` signature: lazily-py uses a context-as-dict
    model (there is no ``Context`` object with a ``.computed`` method), so the
    Rust reference's ``ctx.computed(f)`` becomes ``computed(ctx, f)`` here.
    """
    return Computed(ctx, callable)


def computed_ripple_when[T](
    ctx: Any,
    callable: Callable[[dict], T],
    changed: Callable[[T, T], bool],
) -> Computed[T]:
    """Create a **guarded** :class:`Computed` with an explicit change predicate.

    Like :func:`computed`, but downstream propagation is gated by ``changed(old,
    new)`` instead of the value's natural equality: ``changed`` returns ``True``
    to **propagate** the recompute downstream and ``False`` to **suppress** it
    (treat it as "no meaningful change"). So the two identities hold::

        computed(ctx, f)  # natural-equality guard
        computed_ripple_when(ctx, f, lambda o, n: o != n)  # ...same thing

        # pass-through: always propagate (no suppression), the escape for a
        # value with no cheap/meaningful equality
        computed_ripple_when(ctx, f, lambda o, n: True)

    Use it for a **custom significance policy** — dedup a large value by a
    version/hash field, epsilon float compare, hysteresis, a monotonic gate, or
    "propagate every N" when the counter lives in the value.

    The value is **always computed** (the predicate needs ``new``); ``changed``
    gates only the downstream cascade, not the computation — it is a *propagate*
    guard, not a *compute* guard. A suppressed recompute leaves the exposed value
    on the last value that actually propagated (mirroring the Rust
    ``slot_with_equals`` engine), so ``old`` in the next ``changed(old, new)`` is
    always the last propagated value.

    ``changed`` MUST be a **pure** function of ``(old, new)``. Reading
    value-carried state is fine and stays deterministic — a version/counter/
    sequence field that rides *inside* the value (as in "propagate every N",
    ``lambda o, n: n // N != o // N``). Capturing **external mutable** state is
    not: under laziness the predicate keys off recompute/read frequency rather
    than off the values, which breaks determinism.

    As with the natural-equality guard, suppression is observable only on the
    **eager** form (``.eager()``): a lazy computed recomputes through its backing
    memo on every read and does not hold a settled value to guard against. Call
    :meth:`~Computed.eager` for the guarded, materialized form::

        input = source(lambda c: 0)
        # propagate only when the /10 bucket changes, ignoring the raw payload
        derived = computed_ripple_when(
            ctx,
            lambda c: (input(c).value, input(c).value // 10),
            lambda o, n: o[1] != n[1],
        ).eager()

    Note the ``(ctx, callable, changed)`` signature: lazily-py uses a
    context-as-dict model (there is no ``Context`` object with a
    ``.computed_ripple_when`` method), so the Rust reference's
    ``ctx.computed_ripple_when(f, changed)`` becomes
    ``computed_ripple_when(ctx, f, changed)`` here.
    """
    return Computed(ctx, callable, changed=changed)


def computed_def[C_in, T](
    resolve_ctx: Callable[[C_in], dict],
) -> Callable[[Callable[[dict], T]], Slot[C_in, dict, Computed[T]]]:
    """Decorator factory: a context-cached lazy :class:`Computed`, custom resolver.

    Like :func:`computed` but produces a *lazy* computed factory keyed on a
    resolved context. Call :meth:`~Computed.eager` on the resolved computed for
    the eager form.
    """

    def outer(callable: Callable[[dict], T]) -> Slot[C_in, dict, Computed[T]]:
        return Slot(
            callable=lambda ctx: Computed(ctx, callable),
            resolve_ctx=resolve_ctx,
        )

    return outer
