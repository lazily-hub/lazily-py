"""``AsyncReactiveFamily`` tests (``#lzmatmode`` async flavor).

Mirrors the ``lazily-rs`` ``async_reactive_family.rs`` unit tests and the JS
``async-reactive-family`` suite. Exercises the same materialization laws as the
single-threaded family plus **eventual transparency** (proved in
``lazily-formal``'s ``AsyncMaterialization`` module): a non-blocking read of a
derived slot is ``None`` while pending and the canonical value once resolved
(``observe_pending_is_none`` / ``eventual_transparency`` /
``async_resolved_matches_sync``).
"""

from __future__ import annotations

import asyncio

from lazily import (
    AsyncReactiveFamily,
    Cell,
    EntryKind,
    MaterializationMode,
)


def test_eager_materializes_all_up_front() -> None:
    fam = AsyncReactiveFamily.eager({}, [0, 1, 2, 5, 9], lambda k: k * 3)
    assert fam.present_count() == 5
    assert all(fam.is_present(k) for k in (0, 1, 2, 5, 9))


def test_lazy_defers_slots_until_read() -> None:
    fam = AsyncReactiveFamily.lazy({}, [0, 1, 2, 5, 9], lambda k: k * 3)
    assert fam.present_count() == 0
    assert not fam.is_present(5)
    # A non-blocking observe materializes the entry but it is still pending.
    assert fam.observe(5) is None
    assert fam.is_present(5)
    assert fam.present_keys() == [5]


def test_observe_pending_is_none_then_resolves() -> None:
    async def scenario() -> None:
        fam = AsyncReactiveFamily.eager({}, [1, 2, 3], lambda k: k * 10)
        # Pending before drive: non-blocking observe is None (never a stale value).
        assert fam.observe(2) is None
        # Drive to resolution: canonical value.
        assert await fam.resolve(2) == 20
        # Eventual transparency: observe now equals the canonical value.
        assert fam.observe(2) == 20

    asyncio.run(scenario())


def test_async_resolved_matches_sync() -> None:
    async def scenario() -> None:
        keys = [0, 1, 2, 5, 9]
        fam = AsyncReactiveFamily.eager({}, keys, lambda k: k * 3)
        for k in keys:
            assert await fam.resolve(k) == k * 3

    asyncio.run(scenario())


def test_cell_family_is_always_resolved() -> None:
    async def scenario() -> None:
        fam = AsyncReactiveFamily.cell_family({}, ["a", "b"], lambda _k: 7)
        assert fam.entry_kind is EntryKind.CELL
        # Input cells resolve at build — observe is immediate, not None.
        assert fam.observe("a") == 7
        assert await fam.resolve("b") == 7

    asyncio.run(scenario())


def test_cell_family_materialized_in_every_mode() -> None:
    for mode in (MaterializationMode.EAGER, MaterializationMode.LAZY):
        fam = AsyncReactiveFamily.cell_family(
            {}, ["a", "b", "c"], lambda _k: 0, mode=mode
        )
        assert fam.present_count() == 3


def test_cell_family_entries_are_writable_inputs() -> None:
    fam = AsyncReactiveFamily.cell_family({}, [7], lambda k: k)
    handle = fam.get(7)
    assert isinstance(handle, Cell)
    fam.set_cell(7, 100)
    assert fam.observe(7) == 100


def test_present_set_is_monotone_across_reads() -> None:
    fam = AsyncReactiveFamily.lazy({}, [1, 2, 3, 4, 5], lambda k: k * 2)
    sizes = []
    for k in (2, 4, 2, 5):
        fam.observe(k)
        sizes.append(fam.present_count())
    assert sizes == [1, 2, 2, 3]
    assert fam.present_keys() == [2, 4, 5]


def test_stable_handle_across_repeated_get() -> None:
    fam = AsyncReactiveFamily.lazy({}, [], lambda k: k)
    assert fam.get(3) is fam.get(3)
