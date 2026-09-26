from __future__ import annotations

import json

import pytest
from conformance_assert import assert_key, corpus_path, instrument, require_flag

from lazily import (
    DURABLE_CAPABILITIES,
    DurableBrokerPubAck,
    DurableClient,
    DurableHostReceipt,
    DurableIngressEnvelope,
    DurableProjectionCompleteness,
    DurableProjectionEvent,
    DurableProjectionFingerprint,
    DurableProjectionHealth,
    compare_durable_projection_fingerprints,
)


FIXTURE = json.loads(corpus_path("durable-client", "envelope_v1.json").read_text())
FIXTURE = instrument(FIXTURE, name="durable-client/envelope_v1.json")


class Transport:
    def __init__(self) -> None:
        self.publications: list[tuple[str, bytes]] = []

    def publish(self, subject: str, payload: bytes) -> DurableBrokerPubAck:
        self.publications.append((subject, payload))
        return DurableBrokerPubAck("INGRESS", 7)

    def subscribe(self, subject: str) -> object:
        return {"subject": subject}


def projection(position: int, entries: list[str]) -> DurableProjectionEvent[list[str]]:
    return DurableProjectionEvent(
        1,
        "sample-owner",
        1,
        position,
        position,
        7,
        11,
        DurableProjectionCompleteness.COMPLETE_HISTORY,
        entries,
        f"source-{position}",
        f"projection-{position}",
        DurableProjectionHealth.HEALTHY,
    )


def test_replays_all_canonical_durable_client_vectors() -> None:
    transport = Transport()
    client: DurableClient[list[str]] = DurableClient(transport)

    for vector in FIXTURE["envelope_vectors"]:
        try:
            envelope = DurableIngressEnvelope.from_wire(vector["envelope"])
        except ValueError as error:
            envelope = None
            reason = str(error)
        else:
            reason = "accepted"
        expected = vector["expected"]
        assert_key(expected, "accepted", envelope is not None)
        assert_key(expected, "reason", reason)
        assert_key(expected, "payload_decoded", envelope is not None)
        if envelope is not None:
            assert client.publish_ingress("sample.ingress", envelope).sequence == 7
            assert (
                DurableIngressEnvelope.decode(transport.publications[-1][1]) == envelope
            )

    for vector in FIXTURE["ordering_vectors"]:
        order_client: DurableClient[list[str]] = DurableClient(Transport())
        for index, message_id in enumerate(vector["observed_message_ids"]):
            envelope = DurableIngressEnvelope(1, message_id, 7, 11, bytes([index]))
            order_client.observe_ingress(envelope)
        assert order_client.observed_message_ids() == vector["expected_delivery_order"]
        assert (
            require_flag(vector["owner_order_inferred"], where="owner_order_inferred")
            is False
        )

    for vector in FIXTURE["projection_ordering_vectors"]:
        projected: DurableClient[list[str]] = DurableClient(Transport())
        classifications: list[str] = []
        for position in vector["observed_source_positions"]:
            admission = projected.observe_projection(
                projection(position, [str(position)])
            )
            if admission.kind.value == "buffered":
                classifications.append("buffered")
            elif admission.kind.value == "dropped":
                classifications.append("duplicate")
            else:
                classifications.append("applied")
        assert classifications == vector["expected_delivery_classification"]
        assert (
            projected.applied_source_positions("sample-owner")
            == vector["expected_applied_positions"]
        )
        assert (
            require_flag(
                vector["broker_order_authoritative"], where="broker_order_authoritative"
            )
            is False
        )
        assert (
            require_flag(
                vector["may_authorize_transition"], where="may_authorize_transition"
            )
            is False
        )

    for vector in FIXTURE["dedup_vectors"]:
        dedup_client: DurableClient[list[str]] = DurableClient(Transport())
        actual = [
            dedup_client.observe_ingress(DurableIngressEnvelope.from_wire(item)).value
            for item in vector["deliveries"]
        ]
        assert actual == vector["expected_classification"]

    for vector in FIXTURE["receipt_vectors"]:
        receipt = DurableHostReceipt.from_wire(vector["receipt"])
        assert receipt.to_wire() == vector["expected_round_trip"]
        assert client.observe_host_receipt(receipt) == "recorded"
        assert client.observe_host_receipt(receipt) == "duplicate"
        assert (
            require_flag(
                vector["transport_ack_equivalent"], where="transport_ack_equivalent"
            )
            is False
        )

    for vector in FIXTURE["projection_fingerprint_vectors"]:
        comparison = compare_durable_projection_fingerprints(
            DurableProjectionFingerprint.from_wire(vector["left"]),
            DurableProjectionFingerprint.from_wire(vector["right"]),
        )
        actual = {
            "same_source": comparison.same_source,
            "same_fingerprint": comparison.same_fingerprint,
            "same_completeness": comparison.same_completeness,
            "equivalent": comparison.equivalent,
        }
        for key, value in actual.items():
            assert_key(vector["expected"], key, value)


def test_complete_history_projection_orders_deduplicates_and_rejects_conflict() -> None:
    client: DurableClient[list[str]] = DurableClient(Transport())
    assert client.observe_projection(projection(2, ["a", "b"])).kind.value == "buffered"
    assert client.projection("sample-owner") is None
    client.observe_projection(projection(1, ["a"]))
    assert client.projection("sample-owner") == projection(2, ["a", "b"])
    assert client.observe_projection(projection(2, ["a", "b"])).kind.value == "dropped"
    changed = projection(2, ["changed"])
    changed = DurableProjectionEvent(
        changed.protocol_version,
        changed.owner_id,
        changed.generation,
        changed.source_position,
        changed.projection_version,
        changed.schema_version,
        changed.codec_version,
        changed.completeness,
        changed.entries,
        changed.source_fingerprint,
        "changed-fingerprint",
        changed.health,
    )
    with pytest.raises(ValueError, match="conflicting durable projection"):
        client.observe_projection(changed)


def test_projection_is_advisory_and_binding_is_client_only() -> None:
    client: DurableClient[list[str]] = DurableClient(Transport())
    authoritative = projection(1, ["a"])
    authoritative = DurableProjectionEvent(
        authoritative.protocol_version,
        authoritative.owner_id,
        authoritative.generation,
        authoritative.source_position,
        authoritative.projection_version,
        authoritative.schema_version,
        authoritative.codec_version,
        authoritative.completeness,
        authoritative.entries,
        authoritative.source_fingerprint,
        authoritative.projection_fingerprint,
        authoritative.health,
        True,
    )
    with pytest.raises(ValueError, match="projection_is_advisory"):
        client.observe_projection(authoritative)
    assert DURABLE_CAPABILITIES == {
        "core": True,
        "client": True,
        "durable_host": False,
        "distributed_host": False,
        "accelerated_host": False,
    }
