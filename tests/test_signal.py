"""Tests for the eager ``Computed`` (``computed(ctx, f).eager()``)."""

from __future__ import annotations

from lazily import Slot, computed, source


def test_computed_eager_value_at_creation() -> None:
    ctx: dict = {}
    n = source(lambda c: 1)
    doubled = computed(ctx, lambda c: n(c).value * 2).eager()
    # Eager: value is materialized at construction, no read needed first.
    assert doubled.value == 2


def test_computed_recomputes_eagerly_on_dependency_change() -> None:
    ctx: dict = {}
    n = source(lambda c: 1)
    doubled = computed(ctx, lambda c: n(c).value * 2).eager()
    assert doubled.value == 2

    n(ctx).value = 5
    # No read of the cell needed to refresh — the computed already recomputed.
    assert doubled.value == 10


def test_computed_guard_suppresses_equal_recompute() -> None:
    ctx: dict = {}
    src = source(lambda c: 10)
    sig = computed(ctx, lambda c: src(c).value // 10).eager()

    runs = {"n": 0}

    @Slot
    def view(c: dict) -> int:
        runs["n"] += 1
        return c.read(sig)

    assert view(ctx) == 1
    assert runs["n"] == 1

    # 10 -> 15: computed recomputes to 1 (equal) -> downstream cache preserved.
    src(ctx).value = 15
    assert view(ctx) == 1
    assert runs["n"] == 1

    # 15 -> 20: computed -> 2 -> downstream invalidated and recomputed.
    src(ctx).value = 20
    assert view(ctx) == 2
    assert runs["n"] == 2


def test_computed_tracked_as_dependency_by_slot() -> None:
    ctx: dict = {}
    n = source(lambda c: 2)
    sig = computed(ctx, lambda c: n(c).value + 1).eager()

    @Slot
    def derived(c: dict) -> int:
        return c.read(sig) * 100

    assert derived(ctx) == 300
    n(ctx).value = 9  # sig: 3 -> 10
    assert derived(ctx) == 1000


def test_chained_eager_computeds() -> None:
    ctx: dict = {}
    base = source(lambda c: 1)
    a = computed(ctx, lambda c: base(c).value + 1).eager()
    b = computed(ctx, lambda c: c.read(a) * 10).eager()

    assert a.value == 2
    assert b.value == 20

    base(ctx).value = 5  # a: 6, b: 60
    assert a.value == 6
    assert b.value == 60


def test_computed_dispose_reverts_to_lazy() -> None:
    ctx: dict = {}
    src = source(lambda c: 1)
    sig = computed(ctx, lambda c: src(c).value * 2).eager()
    assert sig.value == 2
    assert sig.is_eager()

    sig.dispose()
    assert not sig.is_eager()

    src(ctx).value = 5
    # Eager puller removed; value is recomputed lazily on the next read.
    assert sig.value == 10


def test_eager_computed_factory_is_context_cached() -> None:
    ctx: dict = {}
    n = source(lambda c: 3)

    tripled = Slot(lambda c: computed(c, lambda cc: n(cc).value * 3).eager())

    s1 = tripled(ctx)
    s2 = tripled(ctx)
    assert s1 is s2  # one eager computed per context
    assert s1.value == 9

    n(ctx).value = 4
    assert tripled(ctx).value == 12


def test_computed_depends_on_slot() -> None:
    ctx: dict = {}

    @Slot
    def base(c: dict) -> int:
        return 21

    sig = computed(ctx, lambda c: base(c) * 2).eager()
    assert sig.value == 42

    base.reset(ctx)
    assert sig.value == 42

    runs = {"n": 0}

    @Slot
    def view(c: dict) -> int:
        runs["n"] += 1
        return base(c) + c.read(sig)

    assert view(ctx) == 63
    assert runs["n"] == 1
    assert view(ctx) == 63
    assert runs["n"] == 1


def test_computed_get_and_call_aliases() -> None:
    ctx: dict = {}
    n = source(lambda c: 7)
    sig = computed(ctx, lambda c: n(c).value).eager()
    assert sig.get() == 7
    assert sig() == 7
    assert sig.value == 7
