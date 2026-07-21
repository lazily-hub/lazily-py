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
dependency changes. There is **no unguarded mode**: every cell is guarded. For a
genuinely non-``__eq__``-comparable value hold it in the lower-level, unguarded
:class:`~lazily.slot.Slot` storage primitive (``slot``) instead — the deliberate
storage-sense escape (mirrors ``lazily-rs``'s ``slot``); the former separate
``memo`` constructor is retired because ``computed`` now *is* the guarded form.

The eager construction is ``computed(ctx, f).eager()``. It **retires the former
``Signal``**: eagerness is graph state (an ``_eager`` bit plus an ``_eager_by``
side table holding the puller), not a distinct node kind. The puller is an
ordinary :class:`~lazily.effect.Effect` over the backing memo, so it is
*scheduled*: N writes inside one ``batch`` re-materialize the computed **once**,
at the flush, not once per write (``reactive-graph.md`` clause 3). Because the
only way to make a computed eager is to attach a scheduled ``Effect``, the
``#lzsignaleager`` per-write puller — an ``onInvalidate`` hook that recomputes
during the invalidation wave — is structurally unrepresentable here.

``Signal`` / ``signal`` / ``signal_def`` are retained as thin back-compat
aliases: ``Signal(ctx, f)`` is ``computed(ctx, f).eager()``. ``FormulaCell`` /
``formula`` / ``formula_def`` are retained as v1 back-compat aliases of
``Computed`` / ``computed`` / ``computed_def``.

This module keeps its ``signal.py`` filename (it is on the mypyc compile list in
the Makefile) though its subject is now the ``Computed`` cell.

On the wire an eager computed is just the ordinary backing memo node that stores
its materialized value (no separate wire type). The puller is local execution
state and is never serialized.
"""

from __future__ import annotations


__all__ = [
    "Computed",
    "FormulaCell",
    "Signal",
    "computed",
    "computed_def",
    "formula",
    "formula_def",
    "signal",
    "signal_def",
]

from typing import TYPE_CHECKING, Any, TypeVar

from .effect import Effect
from .slot import Slot, _drain_resets, _reset_work, mypyc_attr, slot, slot_stack


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
    ctx: dict

    def __init__(self, ctx: dict, callable: Callable[[dict], T]) -> None:
        self.ctx = ctx
        # Lazily materialized on first parent: an empty CPython ``set()`` is
        # ~216 B, so deferring it keeps quiescent computeds cheap.
        self._parents = None
        self._eager = False
        self._puller = None
        self._value = _UNSET
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

    # -- v1 back-compat: ``drive`` / ``undrive`` / ``is_driven`` ------------
    def drive(self) -> Computed[T]:
        """Deprecated v1 alias for :meth:`eager`."""
        return self.eager()

    def undrive(self) -> None:
        """Deprecated v1 alias for :meth:`lazy`."""
        self.lazy()

    def is_driven(self) -> bool:
        """Deprecated v1 alias for :meth:`is_eager`."""
        return self._eager

    # Back-compat: the former ``Signal.is_active``.
    def is_active(self) -> bool:
        """Deprecated alias for :meth:`is_eager`."""
        return self._eager

    def _pull(self, ctx: dict) -> None:
        """Puller-Effect body: re-materialize the backing memo into ``_value``."""
        new_value = self._slot(ctx)
        if self._value is _UNSET:
            self._value = new_value
            return None
        # PartialEq guard: an equal recompute suppresses the cascade.
        if new_value != self._value:
            self._value = new_value
            self.touch()
        return None

    @property
    def value(self) -> T:
        """The current value; auto-subscribes the reading slot.

        Eager: returns the materialized value. Lazy: recomputes through the
        backing memo on read.
        """
        if slot_stack:
            # Identity-based parent tracking (mirrors Cell/Slot): avoids a
            # per-read ``functools.partial`` allocation that does not deduplicate
            # in a set and would otherwise grow without bound.
            if self._parents is None:
                self._parents = set()
            self._parents.add(slot_stack[-1])
        if not self._eager:
            # Lazy: recompute on read via the backing memo. The reading slot is
            # tracked as a dependency of the backing memo too (same slot_stack
            # frame), so an upstream change still invalidates the reader.
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


# v1 back-compat: ``FormulaCell`` was the v1 name for the derived cell.
FormulaCell = Computed


@mypyc_attr(allow_interpreted_subclasses=True)
class Signal[T](Computed[T]):
    """Back-compat: an eager :class:`Computed`.

    ``Signal(ctx, f)`` is exactly ``computed(ctx, f).eager()`` — a computed that
    is eager at construction, the behaviour the former standalone ``Signal`` had.
    Prefer ``computed(ctx, f).eager()`` in new code.
    """

    __slots__ = ()

    def __init__(self, ctx: dict, callable: Callable[[dict], T]) -> None:
        super().__init__(ctx, callable)
        self.eager()


def computed[T](ctx: dict, callable: Callable[[dict], T]) -> Computed[T]:
    """Create a lazy, guarded :class:`Computed` bound to ``ctx``.

    The canonical derived-value constructor of the Cell kernel and **guarded by
    default** — an equal recompute suppresses the downstream cascade (matching
    TC39 ``Signal.Computed``). It replaces the v1 ``formula`` and the former
    ``memo`` (there is no unguarded mode; for a non-``__eq__`` value use the
    lower-level :func:`~lazily.slot.slot`). Call :meth:`~Computed.eager` for the
    eager form::

        n = cell(lambda c: 1)
        doubled = computed(ctx, lambda c: n(c).value * 2).eager()
        doubled.value  # 4, kept fresh eagerly

    Note the ``(ctx, callable)`` signature: lazily-py uses a context-as-dict
    model (there is no ``Context`` object with a ``.computed`` method), so the
    Rust reference's ``ctx.computed(f)`` becomes ``computed(ctx, f)`` here.
    """
    return Computed(ctx, callable)


# v1 back-compat: ``formula`` was the v1 name for the guarded derived constructor.
formula = computed


def signal[T](callable: Callable[[dict], T]) -> Slot[dict, dict, Signal[T]]:
    """Back-compat decorator: a context-cached eager :class:`Computed` factory.

    Retained for the former ``Signal`` surface. The returned factory is
    context-cached (one eager computed per context), so ``my_signal(ctx)``
    returns the same eager computed on repeated calls::

        @signal
        def doubled(ctx: dict) -> int:
            return n(ctx).value * 2


        s = doubled(ctx)  # eager: computed now
        s.value  # always current

    Prefer ``computed(ctx, f).eager()`` in new code.
    """
    return slot(lambda ctx: Signal(ctx, callable))


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


# v1 back-compat.
formula_def = computed_def


def signal_def[C_in, T](
    resolve_ctx: Callable[[C_in], dict],
) -> Callable[[Callable[[dict], T]], Slot[C_in, dict, Signal[T]]]:
    """Back-compat decorator factory: like :func:`signal`, with a custom resolver."""

    def outer(callable: Callable[[dict], T]) -> Slot[C_in, dict, Signal[T]]:
        return Slot(
            callable=lambda ctx: Signal(ctx, callable),
            resolve_ctx=resolve_ctx,
        )

    return outer
