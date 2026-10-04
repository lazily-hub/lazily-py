"""QueueCell op-log ``DeltaOp`` variants (``#lzdeltaqueueops``).

protocol.md § "QueueCell op-log delta form" (``#queue-oplog``) makes
``QueuePush`` / ``QueuePop`` / ``QueueClose`` ordinary ``DeltaOp`` variants:
``QueuePush`` shares ``CellSet``'s body, the other two share ``Invalidate``'s.
"""

from __future__ import annotations

import pytest

from lazily.ipc import (
    Delta,
    DeltaOp,
    DeltaOp_QueueClose,
    DeltaOp_QueuePop,
    DeltaOp_QueuePush,
    IpcMessage,
    IpcValue_Inline,
    IpcValue_SharedBlob,
    OpKind,
    PeerPermissions,
)
from lazily.transport import BlobRouter, InProcessBackend, spill_message


def _queue_delta() -> Delta:
    return Delta.next(
        3,
        [
            DeltaOp.queue_push(6, bytes([97])),
            DeltaOp.queue_pop(6),
            DeltaOp.queue_close(6),
        ],
    )


def test_queue_ops_wire_shape() -> None:
    wire = [op.to_wire() for op in _queue_delta().ops]
    assert wire == [
        {"QueuePush": {"node": 6, "payload": {"Inline": [97]}}},
        {"QueuePop": {"node": 6}},
        {"QueueClose": {"node": 6}},
    ]


@pytest.mark.parametrize("codec", ["json", "msgpack"])
def test_queue_ops_round_trip_each_codec(codec: str) -> None:
    msg = IpcMessage.of_delta(_queue_delta())
    if codec == "json":
        back = IpcMessage.decode_json(msg.encode_json())
    else:
        back = IpcMessage.decode_msgpack(msg.encode_msgpack())
    assert back.delta is not None
    ops = back.delta.ops
    assert [type(op) for op in ops] == [
        DeltaOp_QueuePush,
        DeltaOp_QueuePop,
        DeltaOp_QueueClose,
    ]
    push = ops[0]
    assert isinstance(push, DeltaOp_QueuePush)
    assert push.node == 6
    assert push.payload == IpcValue_Inline(bytes([97]))
    assert ops[1] == DeltaOp_QueuePop(6)
    assert ops[2] == DeltaOp_QueueClose(6)
    assert back.to_wire() == msg.to_wire()


def test_queue_ops_permission_filter_is_node_scoped() -> None:
    peer = 1
    perms = PeerPermissions()
    perms.allow_many(peer, OpKind.READ, [6])
    delta = Delta.next(
        0,
        [
            DeltaOp.queue_push(6, bytes([1])),
            DeltaOp.queue_push(7, bytes([2])),
            DeltaOp.queue_pop(6),
            DeltaOp.queue_pop(7),
            DeltaOp.queue_close(7),
            DeltaOp.queue_close(6),
        ],
    )
    filtered = delta.filter_readable(perms, peer)
    assert [(type(op).__name__, op.node) for op in filtered.ops] == [
        ("DeltaOp_QueuePush", 6),
        ("DeltaOp_QueuePop", 6),
        ("DeltaOp_QueueClose", 6),
    ]
    # A peer with no read grant sees none of them (omitted, not redacted).
    assert delta.filter_readable(perms, 2).ops == []


def test_queue_push_payload_spills_and_resolves() -> None:
    backend = InProcessBackend()
    big = bytes([0x51]) * 400
    msg = IpcMessage.of_delta(
        Delta.next(
            1,
            [DeltaOp.queue_push(6, big), DeltaOp.queue_pop(6), DeltaOp.queue_close(6)],
        )
    )
    assert spill_message(msg, backend, 64) == len(big)
    assert msg.delta is not None
    push, pop, close = msg.delta.ops
    assert isinstance(push, DeltaOp_QueuePush)
    assert isinstance(push.payload, IpcValue_SharedBlob)
    # The bodyless ops carry no bytes and are untouched.
    assert pop == DeltaOp_QueuePop(6)
    assert close == DeltaOp_QueueClose(6)
    # The spilled descriptor survives the wire and resolves to the bytes.
    back = IpcMessage.decode_json(msg.encode_json())
    assert back.delta is not None
    wire_push = back.delta.ops[0]
    assert isinstance(wire_push, DeltaOp_QueuePush)
    router = BlobRouter().register(backend)
    assert bytes(router.resolve(wire_push.payload)) == big


def test_queue_push_below_threshold_stays_inline() -> None:
    msg = IpcMessage.of_delta(Delta.next(1, [DeltaOp.queue_push(6, bytes([1, 2]))]))
    assert spill_message(msg, InProcessBackend(), 64) == 0
    assert msg.delta is not None
    push = msg.delta.ops[0]
    assert isinstance(push, DeltaOp_QueuePush)
    assert push.payload == IpcValue_Inline(bytes([1, 2]))


@pytest.mark.parametrize(
    "wire",
    [
        {"QueuePush": {"payload": {"Inline": [1]}}},
        {"QueuePush": {"node": 6}},
        {"QueuePop": {}},
        {"QueueClose": {}},
    ],
)
def test_queue_ops_missing_required_field_rejected(wire: dict) -> None:
    # Same strictness as CellSet / Invalidate: a required field is not defaulted,
    # and the refusal names it in the ValueError family every decode raises.
    with pytest.raises(ValueError, match="missing field"):
        DeltaOp.from_wire(wire)
