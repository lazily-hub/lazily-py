"""Async slot state machine — the ``Empty / Computing / Resolved / Error``
lifecycle with revision-tracked stale-completion discard.

The Python counterpart of the Lean ``LazilyFormal.AsyncSlotState`` formal model
in ``lazily-formal``. The pure ``step`` kernel is exercised directly (mirroring
the named theorems); the ``AsyncSlot`` runtime is exercised through ``asyncio``
for the stale-completion-discard and retry behavior.
"""

from __future__ import annotations

import asyncio

import pytest

from lazily import SlotEvent, SlotState
from lazily.async_slot import AsyncSlot, StepSlot, step


E = SlotState.EMPTY
C = SlotState.COMPUTING
R = SlotState.RESOLVED
X = SlotState.ERROR


def _slot(state=SlotState.EMPTY, revision=0, compute_rev=None, value=None) -> StepSlot:
    return StepSlot(
        state=state, revision=revision, compute_rev=compute_rev, value=value
    )


# =================================================================================
# stale_completeOk_discarded / stale_completeErr_discarded
# A stale completion (revision mismatch) is discarded: the slot is byte-identical.
# =================================================================================


def test_stale_complete_ok_discarded() -> None:
    s = _slot(C, revision=5, compute_rev=5, value=None)
    # A completion carrying a stale revision 3 (not 5) is discarded.
    out = step(s, SlotEvent.COMPLETE_OK, rev=3, value=42)
    assert out is s or out == s  # byte-identical


def test_stale_complete_err_discarded() -> None:
    s = _slot(C, revision=5, compute_rev=5, value=None)
    out = step(s, SlotEvent.COMPLETE_ERR, rev=3)
    assert out == s


# =================================================================================
# current_completeOk_publishes / current_completeErr_to_error
# A current completion (revision matches) publishes.
# =================================================================================


def test_current_complete_ok_publishes() -> None:
    s = _slot(C, revision=5, compute_rev=5)
    out = step(s, SlotEvent.COMPLETE_OK, rev=5, value=42)
    assert out.state is R
    assert out.value == 42
    assert out.compute_rev is None


def test_current_complete_err_to_error() -> None:
    s = _slot(C, revision=5, compute_rev=5)
    out = step(s, SlotEvent.COMPLETE_ERR, rev=5)
    assert out.state is X
    assert out.compute_rev is None


# =================================================================================
# Transitions: start / invalidate / retry / hard_clear.
# =================================================================================


def test_start_from_empty() -> None:
    s = _slot(E, revision=2)
    out = step(s, SlotEvent.START)
    assert out.state is C and out.compute_rev == 2 and out.revision == 2


def test_invalidate_advances_revision_and_recomputes() -> None:
    s = _slot(R, revision=2, value=99)
    out = step(s, SlotEvent.INVALIDATE)
    assert out.state is C
    assert out.revision == 3
    assert out.compute_rev == 3
    assert out.value is None  # cached value dropped


def test_invalidate_noop_on_error() -> None:
    s = _slot(X, revision=2)
    assert step(s, SlotEvent.INVALIDATE) == s


def test_retry_from_error() -> None:
    s = _slot(X, revision=2)
    out = step(s, SlotEvent.RETRY)
    assert out.state is C and out.compute_rev == 2


def test_hard_clear_resets_and_bumps_revision() -> None:
    s = _slot(R, revision=2, value=99)
    out = step(s, SlotEvent.HARD_CLEAR)
    assert out.state is E
    assert out.revision == 3
    assert out.compute_rev is None
    assert out.value is None


# =================================================================================
# step_preserves_wellFormed
# =================================================================================


def test_step_preserves_well_formed() -> None:
    states = [
        _slot(E),
        _slot(C, revision=1, compute_rev=1),
        _slot(R, revision=1, value=7),
        _slot(X, revision=1),
    ]
    events = [
        (SlotEvent.START, {}),
        (SlotEvent.COMPLETE_OK, {"rev": 1, "value": 9}),
        (SlotEvent.COMPLETE_ERR, {"rev": 1}),
        (SlotEvent.INVALIDATE, {}),
        (SlotEvent.RETRY, {}),
        (SlotEvent.HARD_CLEAR, {}),
    ]
    for s in states:
        assert s.is_well_formed(), s
        for ev, kw in events:
            assert step(s, ev, **kw).is_well_formed(), (s, ev)


# =================================================================================
# Runtime: AsyncSlot stale-completion discard + retry (asyncio).
# =================================================================================


def test_async_slot_resolves_and_caches() -> None:
    calls = [0]

    async def compute() -> int:
        calls[0] += 1
        return 41

    slot = AsyncSlot(compute)

    async def main() -> None:
        v1 = await slot.get_async()
        v2 = await slot.get_async()  # cached
        assert v1 == v2 == 41
        assert calls[0] == 1  # computed once

    asyncio.run(main())


def test_async_slot_stale_completion_discarded() -> None:
    """An in-flight completion whose revision no longer matches is discarded;
    get_async re-resolves by spawning a fresh compute and returns the new value.
    """
    compute_state = {"delay": 0.01, "result": "stale"}
    started = asyncio.Event()

    async def slow() -> str:
        started.set()
        await asyncio.sleep(compute_state["delay"])
        return compute_state["result"]

    slot = AsyncSlot(slow)

    async def main() -> str:
        task = asyncio.create_task(slot.get_async())
        await started.wait()
        # Invalidate mid-flight: bumps the revision so the in-flight completion
        # will be discarded (stale) against the new revision.
        compute_state["result"] = "fresh"  # the next compute yields this
        slot.invalidate()
        # get_async must re-resolve: discard the stale completion, spawn a fresh
        # compute, and return the new value (never the stale one).
        return await task

    assert asyncio.run(main()) == "fresh"
    assert slot.state is SlotState.RESOLVED


def test_async_slot_retry_after_error() -> None:
    attempts = [0]

    async def flaky() -> int:
        attempts[0] += 1
        if attempts[0] == 1:
            raise RuntimeError("boom")
        return 7

    slot = AsyncSlot(flaky)

    async def main() -> None:
        with pytest.raises(RuntimeError):
            await slot.get_async()
        assert slot.state is SlotState.ERROR
        v = await slot.get_async()  # retries -> Computing
        assert v == 7
        assert slot.state is SlotState.RESOLVED

    asyncio.run(main())
