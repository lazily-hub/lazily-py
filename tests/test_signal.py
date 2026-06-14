"""Tests for the eager ``Signal`` primitive (Slot → Cell → Signal family)."""

from __future__ import annotations

from lazily import Signal, cell, signal, slot


def test_signal_eager_value_at_creation() -> None:
    ctx: dict = {}
    n = cell(lambda c: 1)
    doubled = Signal(ctx, lambda c: n(c).value * 2)
    # Eager: value is materialized at construction, no read needed first.
    assert doubled.value == 2


def test_signal_recomputes_eagerly_on_dependency_change() -> None:
    ctx: dict = {}
    n = cell(lambda c: 1)
    doubled = Signal(ctx, lambda c: n(c).value * 2)
    assert doubled.value == 2

    n(ctx).value = 5
    # No read of the cell needed to refresh — the Signal already recomputed.
    assert doubled.value == 10


def test_signal_memo_guard_suppresses_equal_recompute() -> None:
    ctx: dict = {}
    src = cell(lambda c: 10)
    sig = Signal(ctx, lambda c: src(c).value // 10)

    runs = {"n": 0}

    @slot
    def view(c: dict) -> int:
        runs["n"] += 1
        return sig.value

    assert view(ctx) == 1
    assert runs["n"] == 1

    # 10 -> 15: signal recomputes to 1 (equal) -> downstream cache preserved.
    src(ctx).value = 15
    assert view(ctx) == 1
    assert runs["n"] == 1

    # 15 -> 20: signal -> 2 -> downstream invalidated and recomputed.
    src(ctx).value = 20
    assert view(ctx) == 2
    assert runs["n"] == 2


def test_signal_tracked_as_dependency_by_slot() -> None:
    ctx: dict = {}
    n = cell(lambda c: 2)
    sig = Signal(ctx, lambda c: n(c).value + 1)

    @slot
    def derived(c: dict) -> int:
        return sig.value * 100

    assert derived(ctx) == 300
    n(ctx).value = 9  # sig: 3 -> 10
    assert derived(ctx) == 1000


def test_chained_signals() -> None:
    ctx: dict = {}
    base = cell(lambda c: 1)
    a = Signal(ctx, lambda c: base(c).value + 1)
    b = Signal(ctx, lambda c: a.value * 10)

    assert a.value == 2
    assert b.value == 20

    base(ctx).value = 5  # a: 6, b: 60
    assert a.value == 6
    assert b.value == 60


def test_signal_dispose_reverts_to_lazy() -> None:
    ctx: dict = {}
    src = cell(lambda c: 1)
    sig = Signal(ctx, lambda c: src(c).value * 2)
    assert sig.value == 2
    assert sig.is_active()

    sig.dispose()
    assert not sig.is_active()

    src(ctx).value = 5
    # Eager puller removed; value is recomputed lazily on the next read.
    assert sig.value == 10


def test_signal_decorator_is_context_cached() -> None:
    ctx: dict = {}
    n = cell(lambda c: 3)

    @signal
    def tripled(c: dict) -> int:
        return n(c).value * 3

    s1 = tripled(ctx)
    s2 = tripled(ctx)
    assert s1 is s2  # one eager Signal per context
    assert s1.value == 9

    n(ctx).value = 4
    assert tripled(ctx).value == 12


def test_signal_get_and_call_aliases() -> None:
    ctx: dict = {}
    n = cell(lambda c: 7)
    sig = Signal(ctx, lambda c: n(c).value)
    assert sig.get() == 7
    assert sig() == 7
    assert sig.value == 7
