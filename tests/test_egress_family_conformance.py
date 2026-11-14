"""The transport-agnostic egress contract replayed against every Python flavor.

``#lzegress``. lazily-py ships all three shells — ``EgressCell`` /
``ThreadSafeEgressCell`` / ``AsyncEgressCell`` — matching the three
``coverage.json`` egress rows (``reactive-egress``, ``egress-thread-safe``,
``egress-async``). The flavor axis lives in the RUNNER, exactly as for ingress:
the fixtures name the primitive (``model: EgressCore``) and one replay function
drives the same JSON through each shell's public API.

What keeps this suite from going green while testing nothing:

* The corpus directory and every named fixture must EXIST — an absent
  ``egress/`` directory is a failure here, never a skip.
* Every replay returns the number of steps it EXECUTED, and every flavor asserts
  that count equals the number of steps the corpus DECLARES. ``_apply_op``
  raises on an op it does not know rather than skipping it.
* Each step's ``expected`` block is a tracked view (``instrument``): every key —
  including each sub-key of ``retry`` and ``invalidates`` — must reach a
  comparison against the fixture's own value or the session fails.
* ``invalidates`` is asserted in BOTH directions through a cache-validity probe
  per reader kind, taken before the state assertions (reading a reader re-warms
  it). A step expecting ``false`` fails if the shell invalidated anyway.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from conformance_assert import assert_key, assert_key_into, corpus_subdir, instrument

import lazily


_SPEC = corpus_subdir("egress")
_SRC = Path(__file__).resolve().parents[1] / "src" / "lazily"

#: Every egress fixture the corpus ships for ``EgressCore``. Named explicitly: a
#: fixture added upstream and not here is a missing replay, which the
#: conformance-coverage guard reports.
FIXTURES = (
    "egress_ordered_ack.json",
    "egress_inflight_window.json",
    "egress_retry_budget.json",
    "egress_generation_fence.json",
)

READER_KINDS = ("pending", "inflight", "acked_through", "retry")


class _Flavor:
    name: str
    cls: type[Any]


class SyncFlavor(_Flavor):
    name = "sync"
    cls = lazily.EgressCell


class ThreadSafeFlavor(_Flavor):
    name = "thread-safe"
    cls = lazily.ThreadSafeEgressCell


class AsyncFlavor(_Flavor):
    name = "async"
    cls = lazily.AsyncEgressCell


FLAVORS = (SyncFlavor, ThreadSafeFlavor, AsyncFlavor)

#: (primitive, flavor, source marker, shipped) — checked against src/lazily in
#: both directions by :func:`test_ledger_matches_the_sources`.
LEDGER = (
    ("egress", "sync", "class EgressCell", True),
    ("egress", "thread-safe", "class ThreadSafeEgressCell", True),
    ("egress", "async", "class AsyncEgressCell", True),
)


# ---------------------------------------------------------------------------
# Fixture decoding
# ---------------------------------------------------------------------------


def _load(name: str) -> dict[str, Any]:
    # Fail, never skip: an absent corpus would make every replay below vacuous.
    assert _SPEC.is_dir(), f"canonical egress corpus not found at {_SPEC}"
    path = _SPEC / name
    assert path.is_file(), f"canonical egress fixture missing: {name}"
    return instrument(json.loads(path.read_text()), name=f"egress/{name}")


def _policy_of(raw: dict[str, Any]) -> lazily.EgressPolicy:
    return lazily.EgressPolicy(
        inflight_limit=raw["inflight_limit"],
        retry_budget=raw["retry_budget"],
        retry_base=raw["retry_base"],
        retry_ceiling=raw["retry_ceiling"],
    )


def _envelope(envelope: lazily.EgressEnvelope[int]) -> dict[str, Any]:
    return {
        "generation": envelope.generation,
        "sequence": envelope.sequence,
        "attempt": envelope.attempt,
        "payload": envelope.payload,
    }


def _retry(retry: lazily.EgressRetry) -> dict[str, Any]:
    return {
        "sequence": retry.sequence,
        "attempt": retry.attempt,
        "backoff": retry.backoff,
        "exhausted": retry.exhausted,
    }


def _claim_value(claim: lazily.EgressClaim[int]) -> dict[str, Any]:
    kind = claim.kind
    if kind is lazily.EgressClaimKind.CLAIMED:
        assert claim.envelope is not None
        return {"claim": "claimed", "envelope": _envelope(claim.envelope)}
    if kind is lazily.EgressClaimKind.STALE_GENERATION:
        return {"claim": "stale_generation", "current": claim.current}
    return {"claim": str(kind)}


def _ack_value(ack: lazily.EgressAck) -> dict[str, Any]:
    if ack.kind is lazily.EgressAckKind.STALE_GENERATION:
        return {"ack": "stale_generation", "current": ack.current}
    if ack.kind is lazily.EgressAckKind.BEYOND_SENT:
        return {"ack": "beyond_sent", "next_sequence": ack.next_sequence}
    return {"ack": str(ack.kind), "through": ack.through}


def _failure_value(failure: lazily.EgressFailure) -> dict[str, Any]:
    kind = failure.kind
    if kind in {lazily.EgressFailureKind.RETRYING, lazily.EgressFailureKind.EXHAUSTED}:
        assert failure.retry is not None
        return {"failure": str(kind), "retry": _retry(failure.retry)}
    if kind is lazily.EgressFailureKind.STALE_GENERATION:
        return {"failure": "stale_generation", "current": failure.current}
    return {"failure": str(kind)}


def _retry_action_value(action: lazily.EgressRetryAction) -> dict[str, Any]:
    kind = action.kind
    if kind is lazily.EgressRetryActionKind.SCHEDULED:
        return {"retry_action": "scheduled", "sequence": action.sequence}
    if kind is lazily.EgressRetryActionKind.STALE_GENERATION:
        return {"retry_action": "stale_generation", "current": action.current}
    return {"retry_action": str(kind)}


def _reconnect_value(reconnect: lazily.EgressReconnect) -> dict[str, Any]:
    kind = reconnect.kind
    if kind is lazily.EgressReconnectKind.ADVANCED:
        return {
            "reconnect": "advanced",
            "generation": reconnect.generation,
            "replayed": reconnect.replayed,
        }
    if kind is lazily.EgressReconnectKind.UNCHANGED:
        return {"reconnect": "unchanged", "generation": reconnect.generation}
    return {"reconnect": "stale_generation", "current": reconnect.current}


def _apply_op(where: str, cell: Any, op: dict[str, Any]) -> dict[str, Any]:
    kind = op["type"]
    if kind == "enqueue":
        return {"sequence": cell.enqueue(op["payload"])}
    if kind == "claim":
        return _claim_value(cell.claim(op["generation"]))
    if kind == "ack":
        return _ack_value(cell.ack(op["generation"], op["through"]))
    if kind == "fail":
        return _failure_value(cell.fail(op["generation"], op["sequence"]))
    if kind == "retry_now":
        return _retry_action_value(cell.retry_now(op["generation"], op["sequence"]))
    if kind == "reconnect":
        return _reconnect_value(cell.reconnect(op["generation"]))
    raise AssertionError(f"{where}: unknown egress op {kind!r}")


# ---------------------------------------------------------------------------
# The cache-validity probe
# ---------------------------------------------------------------------------


def _snapshot(cell: Any) -> dict[str, bool]:
    return {
        "pending": cell.pending_is_valid(),
        "inflight": cell.inflight_is_valid(),
        "acked_through": cell.acked_through_is_valid(),
        "retry": cell.retry_is_valid(),
    }


def _materialize(cell: Any) -> None:
    cell.pending()
    cell.inflight()
    cell.acked_through()
    cell.retry()


def _assert_invalidation(
    where: str, expected: Any, before: dict[str, bool], after: dict[str, bool]
) -> None:
    invalidates = expected.sub("invalidates")
    assert sorted(invalidates) == sorted(READER_KINDS), (
        f"{where}: invalidates names {sorted(invalidates)}, but this probe "
        f"measures {sorted(READER_KINDS)}"
    )
    for kind in READER_KINDS:
        want = assert_key_into(
            invalidates, kind, lambda fixture_value: fixture_value, where=where
        )
        assert isinstance(want, bool), f"{where}: invalidates.{kind} is not a bool"
        invalidated = before[kind] and not after[kind]
        assert invalidated is want, (
            f"{where}: {kind} invalidation is {invalidated}, expected {want} "
            f"(was valid={before[kind]}, now valid={after[kind]})"
        )


def _assert_state(where: str, cell: Any, expected: Any) -> None:
    assert_key(expected, "generation", cell.generation, where=where)
    assert_key(expected, "next_sequence", cell.next_sequence, where=where)
    assert_key(expected, "pending", [_envelope(e) for e in cell.pending()], where=where)
    assert_key(
        expected, "inflight", [_envelope(e) for e in cell.inflight()], where=where
    )
    assert_key(expected, "acked_through", cell.acked_through(), where=where)
    retry = cell.retry()
    if expected["retry"] is None:
        assert_key(
            expected, "retry", None if retry is None else _retry(retry), where=where
        )
    else:
        want = expected.sub("retry")
        assert retry is not None, f"{where}: retry is absent"
        assert sorted(want) == sorted(_retry(retry)), (
            f"{where}: retry names {sorted(want)}"
        )
        for field, value in _retry(retry).items():
            assert_key(want, field, value, where=where)


# ---------------------------------------------------------------------------
# The replay
# ---------------------------------------------------------------------------


def _replay(flavor: type[_Flavor], fixture_name: str) -> int:
    """Replay one fixture against one flavor; returns the steps EXECUTED."""
    fixture = _load(fixture_name)
    assert fixture["kind"] == "Egress", f"{fixture_name}: fixture kind"
    assert fixture["model"] == "EgressCore", f"{fixture_name}: fixture model"
    ctx: dict = {}
    cell = flavor.cls(ctx, fixture["generation"], _policy_of(fixture["policy"]))
    _materialize(cell)

    steps = fixture["steps"]
    assert steps, f"{flavor.name} {fixture_name}: fixture has no steps"
    executed = 0
    for index, step in enumerate(steps):
        where = f"{flavor.name} {fixture_name} step {index} ({step['op']['type']})"
        expected = step["expected"]
        before = _snapshot(cell)
        actual = _apply_op(where, cell, step["op"])
        # Probe BEFORE the state assertions: reading re-warms a reader's cache
        # and would erase the very invalidation under test.
        after = _snapshot(cell)
        assert actual == step["returns"], (
            f"{where}: returns {actual!r}, expected {step['returns']!r}"
        )
        _assert_state(where, cell, expected)
        _assert_invalidation(where, expected, before, after)
        _materialize(cell)
        executed += 1
    return executed


def _declared_step_total() -> int:
    return sum(len(_load(name)["steps"]) for name in FIXTURES)


# ---------------------------------------------------------------------------
# The gates
# ---------------------------------------------------------------------------


def test_corpus_is_present_and_every_fixture_has_steps() -> None:
    assert _SPEC.is_dir(), f"canonical egress corpus not found at {_SPEC}"
    on_disk = {
        path.name
        for path in _SPEC.glob("egress_*.json")
        if json.loads(path.read_text()).get("model") == "EgressCore"
    }
    assert on_disk == set(FIXTURES), (
        f"EgressCore fixtures on disk {sorted(on_disk)} differ from the replayed "
        f"set {sorted(FIXTURES)}"
    )
    for name in FIXTURES:
        assert _load(name)["steps"], f"{name}: a fixture with no steps is vacuous"


@pytest.mark.parametrize("flavor", FLAVORS, ids=lambda flavor: flavor.name)
@pytest.mark.parametrize("fixture_name", FIXTURES)
def test_egress_fixture_all_flavors(flavor: type[_Flavor], fixture_name: str) -> None:
    executed = _replay(flavor, fixture_name)
    declared = len(_load(fixture_name)["steps"])
    assert executed == declared > 0, (
        f"{flavor.name} {fixture_name}: executed {executed} of {declared} steps"
    )


@pytest.mark.parametrize("flavor", FLAVORS, ids=lambda flavor: flavor.name)
def test_every_flavor_replays_the_whole_corpus(flavor: type[_Flavor]) -> None:
    executed = sum(_replay(flavor, name) for name in FIXTURES)
    declared = _declared_step_total()
    assert executed == declared > 0, (
        f"{flavor.name}: executed {executed} steps; the corpus declares {declared}"
    )


def test_ledger_matches_the_sources() -> None:
    sources = "\n".join(path.read_text() for path in sorted(_SRC.rglob("*.py")))
    assert sources, "read no sources from src/lazily; the ledger check is vacuous"
    for primitive, flavor, marker, shipped in LEDGER:
        assert (marker in sources) is shipped, (
            f"{primitive}/{flavor}: ledger says shipped={shipped} but source "
            f"marker {marker!r} present={marker in sources}"
        )
    claimed = {flavor for _, flavor, _, shipped in LEDGER if shipped}
    assert claimed == {flavor.name for flavor in FLAVORS}


@pytest.mark.parametrize("flavor", FLAVORS, ids=lambda flavor: flavor.name)
def test_the_invalidation_probe_discriminates(flavor: type[_Flavor]) -> None:
    """The corpus asserts negative invalidation, so the probe itself must be able
    to fail in both directions."""
    cell = flavor.cls({}, 1, lazily.EgressPolicy())
    assert _snapshot(cell) == dict.fromkeys(READER_KINDS, False), "unread = cold"
    _materialize(cell)
    assert _snapshot(cell) == dict.fromkeys(READER_KINDS, True), "read = warm"
    cell.enqueue(1)
    assert _snapshot(cell) == {
        "pending": False,
        "inflight": True,
        "acked_through": True,
        "retry": True,
    }
    _materialize(cell)
    cell.ack(0, 0)  # stale generation: no reader may move
    assert _snapshot(cell) == dict.fromkeys(READER_KINDS, True)


# ---------------------------------------------------------------------------
# The transport attachment
# ---------------------------------------------------------------------------


class _Transport:
    def __init__(self) -> None:
        self.sent: list[int] = []
        self.fail_next = False

    def send(self, envelope: lazily.EgressEnvelope[int]) -> bool:
        if self.fail_next:
            self.fail_next = False
            return False
        self.sent.append(envelope.sequence)
        return True


@pytest.mark.parametrize("flavor", FLAVORS, ids=lambda flavor: flavor.name)
def test_one_effect_projects_pending_to_the_transport(flavor: type[_Flavor]) -> None:
    cell = flavor.cls({}, 1, lazily.EgressPolicy())
    transport = _Transport()
    attachment = cell.attach_transport(transport)
    cell.enqueue(10)
    cell.enqueue(20)
    assert transport.sent == [0, 1]
    assert cell.pending() == []
    assert [e.sequence for e in cell.inflight()] == [0, 1]
    attachment.dispose()


@pytest.mark.parametrize("flavor", FLAVORS, ids=lambda flavor: flavor.name)
def test_ack_reopens_the_window_reactively(flavor: type[_Flavor]) -> None:
    cell = flavor.cls({}, 1, lazily.EgressPolicy(inflight_limit=1))
    transport = _Transport()
    attachment = cell.attach_transport(transport)
    cell.enqueue(10)
    cell.enqueue(20)
    assert transport.sent == [0]
    assert len(cell.pending()) == 1
    assert cell.ack(1, 0) == lazily.EgressAck(lazily.EgressAckKind.ADVANCED, through=0)
    assert transport.sent == [0, 1], "the acknowledgement re-ran the same attachment"
    assert cell.pending() == []
    attachment.dispose()


@pytest.mark.parametrize("flavor", FLAVORS, ids=lambda flavor: flavor.name)
def test_reconnect_makes_the_old_attachment_inert(flavor: type[_Flavor]) -> None:
    cell = flavor.cls({}, 3, lazily.EgressPolicy())
    old = _Transport()
    old_attachment = cell.attach_transport(old)
    cell.enqueue(10)
    cell.reconnect(4)
    cell.enqueue(20)
    assert old.sent == [0], "the fenced attachment claims nothing more"
    fresh = _Transport()
    fresh_attachment = cell.attach_transport(fresh)
    assert fresh.sent == [0, 1], "the replacement replays under the new incarnation"
    assert {e.generation for e in cell.inflight()} == {4}
    old_attachment.dispose()
    fresh_attachment.dispose()


@pytest.mark.parametrize("flavor", FLAVORS, ids=lambda flavor: flavor.name)
def test_a_failed_send_is_parked_not_respun(flavor: type[_Flavor]) -> None:
    cell = flavor.cls({}, 1, lazily.EgressPolicy(retry_budget=1))
    transport = _Transport()
    transport.fail_next = True
    attachment = cell.attach_transport(transport)
    cell.enqueue(10)
    assert transport.sent == []
    retry = cell.retry()
    assert retry is not None and retry.attempt == 1 and not retry.exhausted
    assert cell.pending() == [] and cell.inflight() == []
    cell.retry_now(1, 0)
    assert transport.sent == [0], "explicit scheduling re-pends and the Effect sends"
    attachment.dispose()


def test_async_attachment_is_the_only_async_colour() -> None:
    class AsyncTransport:
        def __init__(self) -> None:
            self.sent: list[int] = []

        async def send(self, envelope: lazily.EgressEnvelope[int]) -> bool:
            await asyncio.sleep(0)
            self.sent.append(envelope.sequence)
            return True

    async def scenario() -> None:
        cell: lazily.AsyncEgressCell[int] = lazily.AsyncEgressCell(
            {}, 1, lazily.EgressPolicy(inflight_limit=1)
        )
        transport = AsyncTransport()
        attachment = cell.attach_transport_async(transport)
        cell.enqueue(7)
        cell.enqueue(8)
        # Delivery state is synchronous: nothing was claimed before the loop ran.
        assert len(cell.pending()) == 2
        await attachment.settle()
        assert transport.sent == [0]
        assert [e.sequence for e in cell.inflight()] == [0]
        cell.ack(1, 0)
        await attachment.settle()
        assert transport.sent == [0, 1]
        cell.reconnect(2)
        await attachment.settle()
        assert transport.sent == [0, 1], "the fenced async attachment is inert"
        attachment.dispose()

    asyncio.run(scenario())


def test_thread_safe_flavor_serializes_concurrent_enqueues() -> None:
    from concurrent.futures import ThreadPoolExecutor

    cell: lazily.ThreadSafeEgressCell[int] = lazily.ThreadSafeEgressCell(
        {}, 1, lazily.EgressPolicy(inflight_limit=256)
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        sequences = list(pool.map(cell.enqueue, range(128)))
    assert sorted(sequences) == list(range(128)), "sequence assignment is monotone"
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: cell.claim(1), range(128)))
    claimed = sorted(c.envelope.sequence for c in claims if c.envelope is not None)
    assert claimed == list(range(128)), "every sequence claimed exactly once"
    assert cell.pending() == []


def test_policy_validation() -> None:
    for policy, message in (
        (
            lazily.EgressPolicy(inflight_limit=0),
            lazily.EgressConfigError.ZERO_INFLIGHT_LIMIT,
        ),
        (lazily.EgressPolicy(retry_base=0), lazily.EgressConfigError.ZERO_RETRY_BASE),
        (
            lazily.EgressPolicy(retry_base=10, retry_ceiling=5),
            lazily.EgressConfigError.RETRY_CEILING_BELOW_BASE,
        ),
    ):
        with pytest.raises(lazily.EgressConfigError) as excinfo:
            lazily.EgressCore(1, policy)
        assert str(excinfo.value) == message


def test_thread_safe_flavor_serializes_core_access_on_the_shared_lock() -> None:
    """The GIL makes a concurrency stress pass with or without the lock, so prove
    the boundary directly: while another holder owns the context lock, a
    transition cannot reach the core. The blocked direction cannot pass by
    accident — the worker physically cannot finish while the lock is held."""
    import threading

    ts = lazily.ThreadSafeContext()
    cell: lazily.ThreadSafeEgressCell[int] = lazily.ThreadSafeEgressCell(
        {}, 1, lazily.EgressPolicy(), ts=ts
    )
    assert cell.thread_safe_context is ts
    done = threading.Event()

    def worker() -> None:
        cell.enqueue(1)
        done.set()

    with ts.lock:
        thread = threading.Thread(target=worker)
        thread.start()
        assert not done.wait(0.2), "enqueue reached the core without the lock"
        assert cell.next_sequence == 0  # re-entrant: this thread owns the lock
    thread.join(5)
    assert done.is_set() and cell.next_sequence == 1


@pytest.mark.parametrize("flavor", FLAVORS, ids=lambda flavor: flavor.name)
def test_an_ack_beyond_next_sequence_is_refused_and_changes_nothing(
    flavor: type[_Flavor],
) -> None:
    """Acknowledging a sequence never assigned would advance the watermark over
    it, and the record later enqueued at that sequence would be pruned unsent."""
    cell = flavor.cls({}, 1, lazily.EgressPolicy())
    cell.enqueue(10)
    _materialize(cell)
    before = (cell.acked_through(), cell.pending(), cell.next_sequence)
    for through in (1, 5):
        assert cell.ack(1, through) == lazily.EgressAck(
            lazily.EgressAckKind.BEYOND_SENT, next_sequence=1
        )
        assert _snapshot(cell) == dict.fromkeys(READER_KINDS, True), (
            "a refused ACK invalidates nothing"
        )
    assert (cell.acked_through(), cell.pending(), cell.next_sequence) == before
    cell.enqueue(20)
    assert cell.ack(1, 1).kind is lazily.EgressAckKind.ADVANCED
    assert cell.pending() == []


@pytest.mark.parametrize("flavor", FLAVORS, ids=lambda flavor: flavor.name)
def test_reconnect_with_nothing_replayed_still_invalidates_pending(
    flavor: type[_Flavor],
) -> None:
    """Reconnect rewrites every pending envelope to the new generation, so a
    warm `pending` reader is stale even when no in-flight record was replayed."""
    cell = flavor.cls({}, 1, lazily.EgressPolicy())
    cell.enqueue(10)
    _materialize(cell)
    reconnect = cell.reconnect(2)
    assert reconnect.replayed == 0
    assert _snapshot(cell) == {
        "pending": False,
        "inflight": True,
        "acked_through": True,
        "retry": True,
    }
    assert [e.generation for e in cell.pending()] == [2]
    # And with nothing pending at all, nothing is invalidated.
    empty = flavor.cls({}, 1, lazily.EgressPolicy())
    _materialize(empty)
    empty.reconnect(2)
    assert _snapshot(empty) == dict.fromkeys(READER_KINDS, True)
