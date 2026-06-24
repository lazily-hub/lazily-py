"""Regression tests guarding the Signal->Slot ``RecursionError`` fix (commit 30907ea).

Before the fix, two issues caused unbounded recursion:

1. ``Slot.__call__`` fired ``touch()`` immediately after computing, so merely
   *reading* a Slot cascaded invalidation through its subscribers. During
   ``Signal`` construction this closed a ``_SignalSlot`` <-> source-Slot
   subscription cycle and blew the stack.
2. ``Slot.reset`` notified subscribers *before* clearing them, so a subscriber
   that itself triggered a re-entrant reset found a non-empty subscriber set
   and mutually recursed.

The fix:
  - ``Slot.__call__`` no longer touches (computation must not cascade).
  - ``Slot.reset`` snapshots + clears subscribers, then notifies the snapshot.

These tests pin both invariants directly (so reverting either change fails the
suite) and add a deep cascade + the Signal->Slot topology that originally
triggered the bug.
"""

from __future__ import annotations

from lazily import Signal, cell, slot


def test_slot_call_does_not_notify_subscribers() -> None:
    """Fix 1: ``Slot.__call__`` must NOT touch subscribers during computation.

    Re-adding ``self.touch(resolved)`` to ``Slot.__call__`` makes this fail.
    """
    notified: list[str] = []

    @slot
    def derived(c: dict) -> int:
        return 1

    derived.subscribe(lambda slot_, ctx: notified.append("touched"))
    derived({})  # computation must not cascade invalidation

    assert notified == []


def test_slot_reset_is_reentrancy_safe() -> None:
    """Fix 2: ``Slot.reset`` clears subscribers BEFORE notification, so a
    subscriber that re-enters reset finds an empty set and cannot mutually
    recurse.

    Reverting reset to notify-before-clear raises ``RecursionError`` here.
    """
    fired: list[int] = []

    @slot
    def base(c: dict) -> int:
        return 1

    def reentrant(slot_: object, ctx: dict) -> None:
        # Re-entrant reset: under the old code this found non-empty subscribers
        # and recursed without bound.
        base.reset(ctx)
        fired.append(1)

    ctx: dict = {}
    base(ctx)
    base.subscribe(reentrant)
    base.reset(ctx)  # must not raise

    assert fired == [1]


def test_deep_slot_cascade_invalidates_without_recursion() -> None:
    """A 300-deep Slot chain must invalidate + recompute without blowing the
    stack. The reset cascade is one frame per level only because reset clears
    before notifying; the old notify-before-clear path recursed."""
    ctx: dict = {}
    src = cell(lambda c: 1)

    @slot
    def first(c: dict) -> int:
        return src(c).value + 1

    depth = 300
    chain: list = [first]
    for _ in range(depth):
        prev = chain[-1]
        chain.append(slot(lambda c, p=prev: p(c) + 1))

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
    src = cell(lambda c: 1)

    @slot
    def mid(c: dict) -> int:
        return src(c).value + 1

    sig = Signal(ctx, lambda c: mid(c) * 10)  # Signal reads Slot
    assert sig.value == 20

    runs = {"n": 0}

    @slot
    def downstream(c: dict) -> int:  # Slot reads Signal -> cycle closed
        runs["n"] += 1
        return sig.value + 1

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
    src = cell(lambda c: 0)

    sig = Signal(ctx, lambda c: src(c).value + 1)
    assert sig.value == 1

    depth = 200
    for _ in range(depth):
        prev = sig
        sig = Signal(ctx, lambda c, p=prev: p.value + 1)

    src(ctx).value = 7
    assert sig.value == 7 + 1 + depth  # eagerly recomputed end-to-end
