"""Canonical replay and rejection matrix for the public consumer testkit."""

from __future__ import annotations

import copy
import dataclasses
import json
from dataclasses import dataclass, field

import pytest
from conformance_assert import assert_key, corpus_path, instrument, scenarios

from lazily import (
    SimConsumerAction,
    SimConsumerAdapter,
    SimConsumerAdapterKind,
    SimConsumerCallbacks,
    SimConsumerError,
    SimConsumerExternalPort,
    SimConsumerExternalSelection,
    SimConsumerGeneratedAction,
    SimConsumerPort,
    SimConsumerPortDeterminism,
    SimConsumerScenario,
    SimConsumerTestkit,
    SimConsumerTestkitSpec,
    SimConsumerTraceEntry,
    SimConsumerWorldEvidence,
)


FIXTURE_NAME = "simulation/consumer_testkit.json"


@dataclass
class State:
    value: int = 0
    probes: int = 0
    history: list[SimConsumerAction] = field(default_factory=list)
    world: SimConsumerWorldEvidence = field(
        default_factory=lambda: SimConsumerWorldEvidence("", 0, ())
    )


def load_fixture():
    return instrument(
        json.loads(corpus_path("simulation", "consumer_testkit.json").read_text()),
        name=FIXTURE_NAME,
    )


def generated_scenario(fixture) -> SimConsumerScenario:
    model = 0
    actions = []
    for action in fixture["actions"]:
        model += action["payload"]
        actions.append(
            SimConsumerGeneratedAction(
                SimConsumerAction(
                    action["id"],
                    action["actor_id"],
                    action["kind"],
                    action["version"],
                    action["payload"],
                ),
                model,
            )
        )
    return SimConsumerScenario(
        bytes.fromhex(fixture["seed"]),
        fixture["generator"]["path"],
        fixture["generator"]["version"],
        0,
        tuple(actions),
    )


def fixture_ports(fixture, stub_clock: bool) -> tuple[SimConsumerPort, ...]:
    return tuple(
        SimConsumerPort(
            item["id"],
            item["kind"],
            SimConsumerPortDeterminism(item["determinism"]),
            stub_clock and item["id"] == "logical.clock",
        )
        for item in fixture["ports"]
    )


def fixture_adapter(fixture, block) -> tuple[SimConsumerAdapter, State]:
    kind = SimConsumerAdapterKind(block["kind"])
    execution = block["execution_mode"]
    history_mode = block["history_mode"]
    if execution not in {"sim_world", "real", "bypass"}:
        raise AssertionError(f"unknown execution mode {execution!r}")
    if history_mode not in {"none", "exact", "empty"}:
        raise AssertionError(f"unknown history mode {history_mode!r}")
    if block["clock_stub"] not in {"stubbed", "none"}:
        raise AssertionError(f"unknown clock stub {block['clock_stub']!r}")
    state = State()

    def probe() -> None:
        state.probes += 1

    def reset() -> None:
        probes = state.probes
        state.value = 0
        state.probes = probes
        state.history.clear()
        state.world = SimConsumerWorldEvidence(f"{block['id']}.world", 0, ())

    def apply(action: SimConsumerAction) -> None:
        state.value += action.payload + block["delta_bias"]
        if kind is not SimConsumerAdapterKind.IN_MEMORY and history_mode == "exact":
            state.history.append(copy.deepcopy(action))
        if kind is SimConsumerAdapterKind.IN_MEMORY and execution == "sim_world":
            state.world = SimConsumerWorldEvidence(
                state.world.world_id,
                state.world.steps + 1,
                (
                    *state.world.trace,
                    SimConsumerTraceEntry(action.id, "action_execute"),
                ),
            )

    callbacks = SimConsumerCallbacks(
        reset=reset,
        apply=apply,
        observe=lambda: {"consumer.value": state.value},
        world_evidence=(lambda: state.world)
        if kind is SimConsumerAdapterKind.IN_MEMORY
        else None,
        probe=probe if kind.real else None,
        materialized_history=(lambda: tuple(copy.deepcopy(state.history)))
        if kind.real
        else None,
    )
    external_port = (
        SimConsumerExternalPort(block["external_port"])
        if "external_port" in block
        else None
    )
    return SimConsumerAdapter(
        block["id"],
        kind,
        block["service_id"],
        block["reducer_id"],
        block["production_reducer_id"],
        block["protocol_id"],
        fixture_ports(fixture, block["clock_stub"] == "stubbed"),
        callbacks,
        external_port,
    ), state


def test_consumer_testkit_conformance() -> None:
    fixture = load_fixture()
    assert fixture["kind"] == "ConsumerSimulationTestkit"
    materialized = generated_scenario(fixture)
    for scenario in scenarios(fixture):
        states: dict[str, State] = {}
        adapters = []
        for block in scenario["adapters"]:
            adapter, state = fixture_adapter(fixture, block)
            adapters.append(adapter)
            states[adapter.id] = state
        kit = SimConsumerTestkit(
            SimConsumerTestkitSpec(
                scenario["simulation_adapter_id"],
                tuple(
                    SimConsumerAdapterKind(value)
                    for value in scenario["required_real_adapters"]
                ),
                tuple(
                    SimConsumerExternalSelection(
                        value["adapter_id"], SimConsumerExternalPort(value["port"])
                    )
                    for value in scenario["required_external_processes"]
                ),
                tuple(adapters),
            )
        )
        expected = scenario["expected"]
        outcome = expected["outcome"]
        if outcome == "success":
            result = kit.run(materialized)
            assert_key(expected, "outcome", "success")
            assert_key(expected, "adapter_ids", list(result.adapter_ids))
            assert_key(
                expected, "checkpoint_steps", [item.step for item in result.checkpoints]
            )
            assert_key(expected, "checkpoint_values", [1, 3, 6])
            assert result.scenario_digest
            if scenario["id"] == "selected_real_adapters_match_every_checkpoint":
                assert_key(
                    expected,
                    "checkpoint_action_ids",
                    [item.action_id for item in result.checkpoints],
                )
                assert_key(
                    expected, "observation_relation", "all_equal_at_every_checkpoint"
                )
                assert_key(
                    expected,
                    "materialized_history_relation",
                    "exact_prefix_at_every_checkpoint",
                )
                assert_key(expected, "probe_relation", "every_real_adapter_once")
            elif (
                scenario["id"]
                == "selected_external_process_preserves_independent_reducer_identity"
            ):
                external_id = expected["external_adapter_id"]
                evidence = next(
                    item
                    for item in result.adapter_evidence
                    if item.adapter_id == external_id
                )
                assert_key(expected, "external_adapter_id", evidence.adapter_id)
                assert_key(expected, "external_port", evidence.external_port.value)
                assert_key(expected, "external_protocol_id", evidence.protocol_id)
                assert_key(expected, "external_reducer_id", evidence.reducer_id)
                assert_key(
                    expected,
                    "external_production_reducer_id",
                    evidence.production_reducer_id,
                )
            else:
                raise AssertionError(f"unknown successful scenario {scenario['id']!r}")
            assert all(state.value == 6 for state in states.values())
        elif outcome in {
            "observation_divergence",
            "materialized_history_mismatch",
            "simulation_world_bypass",
        }:
            with pytest.raises(SimConsumerError) as raised:
                kit.run(materialized)
            error = raised.value
            assert_key(expected, "outcome", error.kind)
            assert_key(expected, "step", error.step)
            assert_key(expected, "action_id", error.action_id)
            assert_key(expected, "adapter_id", error.adapter_id)
            if outcome == "observation_divergence":
                assert_key(expected, "observation_id", error.observation_id)
            elif outcome == "materialized_history_mismatch":
                assert_key(expected, "expected_prefix_length", error.step)
                assert_key(
                    expected,
                    "actual_prefix_length",
                    len(states[error.adapter_id].history),
                )
        else:
            raise AssertionError(f"unknown expected outcome {outcome!r}")


def valid_pair() -> SimConsumerTestkitSpec:
    fixture = load_fixture()
    blocks = [
        block
        for block in fixture["scenarios"][0]["adapters"]
        if block["kind"] in {"in_memory", "postgres"}
    ]
    adapters = tuple(fixture_adapter(fixture, block)[0] for block in blocks)
    return SimConsumerTestkitSpec(
        "memory", (SimConsumerAdapterKind.POSTGRES,), (), adapters
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda spec: setattr(spec, "required_real_adapters", ()),
        lambda spec: setattr(spec, "simulation_adapter_id", "missing"),
        lambda spec: setattr(spec.adapters[1], "id", spec.adapters[0].id),
        lambda spec: setattr(spec.adapters[1], "protocol_id", "other.protocol"),
        lambda spec: setattr(spec.adapters[1], "ports", spec.adapters[1].ports[:-1]),
        lambda spec: setattr(spec.adapters[1].ports[1], "stubbed", True),
        lambda spec: setattr(spec.adapters[1].callbacks, "probe", None),
        lambda spec: setattr(spec.adapters[1], "reducer_id", "other.reducer"),
    ],
)
def test_constructor_rejection_matrix_is_callback_free(mutate) -> None:
    spec = valid_pair()
    # frozen ports need replacement for the real-stub case.
    try:
        mutate(spec)
    except dataclasses.FrozenInstanceError:
        port = spec.adapters[1].ports[1]
        spec.adapters[1].ports = (
            *spec.adapters[1].ports[:1],
            SimConsumerPort(port.id, port.kind, port.determinism, True),
            *spec.adapters[1].ports[2:],
        )
    with pytest.raises(SimConsumerError):
        SimConsumerTestkit(spec)
