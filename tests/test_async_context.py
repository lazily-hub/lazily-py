"""``AsyncContext`` conformance tests (``lazily-spec/docs/async.md``).

Each test names the conformance point from ``async.md`` § "Conformance" that it
pins. There are no JSON fixtures for the async surface in ``lazily-spec``
(``conformance/`` has no async directory), so — as ``async.md`` § "Implementation
status" itself prescribes — coverage is "targeted deterministic tests rather than
exhaustive interleaving exploration". These mirror the shape of ``lazily-rs``'s
``async_integration.rs`` / ``async_state_machine.rs`` / ``async_resolve_loop.rs``.
"""

from __future__ import annotations

import asyncio

import pytest

from lazily import (
    AsyncCellHandle,
    AsyncComputeContext,
    AsyncContext,
    AsyncContextDisposedError,
    AsyncSlotHandle,
    EffectState,
    SlotState,
)


async def _settle() -> None:
    """Yield to the loop enough times for spawned drive/flush tasks to advance
    to their next suspension point."""
    for _ in range(8):
        await asyncio.sleep(0)


# --------------------------------------------------------------------------- #
# Point 1 — the slot state machine and its transitions
# --------------------------------------------------------------------------- #


def test_slot_starts_empty_and_resolves() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        slot = ctx.computed_async(lambda cc: _const(7))
        assert slot.state is SlotState.EMPTY
        assert slot.get() is None
        assert await slot.get_async() == 7
        assert slot.state is SlotState.RESOLVED
        # Warm fast path: the cached read no longer spawns.
        assert slot.get() == 7

    asyncio.run(scenario())


async def _const[T](value: T) -> T:
    return value


def test_error_state_then_retry_transitions_back_to_computing() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        attempts: list[int] = []

        async def compute(cc: AsyncComputeContext) -> int:
            attempts.append(len(attempts))
            if len(attempts) == 1:
                raise ValueError("boom")
            return 42

        slot = ctx.computed_async(compute)
        with pytest.raises(ValueError, match="boom"):
            await slot.get_async()
        assert slot.state is SlotState.ERROR
        # Error -> Computing on the next get_async (retry).
        assert await slot.get_async() == 42
        assert slot.state is SlotState.RESOLVED

    asyncio.run(scenario())


def test_invalidation_moves_resolved_back_to_computing() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        src = ctx.cell(2)
        slot = ctx.computed_async(lambda cc: _const(cc.get_cell(src) * 10))
        assert await slot.get_async() == 20
        assert slot.state is SlotState.RESOLVED
        ctx.set_cell(src, 3)
        assert slot.state is SlotState.COMPUTING
        assert slot.get() is None  # never a stale cached value
        assert await slot.get_async() == 30

    asyncio.run(scenario())


def test_equal_write_is_guarded_and_does_not_invalidate() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        src = ctx.cell(2)
        runs: list[int] = []

        async def compute(cc: AsyncComputeContext) -> int:
            v = cc.get_cell(src)
            runs.append(v)
            return v

        slot = ctx.computed_async(compute)
        assert await slot.get_async() == 2
        ctx.set_cell(src, 2)  # equal write — PartialEq guard
        assert slot.state is SlotState.RESOLVED
        assert runs == [2]

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# Point 2 — revision tracking discards every stale completion
# --------------------------------------------------------------------------- #


def test_stale_completion_is_discarded_not_published() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        src = ctx.cell(1)
        gate = asyncio.Event()
        seen: list[int] = []
        published: list[int | None] = []

        async def compute(cc: AsyncComputeContext) -> int:
            v = cc.get_cell(src)
            seen.append(v)
            if v == 1:
                await gate.wait()
            return v * 10

        slot = ctx.computed_async(compute)
        task = asyncio.create_task(slot.get_async())
        await _settle()
        assert seen == [1]  # suspended mid-compute

        # Invalidate while the first computation is in flight.
        ctx.set_cell(src, 2)
        assert slot.revision == 1
        gate.set()  # the stale computation now completes with 10
        await _settle()
        published.append(slot.get())

        assert await task == 20  # the fresh revision's value, never 10
        assert seen == [1, 2]
        # The stale 10 was never published at any point.
        assert 10 not in [p for p in published if p is not None]
        assert slot.get() == 20

    asyncio.run(scenario())


def test_stale_error_is_discarded_and_the_slot_re_resolves() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        src = ctx.cell(1)
        gate = asyncio.Event()

        async def compute(cc: AsyncComputeContext) -> int:
            v = cc.get_cell(src)
            if v == 1:
                await gate.wait()
                raise ValueError("stale failure")
            return v

        slot = ctx.computed_async(compute)
        task = asyncio.create_task(slot.get_async())
        await _settle()
        ctx.set_cell(src, 9)
        gate.set()
        # The stale error is discarded — it must not surface to the waiter.
        assert await task == 9
        assert slot.state is SlotState.RESOLVED

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# Point 3 — the five cancellation properties
# --------------------------------------------------------------------------- #


def test_dropping_one_waiter_does_not_cancel_the_shared_computation() -> None:
    """Cancellation property 1 — waiter cancellation is safe."""

    async def scenario() -> None:
        ctx = AsyncContext()
        gate = asyncio.Event()
        runs: list[int] = []

        async def compute(cc: AsyncComputeContext) -> int:
            runs.append(1)
            await gate.wait()
            return 5

        slot = ctx.computed_async(compute)
        a = asyncio.create_task(slot.get_async())
        b = asyncio.create_task(slot.get_async())
        await _settle()
        a.cancel()  # drop one waiter
        await _settle()
        gate.set()
        assert await b == 5  # the surviving waiter still gets the value
        assert runs == [1]  # in-flight deduplication: computed exactly once

    asyncio.run(scenario())


def test_concurrent_get_async_callers_share_one_in_flight_computation() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        gate = asyncio.Event()
        runs: list[int] = []

        async def compute(cc: AsyncComputeContext) -> int:
            runs.append(1)
            await gate.wait()
            return 3

        slot = ctx.computed_async(compute)
        waiters = [asyncio.create_task(slot.get_async()) for _ in range(5)]
        await _settle()
        gate.set()
        assert await asyncio.gather(*waiters) == [3, 3, 3, 3, 3]
        assert runs == [1]

    asyncio.run(scenario())


def test_hard_clear_cancels_the_in_flight_revision() -> None:
    """Cancellation property 3 — explicit cancellation."""

    async def scenario() -> None:
        ctx = AsyncContext()
        gate = asyncio.Event()
        values = iter([100, 200])

        async def compute(cc: AsyncComputeContext) -> int:
            v = next(values)
            if v == 100:
                await gate.wait()
            return v

        slot = ctx.computed_async(compute)
        task = asyncio.create_task(slot.get_async())
        await _settle()
        slot.hard_clear()
        assert slot.state is SlotState.EMPTY
        gate.set()
        assert await task == 200  # the cleared revision's 100 was discarded

    asyncio.run(scenario())


def test_context_disposal_awaits_cleanup_and_makes_reads_inert() -> None:
    """Cancellation property 4 — context disposal."""

    async def scenario() -> None:
        ctx = AsyncContext()
        order: list[str] = []

        async def body(cc: AsyncComputeContext) -> object:
            order.append("body")

            async def cleanup() -> None:
                await asyncio.sleep(0)
                order.append("cleanup")

            return cleanup

        effect = ctx.effect_async(body)
        await effect.settle()
        assert order == ["body", "cleanup"]

        slot = ctx.computed_async(lambda cc: _const(1))
        await ctx.dispose_async()
        # Disposal awaited every active cleanup future before returning.
        assert order == ["body", "cleanup"]
        assert effect.state is EffectState.DISPOSED
        assert ctx.disposed
        with pytest.raises(AsyncContextDisposedError):
            await slot.get_async()

    asyncio.run(scenario())


def test_disposal_cancels_in_flight_computations() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        gate = asyncio.Event()

        async def compute(cc: AsyncComputeContext) -> int:
            await gate.wait()
            return 1

        slot = ctx.computed_async(compute)
        task = asyncio.create_task(slot.get_async())
        await _settle()
        await ctx.dispose_async()
        gate.set()
        # The in-flight revision was hard-cleared, so its completion is
        # discarded; the re-resolving waiter sees the disposed context.
        with pytest.raises(AsyncContextDisposedError):
            await task

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# Point 4 — the get_async re-resolve contract
# --------------------------------------------------------------------------- #


def test_resolved_since_get_window_is_benign() -> None:
    """Benign window 1 — the slot may transition Computing -> Resolved between
    the fast-path read and the re-lock. Re-resolving must return the published
    value, not panic."""

    async def scenario() -> None:
        ctx = AsyncContext()
        gate = asyncio.Event()

        async def compute(cc: AsyncComputeContext) -> int:
            await gate.wait()
            return 11

        slot = ctx.computed_async(compute)
        first = asyncio.create_task(slot.get_async())
        await _settle()
        gate.set()
        assert await first == 11
        # A caller entering after publication takes the cached path.
        assert await slot.get_async() == 11

    asyncio.run(scenario())


def test_repeated_invalidation_during_compute_re_resolves_without_error() -> None:
    """Benign window 2 — repeatedly superseding the in-flight compute must loop,
    not raise."""

    async def scenario() -> None:
        ctx = AsyncContext()
        src = ctx.cell(0)
        gates: dict[int, asyncio.Event] = {}

        async def compute(cc: AsyncComputeContext) -> int:
            v = cc.get_cell(src)
            gate = gates.setdefault(v, asyncio.Event())
            if v < 3:
                await gate.wait()
            return v

        slot = ctx.computed_async(compute)
        task = asyncio.create_task(slot.get_async())
        for nxt in (1, 2, 3):
            await _settle()
            ctx.set_cell(src, nxt)
            for g in gates.values():
                g.set()
        assert await task == 3

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# Point 5 — dependency tracking through the compute context
# --------------------------------------------------------------------------- #


def test_edges_register_before_the_awaited_read() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        src = ctx.cell(1)
        gate = asyncio.Event()

        async def compute(cc: AsyncComputeContext) -> int:
            v = cc.get_cell(src)
            await gate.wait()
            return v

        slot = ctx.computed_async(compute)
        task = asyncio.create_task(slot.get_async())
        await _settle()
        # Suspended at the await — the edge is already live, so a write now
        # supersedes the in-flight computation rather than being missed.
        ctx.set_cell(src, 2)
        assert slot.revision == 1
        gate.set()
        assert await task == 2

    asyncio.run(scenario())


def test_invalidation_propagates_through_the_transitive_cone() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        src = ctx.cell(2)
        a = ctx.computed_async(lambda cc: _const(cc.get_cell(src) + 1))
        b = ctx.computed_async(lambda cc: cc.get_async(a))
        assert await b.get_async() == 3
        ctx.set_cell(src, 10)
        # src -> a -> b: the whole cone is stale, not just the direct dependent.
        assert a.state is SlotState.COMPUTING
        assert b.state is SlotState.COMPUTING
        assert await b.get_async() == 11

    asyncio.run(scenario())


def test_stale_dependencies_are_detached_on_rerun() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        which = ctx.cell(True)
        left = ctx.cell(1)
        right = ctx.cell(100)
        runs: list[int] = []

        async def compute(cc: AsyncComputeContext) -> int:
            flag = cc.get_cell(which)
            v = cc.get_cell(left if flag else right)
            runs.append(v)
            return v

        slot = ctx.computed_async(compute)
        assert await slot.get_async() == 1
        ctx.set_cell(which, False)
        assert await slot.get_async() == 100
        # `left` is no longer a dependency — writing it must not invalidate.
        ctx.set_cell(left, 999)
        assert slot.state is SlotState.RESOLVED
        assert runs == [1, 100]

    asyncio.run(scenario())


def test_memo_guard_republishes_the_previous_value_object() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        src = ctx.cell(1)
        slot = ctx.memo_async(
            lambda cc: _const([cc.get_cell(src) % 2]),
            lambda a, b: a == b,
        )
        first = await slot.get_async()
        ctx.set_cell(src, 3)  # 3 % 2 == 1 — equal under the memo guard
        again = await slot.get_async()
        assert again == [1]
        assert again is first  # identity preserved: nothing new published

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# Point 6 — async effects: serialized, cleanup-before-body, executor-scheduled
# --------------------------------------------------------------------------- #


def test_effect_reruns_are_scheduled_not_inline() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        src = ctx.cell(1)
        runs: list[int] = []

        async def body(cc: AsyncComputeContext) -> None:
            runs.append(cc.get_cell(src))

        effect = ctx.effect_async(body)
        assert runs == []  # the initial run is scheduled, not inline
        await effect.settle()
        assert runs == [1]

        ctx.set_cell(src, 2)
        assert runs == [1]  # still not inline within set_cell
        await effect.settle()
        assert runs == [1, 2]

    asyncio.run(scenario())


def test_effect_cleanup_completes_before_the_next_body() -> None:
    """Cancellation property 5 — cleanup before next body."""

    async def scenario() -> None:
        ctx = AsyncContext()
        src = ctx.cell(1)
        order: list[str] = []

        async def body(cc: AsyncComputeContext) -> object:
            v = cc.get_cell(src)
            order.append(f"body{v}")

            async def cleanup() -> None:
                await asyncio.sleep(0)
                order.append(f"cleanup{v}")

            return cleanup

        effect = ctx.effect_async(body)
        await effect.settle()
        # The delegated AsyncEffect awaits the cleanup at the end of each flush
        # when no rerun is queued (pinned by test_async_effect_cleanup_before_body),
        # which is strictly stronger than "before the next body".
        assert order == ["body1", "cleanup1"]
        ctx.set_cell(src, 2)
        await effect.settle()
        assert order == ["body1", "cleanup1", "body2", "cleanup2"]
        # Every cleanup has already completed, so disposal adds nothing.
        await effect.dispose_async()
        assert order == ["body1", "cleanup1", "body2", "cleanup2"]
        assert effect.state is EffectState.DISPOSED

    asyncio.run(scenario())


def test_effect_accepts_a_synchronous_cleanup() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        order: list[str] = []

        async def body(cc: AsyncComputeContext) -> object:
            order.append("body")
            return lambda: order.append("cleanup")

        effect = ctx.effect_async(body)
        await effect.settle()
        await effect.dispose_async()
        assert order == ["body", "cleanup"]

    asyncio.run(scenario())


def test_effect_reruns_are_serialized_never_overlapping() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        src = ctx.cell(0)
        depth = [0]
        max_depth = [0]

        async def body(cc: AsyncComputeContext) -> None:
            cc.get_cell(src)
            depth[0] += 1
            max_depth[0] = max(max_depth[0], depth[0])
            await asyncio.sleep(0)
            depth[0] -= 1

        effect = ctx.effect_async(body)
        for i in range(1, 5):
            ctx.set_cell(src, i)
        await effect.settle()
        assert max_depth[0] == 1  # bodies never overlap

    asyncio.run(scenario())


def test_disposed_effect_does_not_rerun() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        src = ctx.cell(1)
        runs: list[int] = []

        async def body(cc: AsyncComputeContext) -> None:
            runs.append(cc.get_cell(src))

        effect = ctx.effect_async(body)
        await effect.settle()
        await ctx.dispose_async_effect(effect)
        ctx.set_cell(src, 2)
        await _settle()
        assert runs == [1]

    asyncio.run(scenario())


def test_effect_body_error_does_not_wedge_the_effect() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        src = ctx.cell(1)
        runs: list[int] = []

        async def body(cc: AsyncComputeContext) -> None:
            v = cc.get_cell(src)
            runs.append(v)
            if v == 1:
                raise ValueError("boom")

        effect = ctx.effect_async(body)
        await effect.settle()
        ctx.set_cell(src, 2)
        await effect.settle()
        assert runs == [1, 2]

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# Point 7 — batching is synchronous; async reruns fire after the outermost exit
# --------------------------------------------------------------------------- #


def test_batch_coalesces_into_one_effect_rerun() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        a = ctx.cell(1)
        b = ctx.cell(1)
        runs: list[tuple[int, int]] = []

        async def body(cc: AsyncComputeContext) -> None:
            runs.append((cc.get_cell(a), cc.get_cell(b)))

        effect = ctx.effect_async(body)
        await effect.settle()
        assert runs == [(1, 1)]

        def writes() -> None:
            ctx.set_cell(a, 2)
            ctx.set_cell(b, 3)
            assert runs == [(1, 1)]  # nothing runs inside the batch callback

        ctx.batch(writes)
        await effect.settle()
        # One coalesced rerun observing both writes, not two.
        assert runs == [(1, 1), (2, 3)]

    asyncio.run(scenario())


def test_nested_batches_flush_only_at_the_outermost_boundary() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        src = ctx.cell(1)
        runs: list[int] = []

        async def body(cc: AsyncComputeContext) -> None:
            runs.append(cc.get_cell(src))

        effect = ctx.effect_async(body)
        await effect.settle()

        def inner() -> None:
            ctx.set_cell(src, 2)

        def outer() -> None:
            ctx.batch(inner)
            assert runs == [1]  # the inner exit did not flush
            ctx.set_cell(src, 3)

        ctx.batch(outer)
        await effect.settle()
        assert runs == [1, 3]

    asyncio.run(scenario())


def test_batch_returns_the_callback_value() -> None:
    ctx = AsyncContext()
    assert ctx.batch(lambda: "done") == "done"


def test_batched_slot_invalidation_defers_to_the_outermost_exit() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        src = ctx.cell(1)
        slot = ctx.computed_async(lambda cc: _const(cc.get_cell(src)))
        assert await slot.get_async() == 1

        def writes() -> None:
            ctx.set_cell(src, 5)
            # Invalidation is queued, not applied, inside the batch.
            assert slot.state is SlotState.RESOLVED

        ctx.batch(writes)
        assert slot.state is SlotState.COMPUTING
        assert await slot.get_async() == 5

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# Public API surface
# --------------------------------------------------------------------------- #


def test_context_api_surface_matches_the_spec_table() -> None:
    for name in (
        "cell",
        "get_cell",
        "set_cell",
        "computed_async",
        "get",
        "get_async",
        "memo_async",
        "effect_async",
        "dispose_async_effect",
        "dispose_async",
        "batch",
    ):
        assert callable(getattr(AsyncContext, name)), name


def test_handles_are_exported_from_the_package_root() -> None:
    import lazily

    for name in (
        "AsyncContext",
        "AsyncCellHandle",
        "AsyncSlotHandle",
        "AsyncEffectHandle",
        "AsyncComputeContext",
        "AsyncContextDisposedError",
    ):
        assert name in lazily.__all__, name
        assert hasattr(lazily, name), name


def test_cell_and_slot_handle_types_are_usable_in_annotations() -> None:
    ctx = AsyncContext()
    c: AsyncCellHandle[int] = ctx.cell(1)
    s: AsyncSlotHandle[int] = ctx.computed_async(lambda cc: _const(2))
    assert c.get() == 1
    assert c.peek == 1
    assert repr(c) == "AsyncCellHandle(1)"
    assert s.get() is None
