"""Async effect lifecycle — cleanup-before-body, batch-boundary scheduling,
disposal.

The Python counterpart of the Lean ``LazilyFormal.AsyncEffect`` formal model in
``lazily-formal``. The pure ``step`` kernel is exercised directly (mirroring the
named theorems); the ``AsyncEffect`` runtime is exercised through ``asyncio``.
"""

from __future__ import annotations

import asyncio

from lazily import EffectEvent, EffectState
from lazily.async_effect import AsyncEffect, step


IDLE = EffectState.IDLE
SCHED = EffectState.SCHEDULED
CR = EffectState.CLEANUP_RUNNING
CRS = EffectState.CLEANUP_RUNNING_SCHEDULED
DISP = EffectState.DISPOSED


# =================================================================================
# fire_blocked_during_cleanup — cleanup-before-body (point 6).
# A body rerun cannot start while a cleanup future is pending.
# =================================================================================


def test_fire_blocked_during_cleanup() -> None:
    for s in (CR, CRS):
        out = step(s, EffectEvent.FIRE, has_cleanup=True)
        assert out is s  # fire is a no-op from cleanup-pending states
        out2 = step(s, EffectEvent.FIRE, has_cleanup=False)
        assert out2 is s


# =================================================================================
# invalidate_from_idle_schedules / invalidate_yields_pending_or_disposed (point 7).
# A dependency invalidation only ever queues a rerun, never starts one inline.
# =================================================================================


def test_invalidate_from_idle_schedules() -> None:
    assert step(IDLE, EffectEvent.INVALIDATE) is SCHED


def test_invalidate_yields_pending_or_disposed() -> None:
    pending_or_disposed = {SCHED, CRS, DISP}
    for s in EffectState:
        out = step(s, EffectEvent.INVALIDATE)
        assert out in pending_or_disposed, (s, out)


# =================================================================================
# cleanupDone_resumes_deferred (point 6) — serialized resumption.
# =================================================================================


def test_cleanup_done_resumes_deferred() -> None:
    assert step(CRS, EffectEvent.CLEANUP_DONE) is SCHED
    assert step(CR, EffectEvent.CLEANUP_DONE) is IDLE


# =================================================================================
# dispose_absorbing / disposed_terminal (point 3).
# =================================================================================


def test_dispose_absorbing() -> None:
    for s in EffectState:
        assert step(s, EffectEvent.DISPOSE) is DISP


def test_disposed_terminal() -> None:
    for ev in (
        EffectEvent.INVALIDATE,
        EffectEvent.FIRE,
        EffectEvent.CLEANUP_DONE,
        EffectEvent.DISPOSE,
    ):
        assert step(DISP, ev, has_cleanup=True) is DISP


# =================================================================================
# fire from scheduled — with/without cleanup.
# =================================================================================


def test_fire_from_scheduled() -> None:
    assert step(SCHED, EffectEvent.FIRE, has_cleanup=True) is CR
    assert step(SCHED, EffectEvent.FIRE, has_cleanup=False) is IDLE


def test_invalidate_during_cleanup_defers() -> None:
    assert step(CR, EffectEvent.INVALIDATE) is CRS


# =================================================================================
# Runtime: AsyncEffect cleanup-before-body + batch-boundary scheduling.
# =================================================================================


def test_async_effect_cleanup_before_body() -> None:
    log: list[str] = []

    async def body():
        log.append("body")

        async def cleanup():
            log.append("cleanup")

        return cleanup

    eff = AsyncEffect(body)

    async def main() -> None:
        eff.invalidate()  # queues a rerun, does not run inline
        assert eff.state is EffectState.SCHEDULED
        await eff.flush()  # fires the rerun at the batch boundary
        assert "body" in log
        # cleanup ran before the body returned to idle (cleanup awaited in flush)
        assert log == ["body", "cleanup"]
        assert eff.state is EffectState.IDLE

    asyncio.run(main())


def test_async_effect_dispose_is_terminal() -> None:
    async def body():
        return None

    eff = AsyncEffect(body)

    async def main() -> None:
        await eff.dispose()
        assert eff.state is EffectState.DISPOSED
        eff.invalidate()  # no-op after disposal
        assert eff.state is EffectState.DISPOSED

    asyncio.run(main())


def test_async_effect_invalidation_only_queues() -> None:
    ran = [False]

    async def body():
        ran[0] = True
        return None

    eff = AsyncEffect(body)
    eff.invalidate()
    assert not ran[0]  # body did NOT run inline (only queued)
    assert eff.state is EffectState.SCHEDULED
