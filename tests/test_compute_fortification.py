"""The fortified :class:`~lazily.compute.Compute` view is the sole tracking
surface (``#lzcellkernel``).

The Python mirror of ``lazily-rs`` ``tests/compute_fortification.rs``. These pin
the two halves of the fortification contract:

1. A **tracked** read through the :class:`~lazily.compute.Compute` handed to a
   compute/effect closure registers a dependency edge against the *recomputing
   node*, so a change to the dependency recomputes the dependent.
2. The explicit **untracked** escape (:meth:`~lazily.compute.Compute.untracked`)
   registers **no** edge, so the dependent neither gains a dependency nor
   recomputes.

The recomputing node is threaded as a *value* (``Compute.node``), not an ambient
module global, so the attribution is correct by construction — pinned directly
by :func:`test_value_threaded_attribution_beats_ambient_stack`.
"""

from __future__ import annotations

import pytest

from lazily import (
    Compute,
    Context,
    StaleComputeError,
    computed,
    source,
    tracked_effect,
)
from lazily.slot import Slot, slot_stack


def _tracked_computed(ctx, body):
    """A value-threaded 'computed': a source cell holding the derived value,
    driven by a :class:`ComputeEffect` puller — the shape an eager
    ``Computed`` uses (memo + scheduled puller). Returns ``(out, effect, calls)``
    where ``calls`` is a one-element run counter.
    """
    out = source(lambda _c: None)(ctx)
    calls = [0]

    def puller(compute):
        calls[0] += 1
        out.set(body(compute))

    eff = tracked_effect(puller)
    eff(ctx)
    return out, eff, calls


def test_tracked_read_registers_edge_against_the_recomputing_node():
    ctx: dict = {}
    a = source(lambda _c: 1)(ctx)

    # Tracked read: the edge must attribute to the puller node being recomputed,
    # not to any ambient frame.
    b, eff, calls = _tracked_computed(ctx, lambda c: c.read(a) * 10)

    assert b.get() == 10
    assert calls[0] == 1, "first read computes once"

    # Structural: the edge exists in both directions.
    assert a.dependent_count() == 1, "a must have the puller as its dependent"
    assert eff.dependency_count() == 1, "the puller must depend on a"

    # Behavioural: changing a recomputes b.
    a.set(5)
    assert b.get() == 50
    assert calls[0] == 2, "changing the tracked dependency recomputes b"


def test_untracked_read_registers_no_edge_and_does_not_recompute():
    ctx: dict = {}
    a = source(lambda _c: 1)(ctx)

    # The explicit untracked escape: read a through the owning Context, which
    # forms no dependency edge.
    d, eff, calls = _tracked_computed(ctx, lambda c: c.untracked().read(a) * 10)

    assert d.get() == 10
    assert calls[0] == 1

    # Structural: no edge was formed by the untracked read.
    assert a.dependent_count() == 0, "an untracked read must not register a dependent"
    assert eff.dependency_count() == 0, "d must have acquired no dependency"

    # Behavioural: changing a does NOT recompute d — its cached value stands.
    a.set(5)
    assert d.get() == 10, "untracked dependent keeps its stale value"
    assert calls[0] == 1, "untracked dependent never recomputes"


def test_effect_tracks_through_its_compute_view():
    ctx: dict = {}
    a = source(lambda _c: 1)(ctx)

    runs = [0]

    def body(c):
        runs[0] += 1
        c.read(a)

    watch = tracked_effect(body)
    watch(ctx)

    assert runs[0] == 1, "effect runs once on creation"
    assert a.dependent_count() == 1, "effect owns the edge to a"

    a.set(2)
    assert runs[0] == 2, "a change reruns the tracking effect"

    watch.dispose()
    assert a.dependent_count() == 0, "disposing the effect detaches its edge"


def test_value_threaded_attribution_beats_ambient_stack():
    """The strongest "not ambient" proof: with an unrelated node sitting on the
    legacy ``slot_stack``, a ``Compute.read`` still attributes the edge to the
    view's own node — because the node is a *value* the view carries, never
    ``slot_stack[-1]``.
    """
    ctx: dict = {}
    a = source(lambda _c: 1)(ctx)
    decoy = source(lambda _c: 0)(ctx)  # a stand-in "ambient current node"

    real_node = tracked_effect(lambda _c: None)
    view = Compute(ctx, real_node)

    slot_stack.append(decoy)  # ambient carrier points at the WRONG node
    try:
        assert view.read(a) == 1
    finally:
        slot_stack.pop()

    assert real_node in (a._parents or set()), "edge attributes to the view's node"
    assert decoy not in (a._parents or set()), "edge must NOT attribute to the stack"
    assert a.dependent_count() == 1


def test_stale_compute_view_read_raises():
    """Non-escapability (runtime guard): a view stored past its recompute is
    closed, and reading through it raises rather than silently registering an
    edge against the finished node.
    """
    ctx: dict = {}
    a = source(lambda _c: 1)(ctx)

    escaped: list[Compute] = []

    def body(c):
        escaped.append(c)  # smuggle the view out of the recompute
        c.read(a)

    tracked_effect(body)(ctx)

    stale = escaped[0]
    with pytest.raises(StaleComputeError):
        stale.read(a)
    with pytest.raises(StaleComputeError):
        stale.set(a, 9)


def test_untracked_escape_is_a_context():
    ctx: dict = {}
    a = source(lambda _c: 7)(ctx)
    view = Compute(ctx, tracked_effect(lambda _c: None))
    escape = view.untracked()
    assert isinstance(escape, Context)
    assert escape.read(a) == 7
    assert a.dependent_count() == 0, "reading through the Context forms no edge"


# --- The Compute surface subscribes correctly across *every* node kind -------
#
# For Compute to be the sole tracking surface it must establish a live
# subscription no matter what kind of node is read through it — not only a
# ``Source`` cell (which the tests above cover). A **lazy** ``Computed`` is the
# one that regressed: it holds no settled value and never ``touch``es on its own,
# so its live upstream edges are on its backing memo. ``Compute.read`` must
# subscribe the reader to that memo, or an upstream change silently never reaches
# the reader (the ambient ``.value`` path never had this gap because it pushes
# the reader onto ``slot_stack`` *through* the memo read).
@pytest.mark.parametrize(
    ("kind", "build"),
    [
        ("source_cell", lambda ctx, a: a),
        ("lazy_computed", lambda ctx, a: computed(ctx, lambda c: a.get() * 10)),
        ("eager_computed", lambda ctx, a: computed(ctx, lambda c: a.get() * 10).eager()),
        ("raw_slot", lambda ctx, a: Slot(callable=lambda c: a.get() * 10)),
        (
            "chained_lazy",
            lambda ctx, a: computed(
                ctx, lambda c: computed(ctx, lambda d: a.get() + 1).get() * 10
            ),
        ),
    ],
)
def test_compute_read_subscribes_across_node_kinds(kind, build):
    ctx: dict = {}
    a = source(lambda _c: 1)(ctx)
    node = build(ctx, a)

    runs: list[object] = []
    eff = tracked_effect(lambda c: runs.append(c.read(node)))
    eff(ctx)
    assert len(runs) == 1, f"{kind}: effect runs once on creation"

    # Behavioural: an upstream change must rerun the effect for EVERY node kind.
    a.set(7)
    assert len(runs) == 2, (
        f"{kind}: a Compute.read subscription must survive an upstream change"
    )

    eff.dispose()
    a.set(9)
    assert len(runs) == 2, f"{kind}: a disposed effect re-runs no more"
