"""Cross-language conformance tests for the lazily IPC wire protocol.

Each test loads a canonical JSON fixture and validates that lazily-py agrees on
the wire format. The fixtures are the same files the Rust and Zig bindings test
against, so all implementations stay byte-compatible.

The canonical fixtures live in the sibling ``lazily-spec/conformance`` repo; a
vendored copy under ``tests/conformance`` keeps this binding's standalone CI
self-contained. The spec copy is preferred when present.

Fixture schema::

    {
      "description": "…",
      "protocol_version": 1,
      "kind": "Snapshot" | "Delta",
      "assertions": { … language-agnostic field checks … },
      "wire": { <IpcMessage as serde_json> }
    }
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lazily.ipc import (
    DeltaOp_SlotValue,
    IpcMessage,
    IpcValue_SharedBlob,
    NodeState_Opaque,
    NodeState_Payload,
    NodeState_SharedBlob,
    ShmBlobArena,
)


_LOCAL_FIXTURES = Path(__file__).resolve().parent / "conformance"
_SPEC_FIXTURES = Path(__file__).resolve().parents[2] / "lazily-spec" / "conformance"


def load_fixture(name: str) -> dict:
    spec_path = _SPEC_FIXTURES / name
    path = spec_path if spec_path.exists() else _LOCAL_FIXTURES / name
    fixture = json.loads(path.read_text())
    assert fixture["protocol_version"] == 1, (
        f"fixture {name} uses unsupported protocol version"
    )
    return fixture


def parse_wire(fixture: dict) -> IpcMessage:
    return IpcMessage.from_wire(fixture["wire"])


def assert_round_trip_json(message: IpcMessage, fixture: dict) -> None:
    # Round-trip parity: re-serializing the parsed message yields the same
    # canonical JSON object as the fixture wire.
    assert message.to_wire() == fixture["wire"], (
        f"round-trip JSON mismatch for fixture: {fixture['description']}"
    )
    # And bytes decode back to an equal message.
    assert IpcMessage.decode_json(message.encode_json()) == message


# ---------------------------------------------------------------------------
# Snapshot fixtures
# ---------------------------------------------------------------------------


def test_conformance_snapshot_minimal() -> None:
    fixture = load_fixture("snapshot_minimal.json")
    assert fixture["kind"] == "Snapshot"
    a = fixture["assertions"]

    message = parse_wire(fixture)
    assert message.is_snapshot
    snap = message.snapshot

    assert snap.epoch == a["epoch"]
    assert len(snap.nodes) == a["node_count"]
    assert len(snap.edges) == a["edge_count"]
    assert len(snap.roots) == a["root_count"]
    assert snap.nodes[0].type_tag == a["first_node_type_tag"]
    assert isinstance(snap.nodes[0].state, NodeState_Payload)

    assert_round_trip_json(message, fixture)


def test_conformance_snapshot_multi_node() -> None:
    fixture = load_fixture("snapshot_multi_node.json")
    assert fixture["kind"] == "Snapshot"
    a = fixture["assertions"]

    message = parse_wire(fixture)
    snap = message.snapshot
    assert snap.epoch == 7
    assert len(snap.nodes) == 3
    assert len(snap.edges) == 2
    assert len(snap.roots) == 2

    opaque_id = a["opaque_node_id"]
    opaque_node = next(n for n in snap.nodes if n.node == opaque_id)
    assert isinstance(opaque_node.state, NodeState_Opaque)

    assert_round_trip_json(message, fixture)


def test_conformance_snapshot_shared_blob() -> None:
    fixture = load_fixture("snapshot_shared_blob.json")
    assert fixture["kind"] == "Snapshot"

    message = parse_wire(fixture)
    snap = message.snapshot
    assert snap.epoch == 9
    assert len(snap.nodes) == 1

    state = snap.nodes[0].state
    assert isinstance(state, NodeState_SharedBlob)
    assert state.blob.offset == 0
    assert state.blob.len == 16
    assert state.blob.epoch == 9

    assert_round_trip_json(message, fixture)


# ---------------------------------------------------------------------------
# Delta fixtures
# ---------------------------------------------------------------------------


def test_conformance_delta_sequential() -> None:
    fixture = load_fixture("delta_sequential.json")
    assert fixture["kind"] == "Delta"
    a = fixture["assertions"]

    message = parse_wire(fixture)
    assert message.is_delta
    delta = message.delta

    assert delta.base_epoch == a["base_epoch"]
    assert delta.epoch == a["epoch"]
    assert delta.is_next_after(a["base_epoch"])
    assert not delta.is_next_after(a["base_epoch"] - 1)

    assert len(delta.ops) == a["op_count"]

    seen = {type(op).__name__ for op in delta.ops}
    assert seen == {
        "DeltaOp_CellSet",
        "DeltaOp_SlotValue",
        "DeltaOp_Invalidate",
        "DeltaOp_NodeAdd",
        "DeltaOp_NodeRemove",
        "DeltaOp_EdgeAdd",
        "DeltaOp_EdgeRemove",
    }
    assert len(seen) == 7

    assert_round_trip_json(message, fixture)


def test_conformance_delta_non_sequential() -> None:
    fixture = load_fixture("delta_non_sequential.json")
    assert fixture["kind"] == "Delta"

    message = parse_wire(fixture)
    delta = message.delta
    assert delta.base_epoch == 12
    assert delta.epoch == 13
    assert delta.is_next_after(12)
    assert not delta.is_next_after(10)

    status = delta.apply_status(10)
    assert status.is_resync_required
    assert status.last_epoch == 10
    assert status.base_epoch == 12
    assert status.epoch == 13

    assert_round_trip_json(message, fixture)


def test_conformance_delta_shared_blob() -> None:
    fixture = load_fixture("delta_shared_blob.json")
    assert fixture["kind"] == "Delta"

    message = parse_wire(fixture)
    delta = message.delta
    assert delta.base_epoch == 8
    assert delta.epoch == 9
    assert len(delta.ops) == 1

    op = delta.ops[0]
    assert isinstance(op, DeltaOp_SlotValue)
    assert isinstance(op.payload, IpcValue_SharedBlob)
    assert op.payload.blob.offset == 40
    assert op.payload.blob.len == 17
    assert op.payload.blob.epoch == 9

    assert_round_trip_json(message, fixture)


# ---------------------------------------------------------------------------
# ShmBlobArena host fixture (not a wire type — locks the arena byte contract)
# ---------------------------------------------------------------------------


def test_conformance_arena_blob() -> None:
    fixture = load_fixture("arena_blob.json")
    assert fixture["kind"] == "Arena"
    a = fixture["assertions"]

    arena = ShmBlobArena.with_capacity(fixture["input"]["capacity"])
    payload = bytes(fixture["input"]["payload"])
    desc = arena.write_blob(fixture["input"]["epoch"], payload)

    expected_desc = fixture["expected"]["descriptor"]
    assert desc.offset == expected_desc["offset"]
    assert desc.len == expected_desc["len"]
    assert desc.generation == expected_desc["generation"]
    assert desc.epoch == expected_desc["epoch"]
    assert desc.checksum == expected_desc["checksum"]

    # 40-byte LZSH header byte-identical across rs / py / zig
    buf = arena.buffer()
    header_len = a["header_len"]
    assert bytes(buf[0:header_len]) == bytes(fixture["expected"]["header_bytes"])
    assert bytes(buf[header_len : header_len + len(payload)]) == bytes(
        fixture["expected"]["payload_region"]
    )

    # round-trip
    assert bytes(arena.read_blob(desc)) == payload


# ---------------------------------------------------------------------------
# Every fixture round-trips, parametrized
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "snapshot_minimal.json",
        "snapshot_multi_node.json",
        "snapshot_shared_blob.json",
        "delta_sequential.json",
        "delta_non_sequential.json",
        "delta_shared_blob.json",
    ],
)
def test_fixture_round_trips(name: str) -> None:
    fixture = load_fixture(name)
    message = parse_wire(fixture)
    assert message.to_wire() == fixture["wire"]
    assert IpcMessage.decode_json(message.encode_json()) == message
