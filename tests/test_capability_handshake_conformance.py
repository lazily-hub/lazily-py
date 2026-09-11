"""Canonical capability-handshake negotiation (``#lzhandshakedeadfields``)."""

from __future__ import annotations

import json
from pathlib import Path

from conformance_assert import (
    TrackedBlock,
    assert_key,
    corpus_fixture,
    instrument,
    scenarios,
)

from lazily.ipc import CapabilityHandshake


_LOCAL_FIXTURES = Path(__file__).resolve().parent / "conformance"
_FIXTURE = "codec/capability_handshake.json"


def _load() -> dict:
    path = corpus_fixture(_FIXTURE, _LOCAL_FIXTURES / _FIXTURE)
    fixture = instrument(json.loads(path.read_text()), name=_FIXTURE)
    assert fixture["protocol_version"] == 1
    assert fixture["kind"] == "CapabilityHandshake"
    return fixture


def test_capability_handshake_conformance() -> None:
    fixture = _load()
    replayed = 0

    for scenario in scenarios(fixture):
        local = CapabilityHandshake.from_wire(scenario["local"])
        remote = CapabilityHandshake.from_wire(scenario["remote"])
        result = local.negotiate_with(remote)
        expected: TrackedBlock = scenario["expected"]

        assert_key(expected, "compatible", result.compatible)
        if result.compatible:
            assert result.max_frame_size is not None
            assert result.fragmentation_supported is not None
            assert_key(expected, "negotiated_max_frame_size", result.max_frame_size)
            assert_key(
                expected,
                "negotiated_fragmentation_supported",
                result.fragmentation_supported,
            )
        else:
            assert result.field is not None
            assert_key(expected, "field", result.field)

        # Booked at the END of the body, so a scenario the loop steps over —
        # a future `continue`, an early `break` — is missing from the count.
        replayed += 1

    # No hard-coded corpus count here (#lzcorpusfloorguard): a literal that
    # must be re-pinned by hand every time the corpus moves is the thing that
    # drifted. A SHRINKING corpus is caught corpus-side by lazily-spec's
    # `conformance/corpus-counts.json` + `scripts/check-corpus-floors.mjs`.
    # What this runner owes is exactness: every scenario LOADED was EXECUTED.
    assert replayed == len(fixture["scenarios"]), (
        f"loaded {len(fixture['scenarios'])} scenarios but negotiated "
        f"{replayed} — a scenario was skipped rather than replayed"
    )
