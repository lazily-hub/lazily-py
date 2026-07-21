"""The ``FormulaCell`` — a derived reactive value, and its eager (**driven**) form.

Part of the Cell kernel (``#lzcellkernel``, see
``lazily-spec/docs/reactive-graph.md`` and
``tasks/software/lazily-cell-kernel-design.md``). The kernel is one genus with
two value kinds:

``SourceCell`` (:class:`~lazily.cell.Cell`) — value comes from *outside*
(``set`` / ``merge``); :class:`FormulaCell` — value comes from *upstream*, via a
formula. ``Effect`` stays outside the hierarchy (a sink, no value).

A :class:`FormulaCell` is **lazy by default** and guarded: invalidation only
marks it dirty and the value is recomputed on the next read, and an equal
recompute suppresses the downstream cascade (the memo/PartialEq guard). Calling
:meth:`~FormulaCell.drive` makes it **eager** — it materializes now and
recomputes immediately whenever a tracked dependency changes.

The eager construction is ``formula(ctx, f).drive()``. It **retires the former
``Signal``**: drivenness is graph state (a ``_driven`` bit plus a ``_driven_by``
side table holding the puller), not a distinct node kind. The puller is an
ordinary :class:`~lazily.effect.Effect` over the backing memo, so it is
*scheduled*: N writes inside one ``batch`` re-materialize the formula **once**,
at the flush, not once per write (``reactive-graph.md`` clause 3). Because the
only way to make a formula eager is to attach a scheduled ``Effect``, the
``#lzsignaleager`` per-write puller — an ``onInvalidate`` hook that recomputes
during the invalidation wave — is structurally unrepresentable here.

``Signal`` / ``signal`` / ``signal_def`` are retained as thin back-compat
aliases: ``Signal(ctx, f)`` is ``formula(ctx, f).drive()``.

This module keeps its ``signal.py`` filename (it is on the mypyc compile list in
the Makefile) though its subject is now the ``FormulaCell``.

On the wire a driven formula is just the ordinary backing memo node that stores
its materialized value (no separate wire type). The puller is local execution
state and is never serialized.
"""

from __future__ import annotations


__all__ = [
    "FormulaCell",
    "Signal",
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

# First-materialization sentinel: ``None`` is a legal formula value, so the
# initial pull is distinguished by identity rather than by comparing to None.
_UNSET: Any = object()

# ``driven_by`` side table (``reactive-graph.md`` §9.3.3): the ``_driven`` bit on
# the FormulaCell answers "am I driven?" for free (making ``drive`` idempotent
# with no lookup); this table holds "which effect drives me" for exactly the rare
# driven formulas, and nothing for lazy ones. Owner-keyed by the FormulaCell
# object. Python object identity is stable for the object's lifetime and a
# disposed formula is not recycled onto a live one (unlike Rust's ``SlotId``), so
# the generation-tag hazard of §9.3.5 does not arise; the strong reference is
# released on ``undrive`` / ``dispose`` so a torn-down formula is collectable.
_driven_by: dict[FormulaCell[Any], Effect] = {}


@mypyc_attr(allow_interpreted_subclasses=True)
class FormulaCell[T]:
    """A derived reactive value bound to a single context — lazy by default.

    The value is computed from upstream. Reading :attr:`value` inside a
    Slot/FormulaCell/Effect computation registers a dependency, so downstream
    reactives invalidate when this value changes.

    **Lazy** (the default): the value is recomputed on the next read after an
    upstream change. **Driven** (after :meth:`drive`): the value is materialized
    now and re-materialized eagerly — through a scheduled puller Effect — on
    every upstream change, with an equality guard that suppresses downstream
    cascades on an equal recompute.

    Like every reactive in this library, a FormulaCell exposes **no observer
    API**. See :class:`~lazily.cell.Cell` for the rationale. Where a caller needs
    a stream of *every* transition rather than the settled value, that is a
    :class:`~lazily.queue.Topic`.
    """

    __slots__ = (
        "_driven",
        "_parents",
        "_puller",
        "_slot",
        "_value",
        "ctx",
    )

    _parents: set[Slot[Any, Any, Any]] | None
    _driven: bool
    _puller: Effect | None
    _slot: Slot[dict, dict, T]
    _value: T
    ctx: dict

    def __init__(self, ctx: dict, callable: Callable[[dict], T]) -> None:
        self.ctx = ctx
        # Lazily materialized on first parent: an empty CPython ``set()`` is
        # ~216 B, so deferring it keeps quiescent formulas cheap.
        self._parents = None
        self._driven = False
        self._puller = None
        self._value = _UNSET
        self._slot = Slot(callable=callable)

    # -- drive / undrive: the eager construction (§9.3.1) --------------------

    def drive(self) -> FormulaCell[T]:
        """Make this formula **eager**, and return **this same** formula.

        Idempotent — a second ``drive`` is a no-op — so ``f.drive().drive()``
        never attaches two pullers (which would double the eager compute, the
        ``#lzsignaleager`` cost class from the other direction). Attaches a
        scheduled puller :class:`~lazily.effect.Effect` over the backing memo and
        records it in the ``_driven_by`` side table, then materializes the value
        once (clause 1) and establishes the dependency edges. Because the puller
        is an Effect it obeys *effects are scheduled, not inline*: N writes inside
        one ``batch`` coalesce into ONE re-materialization at the flush (clause
        3).

        Returns the same handle with graph state mutated — ``g = f.drive()``
        gives ``g is f``, both driven; it is not builder-style ``with(...)``.
        """
        if self._driven:
            return self
        self._driven = True
        puller = Effect(self._pull)
        self._puller = puller
        _driven_by[self] = puller
        puller(self.ctx)
        return self

    def undrive(self) -> None:
        """Reverse of :meth:`drive`: stop eager recomputation, dispose the puller.

        The value remains readable and reverts to lazy (recomputed on the next
        read of the backing memo). No-op if the formula is not driven. Clears the
        ``_driven`` bit and the ``_driven_by`` entry (``reactive-graph.md`` clause
        4), so no puller is stranded.
        """
        if not self._driven:
            return
        self._driven = False
        puller = _driven_by.pop(self, None)
        self._puller = None
        if puller is not None:
            puller.dispose()

    def is_driven(self) -> bool:
        """Whether this formula is currently driven (has an active puller)."""
        return self._driven

    # Back-compat: the former ``Signal.is_active``.
    def is_active(self) -> bool:
        """Deprecated alias for :meth:`is_driven`."""
        return self._driven

    def _pull(self, ctx: dict) -> None:
        """Puller-Effect body: re-materialize the backing memo into ``_value``."""
        new_value = self._slot(ctx)
        if self._value is _UNSET:
            self._value = new_value
            return None
        # Memo / PartialEq guard: an equal recompute suppresses the cascade.
        if new_value != self._value:
            self._value = new_value
            self.touch()
        return None

    @property
    def value(self) -> T:
        """The current value; auto-subscribes the reading slot.

        Driven: returns the materialized value. Lazy: recomputes through the
        backing memo on read.
        """
        if slot_stack:
            # Identity-based parent tracking (mirrors Cell/Slot): avoids a
            # per-read ``functools.partial`` allocation that does not deduplicate
            # in a set and would otherwise grow without bound.
            if self._parents is None:
                self._parents = set()
            self._parents.add(slot_stack[-1])
        if not self._driven:
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

        Disposing a driven formula tears down its puller
        (``reactive-graph.md`` clause 4); the backing memo is untouched, so the
        value stays readable, stays correct, and no longer re-materializes on
        write.
        """
        self.undrive()


@mypyc_attr(allow_interpreted_subclasses=True)
class Signal[T](FormulaCell[T]):
    """Back-compat: an eager (driven) :class:`FormulaCell`.

    ``Signal(ctx, f)`` is exactly ``formula(ctx, f).drive()`` — a formula that is
    driven at construction, the behaviour the former standalone ``Signal`` had.
    Prefer ``formula(ctx, f).drive()`` in new code.
    """

    __slots__ = ()

    def __init__(self, ctx: dict, callable: Callable[[dict], T]) -> None:
        super().__init__(ctx, callable)
        self.drive()


def formula[T](ctx: dict, callable: Callable[[dict], T]) -> FormulaCell[T]:
    """Create a lazy, guarded :class:`FormulaCell` bound to ``ctx``.

    The canonical derived-value constructor of the Cell kernel — it replaces
    ``computed`` / ``memo`` / ``slot`` (as a reactive value) and is **guarded by
    default**. Call :meth:`~FormulaCell.drive` for the eager form::

        n = cell(lambda c: 1)
        doubled = formula(ctx, lambda c: n(c).value * 2).drive()
        doubled.value  # 4, kept fresh eagerly

    Note the ``(ctx, callable)`` signature: lazily-py uses a context-as-dict
    model (there is no ``Context`` object with a ``.formula`` method), so the
    Rust reference's ``ctx.formula(f)`` becomes ``formula(ctx, f)`` here.
    """
    return FormulaCell(ctx, callable)


def signal[T](callable: Callable[[dict], T]) -> Slot[dict, dict, Signal[T]]:
    """Back-compat decorator: a context-cached eager (driven) FormulaCell factory.

    Retained for the former ``Signal`` surface. The returned factory is
    context-cached (one driven formula per context), so ``my_signal(ctx)``
    returns the same eager formula on repeated calls::

        @signal
        def doubled(ctx: dict) -> int:
            return n(ctx).value * 2


        s = doubled(ctx)  # eager: computed now
        s.value  # always current

    Prefer ``formula(ctx, f).drive()`` in new code.
    """
    return slot(lambda ctx: Signal(ctx, callable))


def formula_def[C_in, T](
    resolve_ctx: Callable[[C_in], dict],
) -> Callable[[Callable[[dict], T]], Slot[C_in, dict, FormulaCell[T]]]:
    """Decorator factory: a context-cached lazy :class:`FormulaCell`, custom resolver.

    Like :func:`formula` but produces a *lazy* formula factory keyed on a
    resolved context. Call ``.drive()`` on the resolved formula for the eager
    form.
    """

    def outer(callable: Callable[[dict], T]) -> Slot[C_in, dict, FormulaCell[T]]:
        return Slot(
            callable=lambda ctx: FormulaCell(ctx, callable),
            resolve_ctx=resolve_ctx,
        )

    return outer


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
