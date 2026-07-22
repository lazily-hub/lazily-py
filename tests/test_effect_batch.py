"""Tests for the sync Effect and the top-level batch boundary.

Mirrors the named invariants from ``lazily-spec/docs/reactive-graph.md`` § "The
reactive family" (Effect) and § "API surface" (``batch(run)``).
"""

from __future__ import annotations

from lazily import (
    CellSlot,
    Effect,
    Slot,
    batch,
    batch_context,
    effect,
    in_batch,
)


# ---------------------------------------------------------------------------
# Effect
# ---------------------------------------------------------------------------


def test_effect_runs_once_on_start() -> None:
    ctx: dict = {}
    name = CellSlot[dict, dict, str]()
    name(ctx).value = "World"
    runs: list[str] = []

    @effect
    def greet(c: dict) -> None:
        runs.append(f"Hello, {name(c).value}!")

    eff = greet  # the decorator returns the Effect instance
    assert isinstance(eff, Effect)
    eff(ctx)  # start the body
    assert runs == ["Hello, World!"]


def test_effect_reruns_on_dependency_change() -> None:
    ctx: dict = {}
    name = CellSlot[dict, dict, str]()
    name(ctx).value = "World"
    runs: list[str] = []

    @effect
    def greet(c: dict) -> None:
        runs.append(name(c).value)

    greet(ctx)
    name(ctx).value = "Lazily"
    name(ctx).value = "Reactive"
    assert runs == ["World", "Lazily", "Reactive"]


def test_effect_cleanup_runs_before_next_body() -> None:
    ctx: dict = {}
    name = CellSlot[dict, dict, str]()
    name(ctx).value = "a"
    cleanups: list[str] = []

    @effect
    def with_cleanup(c: dict):
        val = name(c).value

        def cleanup() -> None:
            cleanups.append(val)

        return cleanup

    with_cleanup(ctx)
    name(ctx).value = "b"
    name(ctx).value = "c"
    # cleanup for "a" ran before the "b" body; cleanup for "b" before "c".
    assert cleanups == ["a", "b"]


def test_effect_dispose_is_terminal_and_runs_cleanup() -> None:
    ctx: dict = {}
    name = CellSlot[dict, dict, str]()
    name(ctx).value = "x"
    cleanups: list[str] = []

    @effect
    def with_cleanup(c: dict):
        val = name(c).value

        def cleanup() -> None:
            cleanups.append(val)

        return cleanup

    eff = with_cleanup
    eff(ctx)
    eff.dispose()
    assert eff.disposed
    assert cleanups == ["x"]
    name(ctx).value = "y"
    assert cleanups == ["x"]  # no rerun after dispose


def test_effect_reentrant_invalidation_is_suppressed() -> None:
    ctx: dict = {}
    ctr = CellSlot[dict, dict, int]()
    ctr(ctx).value = 0
    runs: list[int] = []

    @effect
    def watch(c: dict) -> None:
        runs.append(ctr(c).value)

    watch(ctx)
    assert runs == [0]
    # Setting the same value is a no-op (PartialEq guard).
    ctr(ctx).value = 0
    assert runs == [0]
    # A real change triggers exactly one rerun.
    ctr(ctx).value = 5
    assert runs == [0, 5]


def test_effect_tracks_slot_dependencies() -> None:
    ctx: dict = {}
    src = CellSlot[dict, dict, int]()
    src(ctx).value = 2

    @Slot
    def doubled(c: dict) -> int:
        return src(c).value * 2

    runs: list[int] = []

    @effect
    def watch(c: dict) -> None:
        runs.append(doubled(c))

    watch(ctx)
    assert runs == [4]
    src(ctx).value = 10
    assert runs == [4, 20]


# ---------------------------------------------------------------------------
# batch
# ---------------------------------------------------------------------------


def test_batch_coalesces_writes_into_one_invalidation() -> None:
    ctx: dict = {}
    ctr = CellSlot[dict, dict, int]()
    ctr(ctx).value = 0
    runs: list[int] = []

    @effect
    def watch(c: dict) -> None:
        runs.append(ctr(c).value)

    watch(ctx)
    assert runs == [0]

    def writes() -> None:
        ctr(ctx).value = 1
        ctr(ctx).value = 2
        ctr(ctx).value = 3

    batch(writes)
    # One coalesced rerun seeing the final value 3.
    assert runs == [0, 3]


def test_batch_context_manager_form() -> None:
    ctx: dict = {}
    ctr = CellSlot[dict, dict, int]()
    ctr(ctx).value = 0
    runs: list[int] = []

    @effect
    def watch(c: dict) -> None:
        runs.append(ctr(c).value)

    watch(ctx)
    with batch_context():
        assert in_batch()
        ctr(ctx).value = 7
        ctr(ctx).value = 9
        # No rerun yet inside the batch.
        assert runs == [0]
    assert not in_batch()
    assert runs == [0, 9]


def test_batch_nested_only_flushes_at_outermost() -> None:
    ctx: dict = {}
    ctr = CellSlot[dict, dict, int]()
    ctr(ctx).value = 0
    runs: list[int] = []

    @effect
    def watch(c: dict) -> None:
        runs.append(ctr(c).value)

    watch(ctx)

    def outer() -> None:
        ctr(ctx).value = 1

        def inner() -> None:
            ctr(ctx).value = 2

        batch(inner)
        # Still deferred: the inner flush is suppressed.
        assert runs == [0]

    batch(outer)
    assert runs == [0, 2]


def test_batch_singleton_refines_set_cell() -> None:
    ctx: dict = {}
    ctr = CellSlot[dict, dict, int]()
    ctr(ctx).value = 0
    runs: list[int] = []

    @effect
    def watch(c: dict) -> None:
        runs.append(ctr(c).value)

    watch(ctx)

    def single() -> None:
        ctr(ctx).value = 42

    batch(single)
    # A one-write batch is observationally identical to a plain Cell.set.
    assert runs == [0, 42]


def test_batch_equal_write_is_silent() -> None:
    ctx: dict = {}
    ctr = CellSlot[dict, dict, int]()
    ctr(ctx).value = 5
    runs: list[int] = []

    @effect
    def watch(c: dict) -> None:
        runs.append(ctr(c).value)

    watch(ctx)

    def noop() -> None:
        ctr(ctx).value = 5  # equal → PartialEq guard → no invalidation

    batch(noop)
    assert runs == [5]


def test_batch_multiple_cells_one_dependent_fires_once() -> None:
    ctx: dict = {}
    a = CellSlot[dict, dict, int]()
    b = CellSlot[dict, dict, int]()
    a(ctx).value = 1
    b(ctx).value = 1

    @Slot
    def sum_ab(c: dict) -> int:
        return a(c).value + b(c).value

    runs: list[int] = []

    @effect
    def watch(c: dict) -> None:
        runs.append(sum_ab(c))

    watch(ctx)
    assert runs == [2]

    def writes() -> None:
        a(ctx).value = 10
        b(ctx).value = 20

    batch(writes)
    # The dependent reads both cells — coalesced into one rerun per batch.
    assert runs == [2, 30]


def test_in_batch_reflects_state() -> None:
    assert not in_batch()
    with batch_context():
        assert in_batch()
    assert not in_batch()
