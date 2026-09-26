"""Client-tier durable ingress and advisory projection transport.

The transport is injected and NATS-compatible; this module never owns a broker,
database transaction, or durable-owner transition.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Protocol, TypeVar, cast

from .ingress_core import IngressAdmission, IngressCore, IngressEnvelope, IngressPolicy
from .merge import KeepLatest


__all__ = [
    "DURABLE_CAPABILITIES",
    "DURABLE_PROTOCOL_VERSION",
    "DurableBrokerPubAck",
    "DurableClient",
    "DurableHostOutcome",
    "DurableHostReceipt",
    "DurableIngressClassification",
    "DurableIngressEnvelope",
    "DurableNATSTransport",
    "DurableProjectionCompleteness",
    "DurableProjectionEvent",
    "DurableProjectionFingerprint",
    "DurableProjectionFingerprintComparison",
    "DurableProjectionHealth",
    "compare_durable_projection_fingerprints",
]

DURABLE_PROTOCOL_VERSION = 1
DURABLE_CAPABILITIES = {
    "core": True,
    "client": True,
    "durable_host": False,
    "distributed_host": False,
    "accelerated_host": False,
}


def _non_empty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"invalid_{name}")
    return value


def _positive_u32(value: object, name: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= 0xFFFFFFFF
    ):
        raise ValueError(f"invalid_{name}")
    return value


@dataclass(frozen=True, slots=True)
class DurableIngressEnvelope:
    protocol_version: int
    message_id: str
    schema_version: int
    codec_version: int
    payload: bytes

    @classmethod
    def from_wire(cls, value: object) -> DurableIngressEnvelope:
        if not isinstance(value, dict):
            raise ValueError("invalid_envelope")
        mapping = cast("dict[str, object]", value)
        # Version is checked before the payload is inspected.
        if mapping.get("protocol_version") != DURABLE_PROTOCOL_VERSION:
            raise ValueError("unsupported_protocol_version")
        message_id = _non_empty(mapping.get("message_id"), "message_id")
        schema_version = _positive_u32(mapping.get("schema_version"), "schema_version")
        codec_version = _positive_u32(mapping.get("codec_version"), "codec_version")
        raw_payload = mapping.get("payload")
        if not isinstance(raw_payload, list) or any(
            not isinstance(item, int) or isinstance(item, bool) or not 0 <= item <= 255
            for item in raw_payload
        ):
            raise ValueError("invalid_payload")
        payload_values = cast("list[int]", raw_payload)
        return cls(
            DURABLE_PROTOCOL_VERSION,
            message_id,
            schema_version,
            codec_version,
            bytes(payload_values),
        )

    def to_wire(self) -> dict[str, object]:
        self.validate()
        return {
            "protocol_version": self.protocol_version,
            "message_id": self.message_id,
            "schema_version": self.schema_version,
            "codec_version": self.codec_version,
            "payload": list(self.payload),
        }

    def encode(self) -> bytes:
        return json.dumps(self.to_wire(), separators=(",", ":")).encode()

    def validate(self) -> None:
        if self.protocol_version != DURABLE_PROTOCOL_VERSION:
            raise ValueError("unsupported_protocol_version")
        _non_empty(self.message_id, "message_id")
        _positive_u32(self.schema_version, "schema_version")
        _positive_u32(self.codec_version, "codec_version")
        if not isinstance(self.payload, bytes):
            raise ValueError("invalid_payload")

    @classmethod
    def decode(cls, value: bytes) -> DurableIngressEnvelope:
        return cls.from_wire(json.loads(value))


@dataclass(frozen=True, slots=True)
class DurableBrokerPubAck:
    """Broker storage acknowledgement, never a host commit receipt."""

    stream: str
    sequence: int
    duplicate: bool = False


class DurableHostOutcome(StrEnum):
    COMMITTED = "committed"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class DurableHostReceipt:
    protocol_version: int
    receipt_id: str
    message_id: str
    outcome: DurableHostOutcome
    owner_position: int

    @classmethod
    def from_wire(cls, value: object) -> DurableHostReceipt:
        if not isinstance(value, dict):
            raise ValueError("invalid_receipt")
        mapping = cast("dict[str, object]", value)
        if mapping.get("protocol_version") != DURABLE_PROTOCOL_VERSION:
            raise ValueError("unsupported_protocol_version")
        try:
            outcome = DurableHostOutcome(mapping.get("outcome"))
        except ValueError as error:
            raise ValueError("invalid_receipt_outcome") from error
        position = mapping.get("owner_position")
        if not isinstance(position, int) or isinstance(position, bool) or position < 0:
            raise ValueError("invalid_owner_position")
        return cls(
            DURABLE_PROTOCOL_VERSION,
            _non_empty(mapping.get("receipt_id"), "receipt_id"),
            _non_empty(mapping.get("message_id"), "message_id"),
            outcome,
            position,
        )

    def to_wire(self) -> dict[str, object]:
        self.validate()
        wire = asdict(self)
        wire["outcome"] = self.outcome.value
        return wire

    def validate(self) -> None:
        if self.protocol_version != DURABLE_PROTOCOL_VERSION:
            raise ValueError("unsupported_protocol_version")
        _non_empty(self.receipt_id, "receipt_id")
        _non_empty(self.message_id, "message_id")
        if not isinstance(self.outcome, DurableHostOutcome):
            raise ValueError("invalid_receipt_outcome")
        if (
            not isinstance(self.owner_position, int)
            or isinstance(self.owner_position, bool)
            or self.owner_position < 0
        ):
            raise ValueError("invalid_owner_position")


class DurableIngressClassification(StrEnum):
    FIRST = "first"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class DurableProjectionFingerprint:
    projection_id: str
    source_position: int
    fingerprint: str
    completeness: str
    may_authorize_transition: bool

    @classmethod
    def from_wire(cls, value: object) -> DurableProjectionFingerprint:
        if not isinstance(value, dict):
            raise ValueError("invalid_projection_fingerprint")
        mapping = cast("dict[str, object]", value)
        position = mapping.get("source_position")
        if not isinstance(position, int) or isinstance(position, bool) or position < 0:
            raise ValueError("invalid_source_position")
        fingerprint = mapping.get("fingerprint")
        if (
            not isinstance(fingerprint, str)
            or re.fullmatch(r"[0-9a-f]+", fingerprint) is None
        ):
            raise ValueError("invalid_fingerprint")
        completeness = mapping.get("completeness")
        if completeness not in {"complete_history", "latest_state_only"}:
            raise ValueError("invalid_completeness")
        completeness_value = cast("str", completeness)
        if mapping.get("may_authorize_transition") is not False:
            raise ValueError("projection_is_advisory")
        return cls(
            _non_empty(mapping.get("projection_id"), "projection_id"),
            position,
            fingerprint,
            completeness_value,
            False,
        )


@dataclass(frozen=True, slots=True)
class DurableProjectionFingerprintComparison:
    same_source: bool
    same_fingerprint: bool
    same_completeness: bool
    equivalent: bool


def compare_durable_projection_fingerprints(
    left: DurableProjectionFingerprint, right: DurableProjectionFingerprint
) -> DurableProjectionFingerprintComparison:
    same_source = (
        left.projection_id == right.projection_id
        and left.source_position == right.source_position
    )
    same_fingerprint = left.fingerprint == right.fingerprint
    same_completeness = left.completeness == right.completeness
    return DurableProjectionFingerprintComparison(
        same_source,
        same_fingerprint,
        same_completeness,
        same_source and same_fingerprint and same_completeness,
    )


class DurableProjectionCompleteness(StrEnum):
    COMPLETE_HISTORY = "complete_history"


class DurableProjectionHealth(StrEnum):
    HEALTHY = "healthy"
    LAGGING = "lagging"
    DRIFTED = "drifted"


P = TypeVar("P")


@dataclass(frozen=True, slots=True)
class DurableProjectionEvent[P]:
    protocol_version: int
    owner_id: str
    generation: int
    source_position: int
    projection_version: int
    schema_version: int
    codec_version: int
    completeness: DurableProjectionCompleteness
    entries: P
    source_fingerprint: str
    projection_fingerprint: str
    health: DurableProjectionHealth
    may_authorize_transition: bool = False

    def validate(self) -> None:
        if self.protocol_version != DURABLE_PROTOCOL_VERSION:
            raise ValueError("unsupported_protocol_version")
        _non_empty(self.owner_id, "owner_id")
        for name in (
            "generation",
            "source_position",
            "projection_version",
            "schema_version",
            "codec_version",
        ):
            _positive_u32(getattr(self, name), name)
        if self.completeness is not DurableProjectionCompleteness.COMPLETE_HISTORY:
            raise ValueError("projection_requires_complete_history")
        if self.may_authorize_transition:
            raise ValueError("projection_is_advisory")
        _non_empty(self.source_fingerprint, "source_fingerprint")
        _non_empty(self.projection_fingerprint, "projection_fingerprint")


class DurableNATSTransport(Protocol):
    def publish(self, subject: str, payload: bytes) -> DurableBrokerPubAck: ...
    def subscribe(self, subject: str) -> object: ...


class DurableClient[P]:
    """Core+Client tier adapter; it provides no durable-host authority."""

    def __init__(self, transport: DurableNATSTransport) -> None:
        if not callable(getattr(transport, "publish", None)) or not callable(
            getattr(transport, "subscribe", None)
        ):
            raise TypeError("transport must provide publish and subscribe")
        self._transport = transport
        self._projections: IngressCore[str, DurableProjectionEvent[P]] = IngressCore(
            IngressPolicy(), KeepLatest
        )
        self._latest: dict[str, DurableProjectionEvent[P]] = {}
        self._receipts: dict[str, DurableHostReceipt] = {}
        self._messages: dict[str, bytes] = {}
        self._delivery_order: list[str] = []
        self._projection_identities: dict[
            tuple[str, int, int], tuple[str, str, int, int]
        ] = {}
        self._applied_projection_positions: dict[str, list[int]] = {}

    def publish_ingress(
        self, subject: str, envelope: DurableIngressEnvelope
    ) -> DurableBrokerPubAck:
        _non_empty(subject, "subject")
        return self._transport.publish(subject, envelope.encode())

    def subscribe(self, subject: str) -> object:
        _non_empty(subject, "subject")
        return self._transport.subscribe(subject)

    def observe_ingress(
        self, envelope: DurableIngressEnvelope
    ) -> DurableIngressClassification:
        wire = envelope.encode()
        self._delivery_order.append(envelope.message_id)
        prior = self._messages.get(envelope.message_id)
        if prior is None:
            self._messages[envelope.message_id] = wire
            return DurableIngressClassification.FIRST
        if prior == wire:
            return DurableIngressClassification.DUPLICATE
        return DurableIngressClassification.CONFLICT

    def observed_message_ids(self) -> list[str]:
        return list(self._delivery_order)

    def observe_host_receipt(self, receipt: DurableHostReceipt) -> str:
        receipt.validate()
        prior = self._receipts.get(receipt.receipt_id)
        if prior is not None and prior != receipt:
            raise ValueError(f"conflicting durable receipt {receipt.receipt_id!r}")
        self._receipts[receipt.receipt_id] = receipt
        return "recorded" if prior is None else "duplicate"

    def host_receipt(self, receipt_id: str) -> DurableHostReceipt | None:
        return self._receipts.get(receipt_id)

    def observe_projection(self, event: DurableProjectionEvent[P]) -> IngressAdmission:
        event.validate()
        identity = (event.owner_id, event.generation, event.source_position)
        fingerprint = (
            event.source_fingerprint,
            event.projection_fingerprint,
            event.schema_version,
            event.codec_version,
        )
        prior = self._projection_identities.get(identity)
        if prior is not None and prior != fingerprint:
            raise ValueError(
                f"conflicting durable projection at {event.owner_id}/{event.source_position}"
            )
        self._projection_identities[identity] = fingerprint
        before_authority = self._projections.authority(event.owner_id)
        before = (
            -1
            if before_authority is None or before_authority.delivered_through is None
            else before_authority.delivered_through
        )
        _, admission = self._projections.admit(
            IngressEnvelope(
                event.owner_id,
                event.generation,
                event.source_position - 1,
                event.source_position,
                event,
            )
        )
        after_authority = self._projections.authority(event.owner_id)
        after = (
            before
            if after_authority is None or after_authority.delivered_through is None
            else after_authority.delivered_through
        )
        if after > before:
            self._applied_projection_positions.setdefault(event.owner_id, []).extend(
                sequence + 1 for sequence in range(before + 1, after + 1)
            )
        _, projected = self._projections.drain(event.owner_id)
        if projected is not None:
            self._latest[event.owner_id] = projected
        return admission

    def projection(self, owner_id: str) -> DurableProjectionEvent[P] | None:
        return self._latest.get(owner_id)

    def applied_source_positions(self, owner_id: str) -> list[int]:
        return list(self._applied_projection_positions.get(owner_id, ()))
