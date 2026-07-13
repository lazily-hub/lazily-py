"""RelayCell Phases 2-6 spike (#relaycell) for the Python binding.

The doc §8 mandate is that Python converges identically to lazily-rs — these
prove the operational invariants: ``relay_converges``, ``transport_independent``,
``spill_lossless``, ``spill_replay_idempotent``, plus overflow behaviour, roles,
and the Phase-6 policies. Mirrors ``lazily-js/test/relay.test.js`` and
``lazily-kt`` ``RelayTest.kt``.
"""

from __future__ import annotations

from functools import reduce

import pytest

from lazily import KeepLatest, Max, RawFifo, Sum
from lazily.relay import (
    BackpressurePolicy,
    BoundDim,
    ExpiryPolicy,
    FramedTransport,
    Inbox,
    IngressOutcome,
    InProcTransport,
    KeyedRelay,
    Outbox,
    Overflow,
    PriorityStorage,
    RatePolicy,
    RelayCell,
    RelayConfigError,
    SpillMode,
    SpillStore,
    WindowPolicy,
)


def _relay(ctx, policy, *, high_water=1_000_000, overflow=Overflow.CONFLATE):
    return RelayCell(
        ctx,
        BackpressurePolicy(ctx, BoundDim.COUNT, high_water, high_water // 2, overflow),
        policy,
    )


# -- Phase 2 -----------------------------------------------------------------


def test_converged_egress_independent_of_drain_schedule() -> None:
    for policy in (Sum, Max):
        ops = [3, 1, 4, 1, 5, 9, 2, 6]
        flat = reduce(policy.merge, ops)

        ctx_a: dict = {}
        r_a = _relay(ctx_a, policy)
        acc_a = None
        for op in ops:
            r_a.ingress(op)
            d = r_a.drain()
            if d is None:
                continue
            acc_a = d if acc_a is None else policy.merge(acc_a, d)
        assert acc_a == flat, f"{policy.name}: drain-every"

        ctx_b: dict = {}
        r_b = _relay(ctx_b, policy)
        for op in ops:
            r_b.ingress(op)
        assert r_b.drain() == flat, f"{policy.name}: drain-once"


def test_reactive_depth_is_full_is_empty() -> None:
    ctx: dict = {}
    r = _relay(ctx, Sum, high_water=3)
    assert r.is_empty() is True
    assert r.depth() == 0
    assert r.is_full() is False

    r.ingress(1)
    r.ingress(1)
    assert r.is_empty() is False
    assert r.depth() == 2
    assert r.is_full() is False

    r.ingress(1)
    assert r.depth() == 3
    assert r.is_full() is True

    r.drain()
    assert r.is_empty() is True
    assert r.depth() == 0


def test_reactive_readers_track_via_effect() -> None:
    """The readers are demand-driven slots — an effect over ``is_full`` reruns on
    the reactive frontier, not on a poll."""
    from lazily import effect

    ctx: dict = {}
    r = _relay(ctx, Sum, high_water=2)
    seen: list[bool] = []

    @effect
    def watch(ctx) -> None:
        seen.append(r.is_full())

    watch(ctx)
    assert seen == [False]
    r.ingress(1)
    r.ingress(1)  # crosses high_water → is_full flips True
    assert seen[-1] is True
    r.drain()  # window emptied → is_full flips False
    assert seen[-1] is False


def test_block_overflow_refuses_ingress() -> None:
    ctx: dict = {}
    r = _relay(ctx, Sum, high_water=2, overflow=Overflow.BLOCK)
    assert r.ingress(1) == IngressOutcome.ACCEPTED
    assert r.ingress(1) == IngressOutcome.CONFLATED
    assert r.ingress(1) == IngressOutcome.BLOCKED
    assert r.drain() == 2


def test_drop_newest_and_drop_oldest() -> None:
    ctx_n: dict = {}
    rn = _relay(ctx_n, Sum, high_water=2, overflow=Overflow.DROP_NEWEST)
    rn.ingress(1)
    rn.ingress(1)
    assert rn.ingress(9) == IngressOutcome.DROPPED
    assert rn.drain() == 2

    ctx_o: dict = {}
    ro = _relay(ctx_o, Sum, high_water=2, overflow=Overflow.DROP_OLDEST)
    ro.ingress(1)
    ro.ingress(1)
    assert ro.ingress(9) == IngressOutcome.DROPPED
    assert ro.drain() == 9


def test_construction_rejects_conflate_for_raw_fifo() -> None:
    ctx: dict = {}
    with pytest.raises(RelayConfigError):
        RelayCell(
            ctx,
            BackpressurePolicy(ctx, BoundDim.COUNT, 4, 2, Overflow.CONFLATE),
            RawFifo,
        )


def test_overflow_is_legal_runtime_guard() -> None:
    ctx: dict = {}
    r = _relay(ctx, Sum, overflow=Overflow.BLOCK)
    assert r.overflow_is_legal() is True
    r.policy.overflow.set(Overflow.CONFLATE)
    assert r.overflow_is_legal() is True  # Sum conflates


# -- Phase 3 -----------------------------------------------------------------


def test_spill_lossless_both_modes() -> None:
    for mode in (SpillMode.COMPACT_ON_WRITE, SpillMode.APPEND_COMPACT):
        store: SpillStore[int] = SpillStore(mode, 2, Sum)
        windows = [1, 2, 3, 4, 5]
        for w in windows:
            store.spill(w, 1)
        hot = 10
        flat = sum([*windows, hot])
        assert store.reconstruct(0, hot) == flat, mode


def test_spill_replay_idempotent_for_idempotent_policy() -> None:
    store: SpillStore[int] = SpillStore(SpillMode.APPEND_COMPACT, 1, Max)
    for w in (3, 7, 5):
        store.spill(w, 1)
    once = store.replay_unacked(0)
    twice = store.replay_unacked(once)
    assert once == twice
    assert once == 7


def test_compact_on_write_bounds_pages_and_ack_reclaims() -> None:
    store: SpillStore[int] = SpillStore(SpillMode.COMPACT_ON_WRITE, 2, Sum)
    for _ in range(5):
        store.spill(1, 1)  # page size 2 → 3 pages
    assert store.page_count() == 3
    first_id, _ = store.manifest()[0]
    store.ack_through(first_id)
    assert len(store.pending_pages()) == 2
    store.reclaim()
    assert store.page_count() == 2


def test_append_compact_preserves_increments() -> None:
    store: SpillStore[int] = SpillStore(SpillMode.APPEND_COMPACT, 1, Sum)
    for w in (1, 2, 3):
        store.spill(w, 1)
    assert store.page_count() == 3
    assert store.fold_pages(0) == 6


# -- Phase 4 -----------------------------------------------------------------


def test_transport_independent_across_framing() -> None:
    for policy in (Sum, Max, KeepLatest):
        ops = [3, 1, 4, 1, 5, 9]
        flat = reduce(policy.merge, ops)
        for transport in (
            InProcTransport(),
            FramedTransport(2),
            FramedTransport(3),
        ):
            for op in ops:
                transport.deliver(op)
            ctx: dict = {}
            r = _relay(ctx, policy)
            while transport.has_pending():
                for op in transport.poll():
                    r.ingress(op)
            assert r.drain() == flat, policy.name


# -- Phase 5 -----------------------------------------------------------------


def test_outbox_conflates_state_broadcast() -> None:
    ctx: dict = {}
    out: Outbox[int] = Outbox(ctx, 8, KeepLatest)
    out.send(1)
    out.send(2)
    out.send(3)
    assert out.drain() == 3


def test_outbox_block_backpressures_producer() -> None:
    ctx: dict = {}
    out: Outbox[int] = Outbox(ctx, 2, Sum, overflow=Overflow.BLOCK)
    assert out.send(1) == IngressOutcome.ACCEPTED
    assert out.send(1) == IngressOutcome.CONFLATED
    assert out.is_full() is True
    assert out.send(1) == IngressOutcome.BLOCKED


def test_inbox_credit_meters_remote() -> None:
    ctx: dict = {}
    inbox: Inbox[int] = Inbox(ctx, 100, 2, Sum)
    assert inbox.ready() is True
    inbox.receive(5)
    inbox.receive(5)
    assert inbox.ready() is False
    assert inbox.consume(2) == 10
    assert inbox.ready() is True


def test_outbox_to_inbox_link_converges() -> None:
    ctx: dict = {}
    out: Outbox[int] = Outbox(ctx, 64, Sum)
    inbox: Inbox[int] = Inbox(ctx, 64, 64, Sum)
    transport: InProcTransport[int] = InProcTransport()
    ops = [1, 2, 3, 4]
    for op in ops:
        out.send(op)
    transport.deliver(out.drain())
    while transport.has_pending():
        for frame in transport.poll():
            inbox.receive(frame)
    assert inbox.consume(64) == sum(ops)


# -- Phase 6 -----------------------------------------------------------------


def test_rate_policy_token_bucket() -> None:
    rate = RatePolicy(2, 1)
    assert rate.try_egress() is True
    assert rate.try_egress() is True
    assert rate.try_egress() is False
    rate.tick()
    assert rate.try_egress() is True


def test_window_policy_flush_on_fill_and_tick() -> None:
    window = WindowPolicy(3)
    assert window.on_ingress() is False
    assert window.on_ingress() is False
    assert window.on_ingress() is True
    assert window.on_ingress() is False
    assert window.tick() is True
    assert window.tick() is False


def test_expiry_policy_drops_aged() -> None:
    expiry = ExpiryPolicy(5)
    expiry.advance(10)
    batch = [(3, "old"), (7, "fresh"), (10, "now")]
    assert expiry.retain_live(batch) == ["fresh", "now"]


def test_priority_storage_pops_highest_first_fifo_within() -> None:
    pq: PriorityStorage[str] = PriorityStorage()
    pq.push(1, "low")
    pq.push(3, "highA")
    pq.push(2, "mid")
    pq.push(3, "highB")
    assert pq.pop() == "highA"
    assert pq.pop() == "highB"
    assert pq.pop() == "mid"
    assert pq.pop() == "low"
    assert pq.pop() is None


def test_keyed_relay_shards_per_key() -> None:
    ctx: dict = {}
    keyed: KeyedRelay[str, int] = KeyedRelay(ctx, 64, Overflow.CONFLATE, Sum)
    keyed.ingress("a", 1)
    keyed.ingress("b", 10)
    keyed.ingress("a", 2)
    assert keyed.drain("a") == 3
    assert keyed.drain("b") == 10
    assert list(keyed.keys()) == ["a", "b"]
