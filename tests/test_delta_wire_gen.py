"""Strict decoding of the generated delta wire types (#lzwiremodel5).

``lazily._delta_wire_gen`` is generated from lazily-spec ``schemas/delta.json``.
These tests pin what the generated decoder refuses that the hand-written one
accepted, and the leniency the protocol requires it to keep.
"""

from __future__ import annotations

from typing import Any

import pytest

from lazily.ipc import (
    Delta,
    DeltaOp,
    DeltaOp_NodeAdd,
    IpcValue,
    IpcValue_Inline,
    NodeKey,
    NodeState,
    NodeState_Opaque,
    ShmBlobRef,
)


def _frame(*ops: Any) -> dict[str, Any]:
    return {"base_epoch": 1, "epoch": 2, "ops": list(ops)}


@pytest.mark.parametrize(
    ("wire", "message"),
    [
        (_frame({"Invalidate": {"node": 1, "extra": 0}}), "unknown field 'extra'"),
        (_frame({"Invalidate": {"node": "1"}}), "expected an unsigned integer"),
        (_frame({"Invalidate": {"node": True}}), "expected an unsigned integer"),
        (_frame({"Invalidate": {"node": 1}, "EdgeAdd": {}}), "single-key object"),
        (_frame({"invalidate": {"node": 1}}), "unknown DeltaOp variant"),
        (
            _frame({"CellSet": {"node": 1, "payload": {"Inline": [1, 256]}}}),
            "array of bytes",
        ),
        (
            _frame({"CellSet": {"node": 1, "payload": {"Inline": "AQ=="}}}),
            "array of bytes",
        ),
        (
            _frame({"NodeAdd": {"node": 1, "type_tag": "t", "state": "opaque"}}),
            "unit variant",
        ),
        (
            _frame(
                {"NodeAdd": {"node": 1, "type_tag": "t", "state": {"Opaque": None}}}
            ),
            "unknown NodeState",
        ),
        (
            _frame(
                {"NodeAdd": {"node": 1, "type_tag": "t", "state": "Opaque", "key": 7}}
            ),
            "NodeKey",
        ),
        ({"base_epoch": 1, "epoch": 2}, "missing field 'ops'"),
        ({"base_epoch": -1, "epoch": 2, "ops": []}, "base_epoch must be in 0"),
        ({"base_epoch": 1, "epoch": 2, "ops": [], "x": 0}, "unknown field 'x'"),
    ],
)
def test_generated_decoder_refuses_off_schema_frames(
    wire: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        Delta.from_wire(wire)


@pytest.mark.parametrize("key", [None, "absent"])
def test_node_add_key_absent_and_null_both_read_as_absent(key: str | None) -> None:
    body: dict[str, Any] = {"node": 1, "type_tag": "t", "state": "Opaque"}
    if key is None:
        body["key"] = None
    op = DeltaOp.from_wire({"NodeAdd": body})
    assert op == DeltaOp_NodeAdd(1, "t", NodeState_Opaque())
    # An encoder omits the absent key (protocol.md § NodeKey).
    assert "key" not in op.to_wire()["NodeAdd"]


def test_node_add_key_round_trips_through_the_hand_written_node_key() -> None:
    op = DeltaOp.node_add(4, "u64", NodeState_Opaque(), NodeKey.new("scores/alice"))
    wire = op.to_wire()
    assert wire["NodeAdd"]["key"] == "scores/alice"
    assert DeltaOp.from_wire(wire) == op


def test_blob_backend_null_stays_lenient_through_the_external_codec() -> None:
    # ShmBlobRef keeps its hand-written codec: an explicit `backend: null` reads
    # as the absent form (#lzblobbackendstrict), which the schema alone rejects.
    blob = {
        "offset": 0,
        "len": 1,
        "generation": 1,
        "epoch": 1,
        "checksum": 9,
        "backend": None,
    }
    value = IpcValue.from_wire({"SharedBlob": blob})
    assert value.to_wire() == {"SharedBlob": ShmBlobRef(0, 1, 1, 1, 9).to_wire()}


def test_constructing_an_out_of_range_node_id_is_refused() -> None:
    with pytest.raises(ValueError, match="node must be in 0"):
        DeltaOp.invalidate(-1)
    with pytest.raises(ValueError, match="node must be in 0"):
        DeltaOp.invalidate(2**64)
    assert DeltaOp.invalidate(2**64 - 1).to_wire() == {
        "Invalidate": {"node": 2**64 - 1}
    }


def test_semantics_survive_generation() -> None:
    assert IpcValue.of(b"ab") == IpcValue_Inline(b"ab")
    delta = Delta.next(7, [DeltaOp.invalidate(1)])
    assert (delta.base_epoch, delta.epoch) == (7, 8)
    assert delta.apply_status(7).is_apply
    assert delta.apply_status(6).is_resync_required
    assert isinstance(NodeState.from_wire("Opaque"), NodeState_Opaque)
