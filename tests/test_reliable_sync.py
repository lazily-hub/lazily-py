"""Reliable sync conformance + SyncDriver loop-shape tests (``#lzsync``).

Replays the canonical ``lazily-spec/conformance/reliable-sync`` fixtures against
the native :class:`~lazily.ResyncCoordinator` / :class:`~lazily.InMemoryOutbox` /
:class:`~lazily.OrSet` / :class:`~lazily.WireLwwRegister`, round-trips the two
control frames (:class:`~lazily.ResyncRequest` / :class:`~lazily.OutboxAck`)
through JSON, and pins the :class:`~lazily.SyncDriver` loop shape over a scripted
in-memory transport (mirroring lazily-js ``reliable-sync.test.js`` and lazily-rs
``reliable_sync.rs``). Cross-language pin with lazily-rs / lazily-kt / lazily-js;
backstop lazily-formal ``ReliableSync.lean``.
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path

from lazily import (
    Delta,
    DriverError,
    InMemoryOutbox,
    InMemoryStore,
    IpcMessage,
    OrSet,
    OutboxAck,
    Progress,
    ResyncCoordinator,
    ResyncRequest,
    Snapshot,
    SqliteOutbox,
    SqliteStore,
    SyncDriver,
    WireLwwRegister,
    WireStamp,
)
from lazily.reliable_sync import Outbox


_LOCAL_FIXTURES = Path(__file__).resolve().parent / "conformance" / "reliable-sync"
_SPEC_FIXTURES = (
    Path(__file__).resolve().parents[2]
    / "lazily-spec"
    / "conformance"
    / "reliable-sync"
)


def _load_fixture(name: str) -> dict:
    spec_path = _SPEC_FIXTURES / name
    path = spec_path if spec_path.exists() else _LOCAL_FIXTURES / name
    return json.loads(path.read_text())


def _scenario(fx: dict, name: str) -> dict:
    return next(s for s in fx["scenarios"] if s["name"] == name)


def _msg(wire: dict) -> IpcMessage:
    return IpcMessage.from_wire(wire)


# ---------------------------------------------------------------------------
# control-frame serde round-trip
# ---------------------------------------------------------------------------


def test_resync_request_round_trips_json() -> None:
    m = IpcMessage.of_resync_request(ResyncRequest(from_epoch=2))
    text = m.encode_json().decode("utf-8")
    assert text == '{"ResyncRequest":{"from_epoch":2}}'
    assert IpcMessage.decode_json(text).to_wire() == m.to_wire()


def test_outbox_ack_round_trips_json() -> None:
    m = IpcMessage.of_outbox_ack(OutboxAck(through_epoch=41))
    text = m.encode_json().decode("utf-8")
    assert text == '{"OutboxAck":{"through_epoch":41}}'
    assert IpcMessage.decode_json(text).to_wire() == m.to_wire()


def test_control_frame_ffi_kinds() -> None:
    from lazily import LazilyFfiMessageKind, kind_of

    assert kind_of(IpcMessage.of_resync_request(ResyncRequest(from_epoch=2))) is (
        LazilyFfiMessageKind.ResyncRequest
    )
    assert kind_of(IpcMessage.of_outbox_ack(OutboxAck(through_epoch=1))) is (
        LazilyFfiMessageKind.OutboxAck
    )
    assert LazilyFfiMessageKind.ResyncRequest.value == 4
    assert LazilyFfiMessageKind.OutboxAck.value == 5


# ---------------------------------------------------------------------------
# multi_epoch_delta.json
# ---------------------------------------------------------------------------


def test_multi_epoch_delta() -> None:
    fx = _load_fixture("multi_epoch_delta.json")
    assert fx["kind"] == "ReliableSync"

    sc = _scenario(fx, "span_3_applies_equal_to_unit_fold")
    base = sc["delta"]["base_epoch"]
    epoch = sc["delta"]["epoch"]
    assert epoch > base + 1, "fixture pins a multi-epoch span"
    delta = Delta.new(base, epoch, [])
    assert delta.epoch - delta.base_epoch == epoch - base
    coord = ResyncCoordinator(sc["receiver_last_epoch"])
    assert coord.ingest_delta(delta).is_apply
    assert coord.last_epoch == sc["expect"]["receiver_last_epoch_after"]

    gap = _scenario(fx, "gap_rule_unchanged_under_span")
    gc = ResyncCoordinator(gap["receiver_last_epoch"])
    res = gc.ingest_delta(
        Delta.new(gap["delta"]["base_epoch"], gap["delta"]["epoch"], [])
    )
    assert res.is_request_snapshot
    assert res.from_epoch == gap["expect"]["request_from"]
    assert gc.last_epoch == gap["receiver_last_epoch"]


# ---------------------------------------------------------------------------
# resync_gap_converge.json
# ---------------------------------------------------------------------------


def test_resync_gap_converge() -> None:
    fx = _load_fixture("resync_gap_converge.json")

    sc = _scenario(fx, "drop_suffix_then_resync_converges")
    coord = ResyncCoordinator(sc["start_last_epoch"])
    requests = 0
    for frame in sc["inbound"]:
        if frame.get("dropped"):
            continue
        res = coord.ingest(_msg(frame["frame"]))
        if frame["expect_action"] == "Apply":
            assert res.is_apply
        elif frame["expect_action"] == "RequestSnapshot":
            requests += 1
            assert res.is_request_snapshot
            assert res.from_epoch == frame["request_from"]
        else:
            assert res.is_ignore
        assert coord.last_epoch == frame["last_epoch_after"]
    assert coord.last_epoch == sc["expect"]["final_last_epoch"]
    assert requests == sc["expect"]["resync_requests_emitted"]

    single = _scenario(fx, "single_request_per_gap")
    c2 = ResyncCoordinator(single["start_last_epoch"])
    req2 = 0
    for frame in single["inbound"]:
        if c2.ingest(_msg(frame["frame"])).is_request_snapshot:
            req2 += 1
    assert req2 == single["expect"]["resync_requests_emitted"]


# ---------------------------------------------------------------------------
# idempotent_redelivery.json
# ---------------------------------------------------------------------------


def test_idempotent_redelivery() -> None:
    fx = _load_fixture("idempotent_redelivery.json")
    for name in ("replayed_delta_is_ignored", "duplicate_current_head_is_ignored"):
        sc = _scenario(fx, name)
        coord = ResyncCoordinator(sc["start_last_epoch"])
        for frame in sc["inbound"]:
            assert coord.ingest(_msg(frame["frame"])).is_ignore, name
            assert coord.last_epoch == frame["last_epoch_after"]
        assert coord.last_epoch == sc["expect"]["final_last_epoch"]


def _frames_of(sc: dict, key: str) -> list[tuple[int, IpcMessage]]:
    return [(e["epoch"], IpcMessage.from_wire(e["frame"])) for e in sc[key]]


# ---------------------------------------------------------------------------
# outbox_replay_after_crash.json
# ---------------------------------------------------------------------------


def test_outbox_replay_after_crash(tmp_path: Path) -> None:
    fx = _load_fixture("outbox_replay_after_crash.json")
    sc = _scenario(fx, "crash_between_append_and_ack_replays_on_reconnect")
    appended = _frames_of(sc, "appended")
    ack = sc["ack_through"]
    cursor = sc["reconnect_cursor"]

    path = tmp_path / "outbox.sqlite3"

    mem = InMemoryOutbox()
    durable = SqliteOutbox(path, "doc")
    for e, m in appended:
        mem.append(e, m)
        durable.append(e, m)
    mem.ack_through(ack)
    durable.ack_through(ack)

    assert mem.retained_epochs() == sc["expect"]["retained_after_ack"]
    assert durable.retained_epochs() == sc["expect"]["retained_after_ack"]
    durable.close()

    # "crash": reopen the durable SQLite outbox from disk.
    durable = SqliteOutbox(path, "doc")
    replay = durable.replay_from(cursor)
    assert [e for (e, _) in replay] == sc["expect"]["replayed_from_cursor"]

    coord = ResyncCoordinator(cursor)
    applied: list[int] = []
    for _e, m in replay:
        if coord.ingest(m).is_apply:
            applied.append(coord.last_epoch)
    assert applied == sc["expect"]["receiver_applies"]
    assert coord.last_epoch == sc["expect"]["receiver_last_epoch_after"]
    durable.close()

    # send_failure_retains_frame_for_next_tick
    sc2 = _scenario(fx, "send_failure_retains_frame_for_next_tick")
    mem2 = InMemoryOutbox()
    for e, m in _frames_of(sc2, "appended"):
        mem2.append(e, m)
    assert mem2.retained_epochs() == sc2["expect"]["retained"]
    assert [e for (e, _) in mem2.replay_from(sc2["expect"]["retained"][0] - 1)] == sc2[
        "expect"
    ]["retained"]


def test_outbox_store_protocol(tmp_path: Path) -> None:
    fixture = _load_fixture("outbox_store_protocol.json")
    ordered = _scenario(fixture, "unordered puts replay in ascending epoch order")
    store = InMemoryStore()
    for epoch in ordered["put_epochs"]:
        store.put(epoch, str(epoch).encode())
    assert [e for e, _ in store.scan_after(ordered["scan_after"])] == ordered["expect"][
        "epochs"
    ]

    monotone = _scenario(fixture, "ack cursor is monotone and prune-safe")
    outbox = Outbox(InMemoryStore())
    for epoch in monotone["put_epochs"]:
        outbox.append(epoch, IpcMessage.of_delta(Delta.new(epoch - 1, epoch, [])))
    for epoch in monotone["ack_through"]:
        outbox.ack_through(epoch)
    assert outbox.acked_through == monotone["expect"]["cursor"]
    assert outbox.retained_epochs() == monotone["expect"]["retained"]
    assert [e for e, _ in outbox.replay_from(0)] == monotone["expect"][
        "replay_from_zero"
    ]

    restart = _scenario(fixture, "restart reloads cursor and unacked suffix")
    path = tmp_path / "protocol.sqlite3"
    first = SqliteOutbox(path, "doc")
    for epoch in restart["put_epochs"]:
        first.append(epoch, IpcMessage.of_delta(Delta.new(epoch - 1, epoch, [])))
    for epoch in restart["ack_through"]:
        first.ack_through(epoch)
    first.close()

    reopened = SqliteOutbox(path, "doc")
    assert reopened.acked_through == restart["expect"]["loaded_cursor"]
    assert reopened.retained_epochs() == restart["expect"]["retained"]
    assert [e for e, _ in reopened.replay_from(0)] == restart["expect"]["replay"]
    reopened.close()


def test_sqlite_cursor_update_is_serialized_monotone(tmp_path: Path) -> None:
    """A stale writer cannot overwrite a newer cursor persisted by another handle."""
    path = tmp_path / "cursor.sqlite3"
    stale = SqliteStore(path, "doc")
    current = SqliteStore(path, "doc")
    current.save_cursor(9)
    stale.save_cursor(3)
    stale.close()
    current.close()

    reopened = SqliteStore(path, "doc")
    assert reopened.load_cursor() == 9
    reopened.close()


# ---------------------------------------------------------------------------
# liveness_orset_lww.json
# ---------------------------------------------------------------------------


def _stamp(o: dict) -> WireStamp:
    return WireStamp(wall_time=o["wall_time"], logical=o["logical"], peer=o["peer"])


def test_liveness_orset_lww() -> None:
    fx = _load_fixture("liveness_orset_lww.json")

    add = _scenario(fx, "open_set_add_wins_over_stale_remove")
    st = OrSet()
    for op in add["ops"]:
        if op["op"] == "add":
            st.add(op["tag"])
        elif op["op"] == "remove":
            st.remove_observed(op["observed_tags"])
    assert st.present() == add["expect"]["present"]

    lww = _scenario(fx, "lww_alive_highest_stamp_wins")
    reg: WireLwwRegister[bool] = WireLwwRegister(
        _stamp(lww["ops"][0]["stamp"]), lww["ops"][0]["value"]
    )
    for op in lww["ops"][1:]:
        reg.set(_stamp(op["stamp"]), op["value"])
    assert reg.value == lww["expect"]["value"]

    death = _scenario(fx, "whole_editor_death_cascades")
    open_entries: list[tuple[str, int]] = []
    for entry in death["open_set"]:
        if entry["present"]:
            doc, pid = entry["key"].split("/")
            open_entries.append((doc, int(pid.replace("pid", ""))))
    alive: dict[int, WireLwwRegister[bool]] = {}
    for pid_str, v in death["alive_before"].items():
        alive[int(pid_str)] = WireLwwRegister(WireStamp(1, 0, 1), v)
    op = death["op"]
    pid = int(op["key"].replace("alive/pid", ""))
    alive[pid].set(_stamp(op["stamp"]), op["value"])
    live = sorted(
        {doc for (doc, p) in open_entries if alive.get(p) and alive[p].value is True}
    )
    assert live == sorted(death["expect"]["live_docs_after"])


# ---------------------------------------------------------------------------
# ResyncCoordinator unit tests (mirror lazily-rs)
# ---------------------------------------------------------------------------


def test_coordinator_applies_contiguous_and_advances() -> None:
    c = ResyncCoordinator.with_epoch(40)
    assert c.ingest_delta(Delta.new(40, 41, [])).is_apply
    assert c.last_epoch == 41
    assert c.ingest_delta(Delta.new(41, 44, [])).is_apply
    assert c.last_epoch == 44


def test_coordinator_ignores_empty_backward_delta() -> None:
    c = ResyncCoordinator.with_epoch(40)
    assert c.ingest_delta(Delta.new(40, 40, [])).is_ignore
    assert c.last_epoch == 40


def test_coordinator_gap_requests_once_then_ignores() -> None:
    c = ResyncCoordinator.with_epoch(2)
    res = c.ingest_delta(Delta.new(3, 4, []))
    assert res.is_request_snapshot and res.from_epoch == 2
    assert c.is_resyncing
    assert c.ingest_delta(Delta.new(4, 5, [])).is_ignore
    assert c.ingest_snapshot(5).is_apply
    assert not c.is_resyncing
    assert c.last_epoch == 5


def test_ack_carries_last_epoch() -> None:
    c = ResyncCoordinator.with_epoch(7)
    assert c.ack() == IpcMessage.of_outbox_ack(OutboxAck(through_epoch=7))


def test_outbox_retains_unacked_and_replays_from_cursor() -> None:
    o = InMemoryOutbox()
    for e in range(41, 44):
        o.append(e, IpcMessage.of_delta(Delta.new(e - 1, e, [])))
    o.ack_through(41)
    assert o.retained_epochs() == [42, 43]
    assert [e for (e, _) in o.replay_from(41)] == [42, 43]


def test_orset_join_is_commutative_and_add_wins() -> None:
    a = OrSet()
    a.add("t1")
    b = OrSet()
    b.remove_observed(["t1"])
    b.add("t3")
    ab = OrSet()
    ab.join(a)
    ab.join(b)
    ba = OrSet()
    ba.join(b)
    ba.join(a)
    assert ab == ba, "join is commutative"
    assert ab.present(), "add tag t3 not shadowed → present"


def test_lww_join_keeps_higher_stamp() -> None:
    a: WireLwwRegister[bool] = WireLwwRegister(WireStamp(10, 0, 1), True)
    b: WireLwwRegister[bool] = WireLwwRegister(WireStamp(20, 0, 1), False)
    a.join(b)
    assert a.value is False
    a.join(WireLwwRegister(WireStamp(5, 0, 1), True))
    assert a.value is False


# ---------------------------------------------------------------------------
# SyncDriver: loop-shape mechanism over a scripted transport (mirror lazily-js)
# ---------------------------------------------------------------------------


class Wire:
    def __init__(self) -> None:
        self.sent: list[IpcMessage] = []
        self.inbound: deque[IpcMessage] = deque()
        self.up = True
        self.source_err = False


class _Sink:
    def __init__(self, wire: Wire) -> None:
        self._wire = wire

    def send(self, message: IpcMessage) -> bool:
        if not self._wire.up:
            return False
        self._wire.sent.append(message)
        return True


class _Source:
    def __init__(self, wire: Wire) -> None:
        self._wire = wire

    def recv(self) -> IpcMessage | None:
        if self._wire.source_err:
            self._wire.source_err = False
            raise RuntimeError("scripted source read failure")
        return self._wire.inbound.popleft() if self._wire.inbound else None


class _SnapAhead:
    """Provider answering ``ResyncRequest{from}`` with a snapshot at ``from + 5``."""

    def snapshot(self, from_epoch: int) -> IpcMessage:
        return IpcMessage.of_snapshot(Snapshot(epoch=from_epoch + 5))


class _ZeroClock:
    def now_millis(self) -> int:
        return 0


def _driver_at(wire: Wire, last_epoch: int) -> SyncDriver:
    return SyncDriver(
        _Sink(wire),
        _Source(wire),
        InMemoryOutbox(),
        _ZeroClock(),
        _SnapAhead(),
        last_epoch,
    )


def _dframe(base: int, epoch: int) -> IpcMessage:
    return IpcMessage.of_delta(Delta.new(base, epoch, []))


def test_driver_drains_append_before_send_and_retains_until_acked() -> None:
    wire = Wire()
    d = _driver_at(wire, 0)
    d.enqueue(1, _dframe(0, 1))
    d.enqueue(2, _dframe(1, 2))
    p = d.tick()
    assert isinstance(p, Progress)
    assert p.sent == 2, "both fresh frames pushed to the sink"
    assert len(wire.sent) == 2
    assert p.retained == 2, "appended-before-send, retained until acked"
    assert not d.is_stalled()

    wire.inbound.append(IpcMessage.of_outbox_ack(OutboxAck(through_epoch=2)))
    p = d.tick()
    assert p.peer_acked_through == 2
    assert p.retained == 0, "acked frames pruned"


def test_driver_retains_on_send_failure_and_replays_on_reconnect() -> None:
    wire = Wire()
    d = _driver_at(wire, 0)
    wire.up = False
    d.enqueue(1, _dframe(0, 1))
    p = d.tick()
    assert p.sent == 0
    assert d.is_stalled(), "a failed send stalls the driver"
    assert p.retained == 1, "frame retained in the outbox despite the failure"
    assert wire.sent == []
    assert d.stalled_for(250) == 250, "stall duration is a host backoff signal"

    wire.up = True
    d.on_reconnect()
    p = d.tick()
    assert not d.is_stalled()
    assert p.sent == 1, "the retained frame is replayed"
    assert any(
        m.is_delta and m.delta is not None and m.delta.epoch == 1 for m in wire.sent
    ), "the replayed delta reached the sink"


def test_driver_applies_delta_and_advertises_receiver_cursor() -> None:
    wire = Wire()
    d = _driver_at(wire, 0)
    wire.inbound.append(_dframe(0, 1))
    p = d.tick()
    assert len(p.applied) == 1, "the applied frame is handed to the host"
    assert d.last_epoch() == 1
    assert any(
        m.is_outbox_ack and m.outbox_ack is not None and m.outbox_ack.through_epoch == 1
        for m in wire.sent
    ), "an OutboxAck advertising the new cursor was sent"


def test_driver_redelivery_is_idempotent_no_op() -> None:
    wire = Wire()
    d = _driver_at(wire, 0)
    wire.inbound.append(_dframe(0, 1))
    assert len(d.tick().applied) == 1
    wire.inbound.append(_dframe(0, 1))
    p = d.tick()
    assert len(p.applied) == 0, "already-applied re-delivery is ignored"
    assert d.last_epoch() == 1, "cursor does not double-advance"


def test_driver_requests_snapshot_on_inbound_gap() -> None:
    wire = Wire()
    d = _driver_at(wire, 2)
    wire.inbound.append(_dframe(3, 4))
    p = d.tick()
    assert p.resync_requested
    assert p.applied == [], "the gapped delta is not applied"
    assert any(
        m.is_resync_request
        and m.resync_request is not None
        and m.resync_request.from_epoch == 2
        for m in wire.sent
    ), "a ResyncRequest at the current cursor was emitted"


def test_driver_answers_resync_request_with_provider_snapshot() -> None:
    wire = Wire()
    d = _driver_at(wire, 0)
    wire.inbound.append(IpcMessage.of_resync_request(ResyncRequest(from_epoch=2)))
    p = d.tick()
    assert p.snapshots_served == 1
    assert any(
        m.is_snapshot and m.snapshot is not None and m.snapshot.epoch == 7
        for m in wire.sent
    ), "a covering snapshot (from_epoch + 5) was sent"


def test_driver_surfaces_source_read_error() -> None:
    wire = Wire()
    d = _driver_at(wire, 0)
    wire.source_err = True
    try:
        d.tick()
    except DriverError as e:
        assert e.kind == "Source"
    else:
        raise AssertionError("expected a DriverError(Source)")


def test_driver_gap_then_snapshot_converges() -> None:
    wire = Wire()
    d = _driver_at(wire, 2)
    wire.inbound.append(_dframe(4, 5))
    d.tick()
    assert d.last_epoch() == 2, "still stuck at the pre-gap cursor"
    wire.inbound.append(IpcMessage.of_snapshot(Snapshot(epoch=5)))
    p = d.tick()
    assert len(p.applied) == 1
    assert d.last_epoch() == 5, "snapshot restored convergence"
