"""``computed_ripple_when`` (#lzcellkernel) — a guarded computed with an
explicit, PURE change predicate (``True`` = propagate).

Mirrors ``lazily-rs`` ``tests/computed_ripple_when.rs``. Covers the two
motivating shapes — a custom significance policy, and "propagate every N" where
the increment evidence lives in the value (so the predicate stays pure) — plus
the two identities: ``computed(f) == computed_ripple_when(f, !=)`` and the
pass-through ``computed_ripple_when(f, True)`` (always propagate).

As with the natural-equality guard, suppression is observable only on the
**eager** form (a lazy computed recomputes through its backing memo on every
read and holds no settled value to guard against), so every guarded computed
here is ``.eager()`` — matching ``test_signal.py``'s
``test_computed_guard_suppresses_equal_recompute``.
"""

from __future__ import annotations

from lazily import Slot, computed, computed_ripple_when, source


def test_custom_significance_propagates_on_proxy_change() -> None:
    ctx: dict = {}
    input = source(lambda c: 0)

    # Derived value carries a ``bucket`` proxy; propagate only when the bucket
    # changes, ignoring the raw payload.
    derived = computed_ripple_when(
        ctx,
        lambda c: (input(c).value, input(c).value // 10),  # (payload, bucket)
        lambda old, new: old[1] != new[1],  # propagate when bucket changed
    ).eager()

    runs = {"n": 0}

    @Slot
    def observer(c: dict) -> int:
        runs["n"] += 1
        return derived.value[0]

    assert observer(ctx) == 0
    base = runs["n"]

    # Same bucket (0..9): dependent stays cached.
    input(ctx).value = 3
    assert observer(ctx) == 0, "suppressed: proxy bucket unchanged"
    assert runs["n"] == base, "no dependent recompute within a bucket"

    # Crossing a bucket boundary propagates.
    input(ctx).value = 12
    assert observer(ctx) == 12, "propagated: bucket changed"
    assert runs["n"] == base + 1


def test_propagate_every_n_via_value_carried_counter() -> None:
    ctx: dict = {}
    input = source(lambda c: 0)

    # "Propagate every 3rd increment" — evidence (the counter) is IN the value,
    # so the predicate is a pure function of (old, new): propagate only when the
    # count crosses a size-3 window boundary.
    sampled = computed_ripple_when(
        ctx,
        lambda c: input(c).value,
        lambda old, new: new // 3 != old // 3,
    ).eager()

    seen = {"n": 0}

    @Slot
    def observer(c: dict) -> int:
        seen["n"] += 1
        return sampled.value

    assert observer(ctx) == 0
    base = seen["n"]

    # 0 -> 1 -> 2 stay in window [0,3): suppressed.
    input(ctx).value = 1
    input(ctx).value = 2
    assert observer(ctx) == 0
    assert seen["n"] == base, "window not crossed yet"

    # 3 crosses into [3,6): propagate.
    input(ctx).value = 3
    assert observer(ctx) == 3
    assert seen["n"] == base + 1


def test_computed_is_computed_ripple_when_not_equal() -> None:
    """``computed(f)`` behaves as ``computed_ripple_when(f, lambda o, n: o != n)``."""
    ctx: dict = {}
    input = source(lambda c: 0)

    via_computed = computed(ctx, lambda c: min(input(c).value, 1)).eager()
    via_when = computed_ripple_when(
        ctx,
        lambda c: min(input(c).value, 1),
        lambda o, n: o != n,
    ).eager()

    counts = {"a": 0, "b": 0}

    @Slot
    def obs_a(c: dict) -> int:
        counts["a"] += 1
        return via_computed.value

    @Slot
    def obs_b(c: dict) -> int:
        counts["b"] += 1
        return via_when.value

    assert obs_a(ctx) == 0
    assert obs_b(ctx) == 0
    base_a, base_b = counts["a"], counts["b"]

    # 0 -> 5 both clamp to 1: both guards suppress identically.
    input(ctx).value = 5
    assert obs_a(ctx) == 1
    assert obs_b(ctx) == 1
    assert counts["a"] == base_a + 1
    assert counts["b"] == base_b + 1

    # 5 -> 9 both stay 1: both suppress the dependent.
    input(ctx).value = 9
    assert obs_a(ctx) == 1
    assert obs_b(ctx) == 1
    assert counts["a"] == base_a + 1, "computed suppressed equal recompute"
    assert counts["b"] == base_b + 1, "computed_ripple_when(!=) matches computed"


def test_pass_through_always_propagates() -> None:
    """``computed_ripple_when(f, lambda o, n: True)`` is the pass-through slot.

    It installs an always-propagate guard: even an equal recompute propagates,
    the ``slot(f)`` identity of the Cell kernel.
    """
    ctx: dict = {}
    input = source(lambda c: 0)

    # Depend on input, but always yield the same value.
    passthrough = computed_ripple_when(
        ctx,
        lambda c: (input(c).value, 0)[1],  # always 0, but reads input
        lambda old, new: True,  # always propagate
    ).eager()

    runs = {"n": 0}

    @Slot
    def observer(c: dict) -> int:
        runs["n"] += 1
        return passthrough.value

    assert observer(ctx) == 0
    base = runs["n"]

    # Value stays 0, but the guard always propagates, so the dependent re-fires.
    input(ctx).value = 5
    assert observer(ctx) == 0
    assert runs["n"] > base, (
        "pass-through always propagates even when the value is unchanged"
    )
