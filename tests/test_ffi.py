"""C-ABI FFI boundary — Status / MessageKind discriminants and canonical-bytes
encode/decode.

Validates the ``lazily-spec/schemas/ffi.json`` contract: the FFI kind
discriminant MUST include ``CrdtSync = 3``; encode/decode round-trips an
:class:`~lazily.ipc.IpcMessage` through canonical JSON bytes with the correct
status + kind.
"""

from __future__ import annotations

from lazily import (
    CrdtSync,
    Delta,
    LazilyFfiMessageKind,
    LazilyFfiStatus,
    NodeKey,
    NodeSnapshot,
    Snapshot,
    WireStamp,
    decode_message,
    encode_message,
    ffi_bytes_of,
    kind_of,
)
from lazily.ipc import CrdtOp, IpcMessage


def _snapshot_msg() -> IpcMessage:
    return IpcMessage.of_snapshot(
        Snapshot(
            epoch=1,
            nodes=[NodeSnapshot.payload(0, "i32", bytes([1, 2, 3]))],
            edges=[],
            roots=[0],
        )
    )


def _delta_msg() -> IpcMessage:
    return IpcMessage.of_delta(Delta(base_epoch=1, epoch=2, ops=[]))


def _crdt_msg() -> IpcMessage:
    return IpcMessage.of_crdt_sync(
        CrdtSync(
            frontier=[],
            ops=[
                CrdtOp.keyed(
                    node=0,
                    key=NodeKey.new("k"),
                    stamp=WireStamp(wall_time=1, logical=1, peer=2),
                    state=bytes([9]),
                )
            ],
        )
    )


def test_status_enum_matches_spec() -> None:
    assert int(LazilyFfiStatus.Ok) == 0
    assert int(LazilyFfiStatus.Empty) == 1
    assert int(LazilyFfiStatus.NullPointer) == 2
    assert int(LazilyFfiStatus.InvalidMessage) == 3
    assert int(LazilyFfiStatus.EncodeFailed) == 4
    assert int(LazilyFfiStatus.Panic) == 5


def test_message_kind_includes_crdt_sync_3() -> None:
    # The spec mandates the FFI kind discriminant MUST include CrdtSync = 3.
    assert int(LazilyFfiMessageKind.Unknown) == 0
    assert int(LazilyFfiMessageKind.Snapshot) == 1
    assert int(LazilyFfiMessageKind.Delta) == 2
    assert int(LazilyFfiMessageKind.CrdtSync) == 3


def test_kind_of_dispatch() -> None:
    assert kind_of(_snapshot_msg()) is LazilyFfiMessageKind.Snapshot
    assert kind_of(_delta_msg()) is LazilyFfiMessageKind.Delta
    assert kind_of(_crdt_msg()) is LazilyFfiMessageKind.CrdtSync


def test_encode_decode_round_trip() -> None:
    for msg in (_snapshot_msg(), _delta_msg(), _crdt_msg()):
        status, kind, payload = encode_message(msg)
        assert status is LazilyFfiStatus.Ok
        assert kind is kind_of(msg)
        assert payload
        dec_status, decoded = decode_message(payload)
        assert dec_status is LazilyFfiStatus.Ok
        assert decoded is not None
        assert decoded.to_wire() == msg.to_wire()


def test_decode_empty_is_empty_status() -> None:
    status, decoded = decode_message(b"")
    assert status is LazilyFfiStatus.Empty
    assert decoded is None


def test_decode_invalid_is_invalid_status() -> None:
    status, decoded = decode_message(b"not json at all")
    assert status is LazilyFfiStatus.InvalidMessage
    assert decoded is None


def test_ffi_bytes_carries_ptr_and_len() -> None:
    payload = b"hello-lazily"
    buf, owning = ffi_bytes_of(payload)
    assert int(buf.len) == len(payload)
    # ptr references the owning bytes object's buffer.
    assert bytes(buf.ptr).startswith(b"hello")
    _ = owning  # keep alive
