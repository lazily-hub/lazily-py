"""Regression tests guarding the Signal->Slot ``RecursionError`` fix (commit 30907ea).

Before the fix, two issues caused unbounded recursion:

1. ``Slot.__call__`` fired ``touch()`` immediately after computing, so merely
   *reading* a Slot cascaded invalidation through its dependents. During
   ``Signal`` construction this closed a ``_SignalSlot`` <-> source-Slot
   dependency cycle and blew the stack.
2. ``Slot.reset`` notified subscribers *before* clearing them, so a subscriber
   that itself triggered a re-entrant reset found a non-empty subscriber set
   and mutually recursed.

The fix:
  - ``Slot.__call__`` no longer touches (computation must not cascade).
  - the invalidation wave rebinds a node's downstream edges to ``None`` BEFORE
    propagating.

**Both tests were ported when the observer registries were removed**, because
each was originally written against ``Slot.subscribe``. What they pin, and how
faithfully, differs — see each test:

* Fix 1 still discriminates exactly. Its *symptom* changed: with observers gone,
  a cascade during computation does not spuriously notify a callback, it
  destroys the dependency edge (``touch`` rebinds ``_parents`` to ``None``), so
  the dependent silently stops updating forever. That is a strictly worse bug
  than the original and the test now catches it.
* Fix 2's original mechanism no longer exists — notify-before-clear was a
  property of a *callback registry*, and there is no callback registry to
  re-enter. Propagation is now an append to an iterative work-stack drained by a
  single loop, so reordering the clear in ``Slot._invalidate`` is unobservable
  (verified: both orderings terminate identically). The re-entrancy guarantee
  relocated to ``Effect._running``, and the ported test pins it there — removing
  that guard makes it diverge without bound. Coverage was relocated, not lost.
"""

from __future__ import annotations

from lazily import Slot, computed, source
from lazily.effect import effect


def test_reading_a_slot_does_not_cascade_and_preserves_its_edges() -> None:
    """Fix 1: ``Slot.__call__`` must NOT touch during computation.

    Re-adding ``self.touch(resolved)`` to ``Slot.__call__`` makes this fail.
    The failure mode is edge destruction, not a spurious notification: ``touch``
    rebinds ``_parents`` to ``None``, so a slot that cascaded while computing
    wipes the very dependent that was mid-computation registering itself. The
    effect then never reruns again. Asserting the second run is therefore a
    stronger check than the original ``notified == []``.
    """
    runs: list[int] = []
    ctx: dict = {}
    src = source(lambda c: 1)

    @Slot
    def derived(c: dict) -> int:
        return src(c).value + 1

    eff = effect(lambda c: runs.append(derived(c)))
    eff(ctx)
    assert runs == [2], "initial run"

    src(ctx).value = 5
    assert runs == [2, 6], (
        "the dependency edge must survive computation; under the reverted fix "
        "`derived._parents` is wiped by its own touch and this stays [2]"
    )


def test_reentrant_reset_from_an_effect_body_terminates() -> None:
    """Fix 2, ported: a re-entrant reset must not diverge.

    The original form drove this through a ``Slot.subscribe`` callback that
    re-entered ``reset``; with observers removed there is no callback to
    re-enter, so this drives it through an Effect body instead — the only
    remaining way user code runs inside an invalidation wave.

    This no longer discriminates the historical notify-before-clear ordering
    (that ordering is unobservable now). It pins the guarantee's current owner:
    deleting the ``self._running`` check in ``Effect._invalidate`` makes this
    loop without bound.
    """
    fired: list[int] = []
    ctx: dict = {}

    @Slot
    def base(c: dict) -> int:
        return 1

    def body(c: dict) -> None:
        base(c)  # depend on base
        fired.append(1)
        assert len(fired) < 100, "re-entrant reset diverged"
        base.reset(c)  # re-entrant reset from inside the rerun

    eff = effect(body)
    eff(ctx)
    base.reset(ctx)  # must terminate

    assert fired == [1]


def test_deep_slot_cascade_invalidates_without_recursion() -> None:
    """A 300-deep Slot chain must invalidate + recompute without blowing the
    stack. The reset cascade is one frame per level only because reset clears
    before notifying; the old notify-before-clear path recursed."""
    ctx: dict = {}
    src = source(lambda c: 1)

    @Slot
    def first(c: dict) -> int:
        return src(c).value + 1

    depth = 300
    chain: list = [first]
    for _ in range(depth):
        prev = chain[-1]
        chain.append(Slot(lambda c, p=prev: p(c) + 1))

    tail = chain[-1]
    assert tail(ctx) == depth + 2  # src(1) + 1 (first) + 1 per extra level

    src(ctx).value = 10
    assert tail(ctx) == 10 + 1 + depth  # full cascade through `depth` levels


def test_signal_over_slot_cycle_invalidates_without_recursion() -> None:
    """The original topology: a Signal that reads a Slot, with a downstream
    Slot reading that Signal, closing the ``_SignalSlot`` <-> source-Slot
    subscription cycle. Driving it from a Cell mutation exercised the
    RecursionError path."""
    ctx: dict = {}
    src = source(lambda c: 1)

    @Slot
    def mid(c: dict) -> int:
        return src(c).value + 1

    sig = computed(ctx, lambda c: mid(c) * 10).eager()  # Signal reads Slot
    assert sig.value == 20

    runs = {"n": 0}

    @Slot
    def downstream(c: dict) -> int:  # Slot reads Signal -> cycle closed
        runs["n"] += 1
        return c.read(sig) + 1

    assert downstream(ctx) == 21
    assert runs["n"] == 1

    src(ctx).value = 4  # mid -> 5, sig -> 50, downstream -> 51
    assert sig.value == 50
    assert downstream(ctx) == 51
    assert runs["n"] == 2


def test_deep_signal_chain_propagates_without_recursion() -> None:
    """A deep chain of eager Signals over a Cell must recompute eagerly across
    the whole chain on a single mutation without RecursionError."""
    ctx: dict = {}
    src = source(lambda c: 0)

    sig = computed(ctx, lambda c: src(c).value + 1).eager()
    assert sig.value == 1

    depth = 200
    for _ in range(depth):
        prev = sig
        sig = computed(ctx, lambda c, p=prev: c.read(p) + 1).eager()

    src(ctx).value = 7
    assert sig.value == 7 + 1 + depth  # eagerly recomputed end-to-end
