"""Unit tests for the lazily IPC wire types and permission boundary."""

from __future__ import annotations

import pytest

from lazily.ipc import (
    SHM_BLOB_HEADER_LEN,
    Delta,
    DeltaOp,
    DeltaOp_SlotValue,
    EdgeSnapshot,
    IpcMessage,
    IpcValue_SharedBlob,
    NodeSnapshot,
    NodeState_Payload,
    OpKind,
    PeerPermissions,
    PermissionDenied,
    RemoteOp,
    ShmBlobArena,
    ShmBlobCapacityTooSmall,
    ShmBlobChecksumMismatch,
    ShmBlobDescriptorMismatch,
    ShmBlobRef,
    ShmBlobTooLarge,
    Snapshot,
)


# ---------------------------------------------------------------------------
# Round-trip / serialization
# ---------------------------------------------------------------------------


def test_snapshot_round_trip_bytes() -> None:
    snap = Snapshot(
        epoch=7,
        nodes=[
            NodeSnapshot.payload(1, "i32", bytes([1, 2, 3])),
            NodeSnapshot.opaque(2, "opaque-type"),
            NodeSnapshot.shared_blob(3, "text/plain", ShmBlobRef(0, 16, 1, 7, 999)),
        ],
        edges=[EdgeSnapshot(2, 1), EdgeSnapshot(3, 1)],
        roots=[1, 2],
    )
    message = IpcMessage.of_snapshot(snap)
    decoded = IpcMessage.decode_json(message.encode_json())
    assert decoded == message
    assert decoded.snapshot == snap


def test_delta_round_trip_all_ops() -> None:
    delta = Delta.next(
        40,
        [
            DeltaOp.cell_set(1, bytes([10])),
            DeltaOp.slot_value(2, bytes([20])),
            DeltaOp.invalidate(3),
            DeltaOp.node_add(4, "u64", NodeState_Payload(bytes([64]))),
            DeltaOp.node_remove(5),
            DeltaOp.edge_add(2, 1),
            DeltaOp.edge_remove(3, 1),
        ],
    )
    message = IpcMessage.of_delta(delta)
    decoded = IpcMessage.decode_json(message.encode_json())
    assert decoded == message
    assert decoded.delta.epoch == 41


def test_payload_serializes_as_byte_array_not_base64() -> None:
    op = DeltaOp.cell_set(1, bytes([10, 255, 0]))
    assert op.to_wire() == {"CellSet": {"node": 1, "payload": {"Inline": [10, 255, 0]}}}


def test_opaque_serializes_as_bare_string() -> None:
    node = NodeSnapshot.opaque(3, "opaque-type")
    assert node.to_wire()["state"] == "Opaque"


def test_ipc_value_blob_coercion() -> None:
    blob = ShmBlobRef(40, 17, 2, 9, 123)
    op = DeltaOp.slot_value(7, blob)
    assert isinstance(op, DeltaOp_SlotValue)
    assert isinstance(op.payload, IpcValue_SharedBlob)
    assert op.payload.blob == blob


def test_transport_agnostic_bytes() -> None:
    message = IpcMessage.of_delta(
        Delta.next(15, [DeltaOp.cell_set(1, b"cell"), DeltaOp.slot_value(2, b"slot")])
    )
    websocket_text = message.encode_json().decode("utf-8")
    webrtc_data = websocket_text.encode("utf-8")
    ffi_buffer = bytes(webrtc_data)

    assert IpcMessage.decode_json(websocket_text) == message
    assert IpcMessage.decode_json(webrtc_data) == message
    assert IpcMessage.decode_json(ffi_buffer) == message


# ---------------------------------------------------------------------------
# Epoch sequencing
# ---------------------------------------------------------------------------


def test_delta_is_next_after() -> None:
    delta = Delta.next(5, [])
    assert delta.base_epoch == 5
    assert delta.epoch == 6
    assert delta.is_next_after(5)
    assert not delta.is_next_after(4)
    assert not delta.is_next_after(6)


def test_delta_apply_status_apply() -> None:
    delta = Delta.next(5, [])
    status = delta.apply_status(5)
    assert status.is_apply
    assert not status.is_resync_required


def test_delta_apply_status_resync() -> None:
    delta = Delta.new(12, 13, [])
    status = delta.apply_status(10)
    assert status.is_resync_required
    assert (status.last_epoch, status.base_epoch, status.epoch) == (10, 12, 13)


# ---------------------------------------------------------------------------
# Permission boundary
# ---------------------------------------------------------------------------


def test_snapshot_filter_omits_unreadable_nodes() -> None:
    peer_a, peer_b = 1, 2
    perms = PeerPermissions()
    perms.allow_many(peer_a, OpKind.READ, [1, 2])

    snap = Snapshot(
        epoch=5,
        nodes=[
            NodeSnapshot.payload(1, "i32", bytes([1])),
            NodeSnapshot.payload(2, "i32", bytes([2])),
            NodeSnapshot.payload(3, "i32", bytes([3])),
        ],
        edges=[EdgeSnapshot(2, 1), EdgeSnapshot(3, 1)],
        roots=[1, 2, 3],
    )

    filtered = snap.filter_readable(perms, peer_a)
    assert len(filtered.nodes) == 2
    assert len(filtered.edges) == 1  # only 2->1 survives (3 unreadable)
    assert filtered.roots == [1, 2]

    empty = snap.filter_readable(perms, peer_b)
    assert empty.nodes == []
    assert empty.edges == []
    assert empty.roots == []


def test_delta_filter_omits_without_redaction() -> None:
    peer_a = 1
    perms = PeerPermissions()
    perms.allow_many(peer_a, OpKind.READ, [1, 2, 5])

    delta = Delta.next(
        8,
        [
            DeltaOp.cell_set(1, bytes([1])),
            DeltaOp.slot_value(2, bytes([2])),
            DeltaOp.invalidate(3),
            DeltaOp.node_add(4, "u8", NodeState_Payload(bytes([4]))),
            DeltaOp.node_remove(5),
            DeltaOp.edge_add(2, 1),
            DeltaOp.edge_remove(3, 1),
        ],
    )

    filtered = delta.filter_readable(perms, peer_a)
    kinds = [type(op).__name__ for op in filtered.ops]
    assert kinds == [
        "DeltaOp_CellSet",
        "DeltaOp_SlotValue",
        "DeltaOp_NodeRemove",
        "DeltaOp_EdgeAdd",
    ]


def test_permissions_independent_gating() -> None:
    perms = PeerPermissions()
    assert perms.allow(1, RemoteOp.read(10)) is True
    assert perms.allow(1, RemoteOp.read(10)) is False  # already held

    assert perms.is_allowed(1, RemoteOp.read(10))
    # Read grant never implies write or trigger.
    assert not perms.is_allowed(1, RemoteOp.write(10))
    assert not perms.is_allowed(1, RemoteOp.trigger_effect(10))


def test_permissions_check_raises() -> None:
    perms = PeerPermissions()
    perms.allow(1, RemoteOp.read(10))
    perms.check(1, RemoteOp.read(10))  # no raise
    with pytest.raises(PermissionDenied):
        perms.check(1, RemoteOp.write(10))


def test_permissions_revoke_and_prune() -> None:
    perms = PeerPermissions()
    perms.allow(1, RemoteOp.read(10))
    assert perms.peer_count() == 1
    assert perms.revoke(1, RemoteOp.read(10)) is True
    assert perms.revoke(1, RemoteOp.read(10)) is False
    assert perms.peer_count() == 0  # pruned empty peer entry


def test_permissions_revoke_peer() -> None:
    perms = PeerPermissions()
    perms.allow_many(1, OpKind.READ, [10, 11])
    assert perms.revoke_peer(1) is True
    assert perms.revoke_peer(1) is False
    assert perms.peer_count() == 0


def test_filter_readable_preserves_order() -> None:
    perms = PeerPermissions()
    perms.allow_many(1, OpKind.READ, [3, 1])
    assert perms.filter_readable(1, [1, 2, 3, 4]) == [1, 3]
    assert perms.filter_readable(99, [1, 2, 3]) == []


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_malformed_wire_rejected() -> None:
    with pytest.raises(ValueError):
        IpcMessage.from_wire({"Unknown": {}})
    with pytest.raises(ValueError):
        DeltaOp.from_wire({"Bogus": {}})


# ---------------------------------------------------------------------------
# ShmBlobArena host (parity with lazily-rs / lazily-zig)
# ---------------------------------------------------------------------------


def test_shm_blob_arena_round_trip() -> None:
    arena = ShmBlobArena.with_capacity(256)

    payload = b"hello lazily"
    desc = arena.write_blob(7, payload)

    assert desc.offset == 0
    assert desc.len == len(payload)
    assert desc.epoch == 7
    assert desc.generation == 1

    assert bytes(arena.read_blob(desc)) == payload


def test_shm_blob_arena_rejects_oversized_and_tiny_capacity() -> None:
    arena = ShmBlobArena.with_capacity(SHM_BLOB_HEADER_LEN + 4)
    with pytest.raises(ShmBlobTooLarge):
        arena.write_blob(0, b"abcdef")  # 6 > max_blob_len (4)
    with pytest.raises(ShmBlobCapacityTooSmall):
        ShmBlobArena.with_capacity(SHM_BLOB_HEADER_LEN)


def test_shm_blob_arena_from_buffer_wraps_external_storage() -> None:
    backing = bytearray(128)
    arena = ShmBlobArena.from_buffer(backing)  # aliasing, no copy/zero

    desc = arena.write_blob(1, b"abc")
    assert bytes(arena.read_blob(desc)) == b"abc"
    # the arena writes through into the caller-owned backing buffer
    assert backing[SHM_BLOB_HEADER_LEN : SHM_BLOB_HEADER_LEN + 3] == b"abc"


def test_shm_blob_arena_wraparound_invalidates_stale_descriptor() -> None:
    # capacity holds exactly one max-len blob (header + 5)
    arena = ShmBlobArena.with_capacity(SHM_BLOB_HEADER_LEN + 5)

    first = arena.write_blob(1, b"first")
    assert bytes(arena.read_blob(first)) == b"first"

    # next write wraps to offset 0, bumps generation, overwrites first
    second = arena.write_blob(2, b"2nd!!")
    assert second.offset == 0
    assert second.generation > first.generation

    with pytest.raises(ShmBlobDescriptorMismatch):
        arena.read_blob(first)
    assert bytes(arena.read_blob(second)) == b"2nd!!"


def test_shm_blob_arena_checksum_mismatch_on_corrupted_payload() -> None:
    backing = bytearray(128)
    arena = ShmBlobArena.from_buffer(backing)

    desc = arena.write_blob(0, b"payload")
    backing[SHM_BLOB_HEADER_LEN] ^= 0xFF  # corrupt first payload byte via alias
    with pytest.raises(ShmBlobChecksumMismatch):
        arena.read_blob(desc)


def test_shm_blob_descriptor_flows_through_ipc_value_shared_blob() -> None:
    arena = ShmBlobArena.with_capacity(128)
    desc = arena.write_blob(3, b"blob payload")

    value = IpcValue_SharedBlob(desc)
    assert value.blob == desc
    assert bytes(arena.read_blob(value.blob)) == b"blob payload"
