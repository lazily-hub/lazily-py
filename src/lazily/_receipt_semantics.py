"""Hand-written receipt semantics mixed into the generated wire types.

``_receipts_wire_gen`` is generated from ``lazily-spec/schemas/receipts.json``
and holds only declarations and codecs. Behaviour that is not a property of
the wire format stays here, mirroring lazily-rs ``src/receipt.rs`` and
lazily-go ``causal_receipts.go`` beside their generated files.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from ._receipts_wire_gen import CausalReceipt, CausalReceipts

_TERMINAL_OUTCOMES = frozenset({"applied", "rejected"})


class ReceiptOutcomeSemantics:
    """Terminality of a :class:`~lazily.ipc.ReceiptOutcome`.

    ``observed`` and ``accepted`` are **non-terminal** (an ACK-like
    transport/queue observation, never proof an effect happened); ``applied``
    and ``rejected`` are **terminal** (the generic outcome a domain fact
    refines). Mirrors ``LazilyFormal.Receipt.ReceiptOutcome``.
    """

    __slots__ = ()

    if TYPE_CHECKING:

        @property
        def value(self) -> str: ...

    @property
    def is_terminal(self) -> bool:
        """Whether this outcome completes the causation projection."""
        return self.value in _TERMINAL_OUTCOMES


class CausalReceiptsSemantics:
    """Batch helpers for a :class:`~lazily.ipc.CausalReceipts` frame.

    A frame is a standalone externally-tagged JSON object, **not** an
    :class:`~lazily.ipc.IpcMessage` variant, so transports may carry it on any
    channel. It may hold receipts for several ``causation_id`` s.
    """

    __slots__ = ()

    if TYPE_CHECKING:
        receipts: list[CausalReceipt]

        def to_wire(self) -> dict[str, Any]: ...

        @classmethod
        def from_wire(cls, value: Any) -> CausalReceipts: ...

    def group_by_causation(self) -> dict[str, list[CausalReceipt]]:
        """Group the frame's receipts by ``causation_id``.

        The map a caller iterates to build one :class:`ReceiptProjection` per
        causation id. Insertion order is the order receipts appear in the frame.
        """
        groups: dict[str, list[CausalReceipt]] = {}
        for receipt in self.receipts:
            groups.setdefault(receipt.causation_id, []).append(receipt)
        return groups

    def encode_json(self) -> bytes:
        """Serialize to transport-agnostic JSON bytes."""
        return json.dumps(self.to_wire(), separators=(",", ":")).encode("utf-8")

    @classmethod
    def decode_json(cls, data: bytes | str) -> CausalReceipts:
        """Parse JSON bytes (or str) produced by any lazily binding."""
        if isinstance(data, (bytes, bytearray)):
            data = bytes(data).decode("utf-8")
        return cls.from_wire(json.loads(data))
