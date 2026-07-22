"""Thread-safe reactive context — batch-flush coalescing.

The Python counterpart of the Lean ``LazilyFormal.ThreadSafe`` formal model in
``lazily-formal``. Each test mirrors a named theorem (the empty-batch identity,
singleton-batch refinement of ``setCell``, the coalesced frontier, and
glitch-freedom for non-dependents). The pure ``flush_batch`` / ``apply_batch``
/ ``union_dependents`` kernels are exercised directly; the runtime
``ThreadSafeContext.batch`` is exercised through live ``Cell`` writes.
"""

from __future__ import annotations

from lazily import Cell, ThreadSafeContext


# =================================================================================
# flushBatch_empty — an empty batch flush is the identity.
# =================================================================================


def test_flush_batch_empty_is_identity() -> None:
    nodes = {0: (1, "clean"), 1: (2, "clean")}
    dependents = {0: [1]}
    out = ThreadSafeContext.flush_batch(nodes, dependents, [])
    assert out == nodes


# =================================================================================
# flushBatch_singleton_eq_setCell — a one-write batch refines setCell.
# =================================================================================


def test_flush_batch_singleton_eq_set_cell() -> None:
    nodes = {0: (1, "clean"), 1: (None, "clean")}
    dependents = {0: [1]}
    # One write of an equal value -> graph unchanged (PartialEq guard).
    out_eq = ThreadSafeContext.flush_batch(nodes, dependents, [(0, 1)])
    assert out_eq == nodes
    # One write of a different value -> source value updated, dependent dirty.
    out_diff = ThreadSafeContext.flush_batch(nodes, dependents, [(0, 5)])
    assert out_diff[0] == (5, "dirty")
    assert out_diff[1] == (None, "dirty")


# =================================================================================
# flushBatch_dependent_dirty — coalesced frontier (positive direction).
# After a batch flush, a dependent of any changed source is dirty.
# =================================================================================


def test_flush_batch_dependent_dirty() -> None:
    # 0 -> 2, 1 -> 2 (node 2 depends on both 0 and 1)
    nodes = {0: (1, "clean"), 1: (1, "clean"), 2: (None, "clean")}
    dependents = {0: [2], 1: [2]}
    out = ThreadSafeContext.flush_batch(nodes, dependents, [(0, 7), (1, 8)])
    # node 2 is a dependent of two changed sources -> dirty exactly once
    assert out[2] == (None, "dirty")
    assert out[0] == (7, "dirty")
    assert out[1] == (8, "dirty")


# =================================================================================
# flushBatch_preserves_nondependent_dirty — glitch-freedom.
# A node that is a dependent of no changed source keeps its dirty flag.
# =================================================================================


def test_flush_batch_preserves_nondependent() -> None:
    # 0 -> 1; node 2 is unrelated to the changed source 0.
    nodes = {0: (1, "clean"), 1: (None, "clean"), 2: (None, "clean")}
    dependents = {0: [1]}
    out = ThreadSafeContext.flush_batch(nodes, dependents, [(0, 9)])
    assert out[1] == (None, "dirty")  # dependent -> dirty
    assert out[2] == (None, "clean")  # non-dependent -> untouched (glitch-free)


def test_union_dependents() -> None:
    dependents = {0: [1, 2], 1: [2, 3]}
    out = ThreadSafeContext.union_dependents(dependents, [0, 1])
    assert out == [1, 2, 2, 3]  # flat union (dedup is a wire concern)


# =================================================================================
# Runtime: ThreadSafeContext.batch coalesces writes into one invalidation wave.
# =================================================================================


def test_batch_coalesces_into_one_invalidation() -> None:
    ctx: dict = {}
    src_a = Cell(ctx, 1)
    src_b = Cell(ctx, 1)
    runs = [0]

    # A downstream slot that depends on BOTH sources — without batching, two
    # set_cell calls would invalidate it twice; with batching, once.
    from lazily import Slot

    @Slot
    def downstream(_ctx: dict) -> int:
        runs[0] += 1
        return src_a.value + src_b.value

    assert downstream(ctx) == 2
    assert runs[0] == 1

    ts = ThreadSafeContext()
    ts.batch(lambda: (ts.set(src_a, 10), ts.set(src_b, 20)))
    assert downstream(ctx) == 30
    # The batch coalesced two writes into one invalidation wave: the downstream
    # slot recomputed exactly once (it had been invalidated once, then read once).
    assert runs[0] == 2


def test_batch_singleton_refines_set_cell() -> None:
    ctx: dict = {}
    src = Cell(ctx, 1)
    ts = ThreadSafeContext()
    ts.batch(lambda: ts.set(src, 2))
    assert src.value == 2


def test_outside_batch_applies_immediately() -> None:
    ctx: dict = {}
    src = Cell(ctx, 1)
    ts = ThreadSafeContext()
    ts.set(src, 5)  # outside a batch -> immediate
    assert src.value == 5


def test_equal_write_is_silent() -> None:
    ctx: dict = {}
    src = Cell(ctx, 1)
    runs = [0]

    from lazily import Slot

    @Slot
    def reader(_ctx: dict) -> int:
        runs[0] += 1
        return src.value

    assert reader(ctx) == 1
    n = runs[0]
    ts = ThreadSafeContext()
    ts.batch(lambda: ts.set(src, 1))  # equal value -> silent (PartialEq guard)
    assert reader(ctx) == 1
    assert runs[0] == n  # not invalidated
