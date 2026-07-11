"""``ThreadSafeReactiveFamily`` tests (``#lzmatmode`` thread-safe flavor).

Mirrors the ``lazily-rs`` ``thread_safe_reactive_family.rs`` unit tests and the
JS ``thread-safe-reactive-family`` suite. Exercises the same materialization laws
as the single-threaded family (eager/lazy, present-set monotonicity,
transparency) plus **materialization confluence** (proved in ``lazily-formal``'s
``Materialization`` module as ``materialize_present_comm`` /
``materialize_observe_comm``): whatever order the lock admits concurrent
materializations in, the present set and every observed value are identical.
"""

from __future__ import annotations

import threading

from lazily import (
    Cell,
    EntryKind,
    MaterializationMode,
    ThreadSafeContext,
    ThreadSafeReactiveFamily,
    slot,
)


def test_eager_materializes_all_up_front() -> None:
    fam = ThreadSafeReactiveFamily.eager({}, [0, 1, 2, 5, 9], lambda k: k * 3)
    assert fam.present_count() == 5
    assert all(fam.is_present(k) for k in (0, 1, 2, 5, 9))


def test_lazy_defers_slots_until_read() -> None:
    fam = ThreadSafeReactiveFamily.lazy({}, [0, 1, 2, 5, 9], lambda k: k * 3)
    assert fam.present_count() == 0
    assert fam.observe(5) == 15
    assert fam.is_present(5)
    assert fam.present_keys() == [5]


def test_eager_and_lazy_observe_identically() -> None:
    ctx: dict = {}
    eager = ThreadSafeReactiveFamily.eager(ctx, [0, 1, 2, 5, 9], lambda k: k * 3)
    lazy = ThreadSafeReactiveFamily.lazy(ctx, [0, 1, 2, 5, 9], lambda k: k * 3)
    for k in (0, 1, 2, 5, 9):
        assert eager.observe(k) == lazy.observe(k)


def test_cell_family_materialized_in_every_mode() -> None:
    for mode in (MaterializationMode.EAGER, MaterializationMode.LAZY):
        fam = ThreadSafeReactiveFamily.cell_family(
            {}, ["a", "b", "c"], lambda _k: 0, mode=mode
        )
        assert fam.entry_kind is EntryKind.CELL
        assert fam.present_count() == 3


def test_cell_family_writes_through_coalescing_context() -> None:
    fam = ThreadSafeReactiveFamily.cell_family({}, [7], lambda k: k)
    assert fam.observe(7) == 7
    fam.set_cell(7, 100)  # routed through the ThreadSafeContext boundary
    assert fam.observe(7) == 100


def test_set_cell_on_slot_entry_raises() -> None:
    fam = ThreadSafeReactiveFamily.slot_family({}, [1], lambda k: k)
    try:
        fam.set_cell(1, 5)
    except TypeError as exc:
        assert "derived slot" in str(exc)
    else:  # pragma: no cover - the raise is the contract
        raise AssertionError("expected TypeError for slot set_cell")


def test_observe_is_reactive_when_factory_reads_a_cell() -> None:
    ctx: dict = {}
    src = Cell(ctx, 10)
    fam = ThreadSafeReactiveFamily.eager(ctx, [1], lambda k: src.value + k)
    seen: list[int] = []
    reader = slot(lambda c: fam.observe(1))
    watcher = slot(lambda c: seen.append(reader(c)))
    watcher(ctx)
    assert seen == [11]
    src.set(100)
    watcher(ctx)
    assert seen == [11, 101]


def test_stable_handle_across_repeated_get() -> None:
    fam = ThreadSafeReactiveFamily.lazy({}, [], lambda k: k * 2)
    h1 = fam.get(3)
    h2 = fam.get(3)
    assert h1 is h2  # first-writer-wins keeps one stable handle


def test_materialization_confluence_under_concurrent_reads() -> None:
    # Many threads materialize the same key space in different orders; the present
    # set (as a set) and every observed value MUST be identical regardless of the
    # interleaving the lock admits (materialize_present_comm / _observe_comm).
    keys = list(range(64))
    ctx: dict = {}
    fam = ThreadSafeReactiveFamily.lazy(
        ctx, [], lambda k: k * 7, ts=ThreadSafeContext()
    )
    handles: dict[int, list[object]] = {k: [] for k in keys}
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker(order: list[int]) -> None:
        barrier.wait()
        for k in order:
            h = fam.get(k)
            with lock:
                handles[k].append(h)

    threads = [
        threading.Thread(target=worker, args=(keys[::-1] if i % 2 else keys,))
        for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert set(fam.present_keys()) == set(keys)
    assert fam.present_count() == len(keys)
    # First-writer-wins: every thread observed the SAME handle object per key.
    for k in keys:
        first = handles[k][0]
        assert all(h is first for h in handles[k]), f"key {k} handle not stable"
    for k in keys:
        assert fam.observe(k) == k * 7
