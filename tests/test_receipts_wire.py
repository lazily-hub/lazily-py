"""Generated receipt wire types decode strictly (#lzwiremodel2).

``_receipts_wire_gen`` is generated from lazily-spec ``schemas/receipts.json``.
The schema closes every record (``additionalProperties: false``) and requires
``reason`` / ``payload_hash`` on the wire even when they are ``null``; the
generated decoder enforces both instead of defaulting or ignoring.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from lazily import CausalReceipt, CausalReceipts, ReceiptOutcome


_RECEIPT: dict[str, Any] = {
    "receipt_id": "receipt-1",
    "causation_id": "cmd-1",
    "observer": "peer-a",
    "generation": 7,
    "outcome": "applied",
    "reason": None,
    "payload_hash": "sha256:abc",
}


def _frame(*receipts: dict[str, Any]) -> dict[str, Any]:
    return {"CausalReceipts": {"receipts": list(receipts)}}


def test_round_trip_and_semantics() -> None:
    frame = CausalReceipts.from_wire(_frame(_RECEIPT))
    assert frame.receipts[0].outcome is ReceiptOutcome.APPLIED
    assert frame.receipts[0].outcome.is_terminal
    assert not ReceiptOutcome.ACCEPTED.is_terminal
    assert frame.to_wire() == _frame(_RECEIPT)
    assert list(frame.group_by_causation()) == ["cmd-1"]


@pytest.mark.parametrize("key", ["reason", "payload_hash", "generation"])
def test_missing_required_field_is_rejected(key: str) -> None:
    receipt = copy.deepcopy(_RECEIPT)
    del receipt[key]
    with pytest.raises(ValueError, match=f"CausalReceipt: missing field '{key}'"):
        CausalReceipts.from_wire(_frame(receipt))


def test_unknown_field_is_rejected() -> None:
    receipt = {**_RECEIPT, "extra": 1}
    with pytest.raises(ValueError, match="CausalReceipt: unknown field 'extra'"):
        CausalReceipt.from_wire(receipt)
    with pytest.raises(ValueError, match="CausalReceipts: unknown field 'x'"):
        CausalReceipts.from_wire({"CausalReceipts": {"receipts": [], "x": 0}})
    with pytest.raises(ValueError, match="ReceiptMessage: unknown field 'Other'"):
        CausalReceipts.from_wire({"CausalReceipts": {"receipts": []}, "Other": {}})


def test_missing_receipts_list_is_rejected() -> None:
    with pytest.raises(ValueError, match="missing field 'receipts'"):
        CausalReceipts.from_wire({"CausalReceipts": {}})


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("generation", -1, "generation must be in 0..=2"),
        ("generation", 2**64, "generation must be in 0..=2"),
        ("generation", True, "expected an unsigned integer"),
        ("generation", 1.0, "expected an unsigned integer"),
        ("receipt_id", "", "receipt_id must be a non-empty string"),
        ("observer", 3, "expected a string"),
        ("reason", 3, "expected a string"),
        ("outcome", "done", "unknown ReceiptOutcome"),
    ],
)
def test_ill_typed_field_is_rejected(key: str, value: Any, message: str) -> None:
    receipt = {**_RECEIPT, key: value}
    with pytest.raises(ValueError, match=message):
        CausalReceipt.from_wire(receipt)


def test_u64_max_generation_is_accepted() -> None:
    receipt = {**_RECEIPT, "generation": 2**64 - 1}
    assert CausalReceipt.from_wire(receipt).to_wire() == receipt
