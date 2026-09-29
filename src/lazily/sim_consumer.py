"""Consumer simulation conformance testkit.

The public callback surface keeps service ownership in the integration while
letting construction reject incomplete topologies before any callback runs.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from .replay import canonical_bytes, canonical_digest


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

__all__ = [
    "SimConsumerAction",
    "SimConsumerAdapter",
    "SimConsumerAdapterEvidence",
    "SimConsumerAdapterKind",
    "SimConsumerCallbacks",
    "SimConsumerCheckpoint",
    "SimConsumerError",
    "SimConsumerExternalPort",
    "SimConsumerExternalSelection",
    "SimConsumerGeneratedAction",
    "SimConsumerPort",
    "SimConsumerPortDeterminism",
    "SimConsumerRunResult",
    "SimConsumerScenario",
    "SimConsumerTestkit",
    "SimConsumerTestkitSpec",
    "SimConsumerTraceEntry",
    "SimConsumerWorldEvidence",
]


class SimConsumerAdapterKind(str, Enum):
    IN_MEMORY = "in_memory"
    POSTGRES = "postgres"
    NATS = "nats"
    EXTERNAL_PROCESS = "external_process"

    @property
    def real(self) -> bool:
        return self is not SimConsumerAdapterKind.IN_MEMORY


class SimConsumerExternalPort(str, Enum):
    CLI = "cli"
    FILESYSTEM = "filesystem"
    LOCAL_SOCKET = "local_socket"
    EDITOR_REPLICA = "editor_replica"


class SimConsumerPortDeterminism(str, Enum):
    DETERMINISTIC = "deterministic"
    NONDETERMINISTIC = "nondeterministic"


@dataclass(frozen=True)
class SimConsumerPort:
    id: str
    kind: str
    determinism: SimConsumerPortDeterminism
    stubbed: bool = False


@dataclass(frozen=True)
class SimConsumerAction:
    id: str
    actor_id: str
    kind: str
    version: str
    payload: Any


@dataclass(frozen=True)
class SimConsumerGeneratedAction:
    action: SimConsumerAction
    model_after: Any


@dataclass(frozen=True)
class SimConsumerScenario:
    seed: bytes
    generator_path: str
    generator_version: str
    initial_model: Any
    actions: tuple[SimConsumerGeneratedAction, ...]


@dataclass(frozen=True)
class SimConsumerTraceEntry:
    action_id: str
    kind: str


@dataclass(frozen=True)
class SimConsumerWorldEvidence:
    world_id: str
    steps: int
    trace: tuple[SimConsumerTraceEntry, ...]


@dataclass
class SimConsumerCallbacks:
    probe: Callable[[], None] | None = None
    reset: Callable[[], None] | None = None
    apply: Callable[[SimConsumerAction], None] | None = None
    observe: Callable[[], Mapping[str, Any]] | None = None
    materialized_history: Callable[[], tuple[SimConsumerAction, ...]] | None = None
    world_evidence: Callable[[], SimConsumerWorldEvidence] | None = None


@dataclass
class SimConsumerAdapter:
    id: str
    kind: SimConsumerAdapterKind
    service_id: str
    reducer_id: str
    production_reducer_id: str
    protocol_id: str
    ports: tuple[SimConsumerPort, ...]
    callbacks: SimConsumerCallbacks
    external_port: SimConsumerExternalPort | None = None


@dataclass(frozen=True)
class SimConsumerExternalSelection:
    adapter_id: str
    port: SimConsumerExternalPort


@dataclass
class SimConsumerTestkitSpec:
    simulation_adapter_id: str
    required_real_adapters: tuple[SimConsumerAdapterKind, ...]
    required_external_processes: tuple[SimConsumerExternalSelection, ...]
    adapters: tuple[SimConsumerAdapter, ...]


@dataclass(frozen=True)
class SimConsumerAdapterEvidence:
    adapter_id: str
    kind: SimConsumerAdapterKind
    service_id: str
    reducer_id: str
    production_reducer_id: str
    protocol_id: str
    external_port: SimConsumerExternalPort | None


@dataclass(frozen=True)
class SimConsumerCheckpoint:
    step: int
    action_id: str
    observation_digests: Mapping[str, str]


@dataclass(frozen=True)
class SimConsumerRunResult:
    scenario_digest: str
    adapter_ids: tuple[str, ...]
    adapter_evidence: tuple[SimConsumerAdapterEvidence, ...]
    checkpoints: tuple[SimConsumerCheckpoint, ...]


class SimConsumerError(RuntimeError):
    """Fail-closed configuration, execution, or divergence error."""

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        step: int | None = None,
        action_id: str | None = None,
        adapter_id: str | None = None,
        observation_id: str | None = None,
    ) -> None:
        super().__init__(f"consumer simulation conformance failed: {message}")
        self.kind = kind
        self.step = step
        self.action_id = action_id
        self.adapter_id = adapter_id
        self.observation_id = observation_id


def _invalid(message: str) -> SimConsumerError:
    return SimConsumerError("invalid_configuration", message)


def _stable_id(value: str) -> bool:
    return bool(value) and all(char.isalnum() or char in "._:-/" for char in value)


def _port_contract(
    adapter: SimConsumerAdapter,
) -> frozenset[tuple[str, str, SimConsumerPortDeterminism]]:
    return frozenset((port.id, port.kind, port.determinism) for port in adapter.ports)


def _validate_adapter(adapter: SimConsumerAdapter) -> None:
    if not all(
        _stable_id(value)
        for value in (adapter.id, adapter.protocol_id, adapter.reducer_id)
    ):
        raise _invalid("adapter needs stable adapter, protocol, and reducer ids")
    if (
        adapter.callbacks.reset is None
        or adapter.callbacks.apply is None
        or adapter.callbacks.observe is None
    ):
        raise _invalid(
            f"adapter {adapter.id!r} needs reset, apply, and observe callbacks"
        )
    ids: set[str] = set()
    for port in adapter.ports:
        if not _stable_id(port.id) or not _stable_id(port.kind) or port.id in ids:
            raise _invalid("ports need unique stable ids and kinds")
        ids.add(port.id)
        if (
            port.stubbed
            and port.determinism is SimConsumerPortDeterminism.DETERMINISTIC
        ):
            raise _invalid("deterministic port cannot be stubbed")
        if adapter.kind.real and port.stubbed:
            raise _invalid("real adapter cannot stub a port")
    callbacks = adapter.callbacks
    if adapter.kind is SimConsumerAdapterKind.IN_MEMORY:
        if (
            not _stable_id(adapter.production_reducer_id)
            or adapter.service_id
            or adapter.external_port is not None
            or callbacks.probe is not None
            or callbacks.materialized_history is not None
            or callbacks.world_evidence is None
        ):
            raise _invalid("invalid in-memory adapter evidence or callbacks")
    else:
        if (
            not _stable_id(adapter.service_id)
            or callbacks.probe is None
            or callbacks.materialized_history is None
        ):
            raise _invalid(
                "real adapter needs service id, probe, and materialized history"
            )
        if callbacks.world_evidence is not None:
            raise _invalid("real adapter cannot expose simulation-world evidence")
        if adapter.kind is SimConsumerAdapterKind.EXTERNAL_PROCESS:
            if adapter.production_reducer_id or adapter.external_port is None:
                raise _invalid(
                    "external adapter must have its own reducer and selected port"
                )
        elif (
            not _stable_id(adapter.production_reducer_id)
            or adapter.reducer_id != adapter.production_reducer_id
            or adapter.external_port is not None
        ):
            raise _invalid("invalid built-in real reducer evidence")


class SimConsumerTestkit:
    """Run one generated history through simulation and selected real adapters."""

    def __init__(self, spec: SimConsumerTestkitSpec) -> None:
        if not _stable_id(spec.simulation_adapter_id):
            raise _invalid("simulation adapter id is not stable")
        if not spec.required_real_adapters and not spec.required_external_processes:
            raise _invalid("at least one real adapter must be selected")
        ids: set[str] = set()
        for adapter in spec.adapters:
            _validate_adapter(adapter)
            if adapter.id in ids:
                raise _invalid(f"duplicate adapter {adapter.id!r}")
            ids.add(adapter.id)
        by_id = {adapter.id: adapter for adapter in spec.adapters}
        baseline = by_id.get(spec.simulation_adapter_id)
        if baseline is None or baseline.kind is not SimConsumerAdapterKind.IN_MEMORY:
            raise _invalid("simulation adapter must resolve to an in_memory adapter")
        selected = {baseline.id}
        for kind in spec.required_real_adapters:
            if kind not in (
                SimConsumerAdapterKind.POSTGRES,
                SimConsumerAdapterKind.NATS,
            ):
                raise _invalid("required real kind must be postgres or nats")
            matches = [adapter for adapter in spec.adapters if adapter.kind is kind]
            if len(matches) != 1:
                raise _invalid(
                    f"selection {kind.value} resolves to {len(matches)} adapters"
                )
            selected.add(matches[0].id)
        external_ids: set[str] = set()
        for selection in spec.required_external_processes:
            if selection.adapter_id in external_ids:
                raise _invalid("duplicate external selection")
            external_ids.add(selection.adapter_id)
            selected_adapter = by_id.get(selection.adapter_id)
            if (
                selected_adapter is None
                or selected_adapter.kind is not SimConsumerAdapterKind.EXTERNAL_PROCESS
                or selected_adapter.external_port is not selection.port
            ):
                raise _invalid("external selection has wrong kind or port")
            selected.add(selection.adapter_id)
        if selected != ids:
            raise _invalid("configured adapter was not explicitly selected")
        for adapter in spec.adapters:
            if adapter.protocol_id != baseline.protocol_id:
                raise _invalid("adapters do not share a protocol id")
            if (
                adapter.kind is not SimConsumerAdapterKind.EXTERNAL_PROCESS
                and adapter.production_reducer_id != baseline.production_reducer_id
            ):
                raise _invalid("built-in adapters do not share a production reducer")
            if _port_contract(adapter) != _port_contract(baseline):
                raise _invalid("adapters do not expose the same narrow-port contract")
        self._adapters = tuple(sorted(spec.adapters, key=lambda adapter: adapter.id))
        self._baseline = next(
            index
            for index, adapter in enumerate(self._adapters)
            if adapter.id == baseline.id
        )

    def run(self, scenario: SimConsumerScenario) -> SimConsumerRunResult:
        _validate_scenario(scenario)
        scenario_digest = canonical_digest(scenario)
        for adapter in self._adapters:
            callbacks = adapter.callbacks
            if adapter.kind.real:
                assert callbacks.probe is not None
                callbacks.probe()
            assert callbacks.reset is not None
            callbacks.reset()
            if adapter.kind is SimConsumerAdapterKind.IN_MEMORY:
                assert callbacks.world_evidence is not None
                callbacks.world_evidence()
            else:
                assert callbacks.materialized_history is not None
                if callbacks.materialized_history():
                    raise _invalid(
                        f"adapter {adapter.id!r} history is not initially empty"
                    )
        checkpoints: list[SimConsumerCheckpoint] = []
        for index, generated in enumerate(scenario.actions):
            step = index + 1
            observed: list[dict[str, Any]] = []
            digests: dict[str, str] = {}
            for adapter in self._adapters:
                callbacks = adapter.callbacks
                before = (
                    callbacks.world_evidence()
                    if callbacks.world_evidence is not None
                    else None
                )
                assert callbacks.apply is not None
                callbacks.apply(copy.deepcopy(generated.action))
                if before is not None:
                    assert callbacks.world_evidence is not None
                    after = callbacks.world_evidence()
                    executed = any(
                        entry.action_id == generated.action.id
                        and entry.kind.startswith("action_")
                        for entry in after.trace[len(before.trace) :]
                    )
                    if (
                        after.world_id != before.world_id
                        or after.steps <= before.steps
                        or not executed
                    ):
                        raise SimConsumerError(
                            "simulation_world_bypass",
                            "in-memory adapter bypassed its simulation world",
                            step=step,
                            action_id=generated.action.id,
                            adapter_id=adapter.id,
                        )
                else:
                    assert callbacks.materialized_history is not None
                    actual = callbacks.materialized_history()
                    expected = tuple(item.action for item in scenario.actions[:step])
                    if len(actual) != len(expected) or any(
                        canonical_bytes(a) != canonical_bytes(b)
                        for a, b in zip(actual, expected, strict=True)
                    ):
                        raise SimConsumerError(
                            "materialized_history_mismatch",
                            f"history length {len(actual)} does not match exact prefix {len(expected)}",
                            step=step,
                            action_id=generated.action.id,
                            adapter_id=adapter.id,
                        )
                assert callbacks.observe is not None
                values = copy.deepcopy(dict(callbacks.observe()))
                if not values:
                    raise SimConsumerError(
                        "empty_observation",
                        "adapter returned no observations",
                        step=step,
                        action_id=generated.action.id,
                        adapter_id=adapter.id,
                    )
                canonical_bytes(values)
                observed.append(values)
                digests[adapter.id] = canonical_digest(values)
            baseline = observed[self._baseline]
            for adapter_index, adapter in enumerate(self._adapters):
                if adapter_index == self._baseline:
                    continue
                other = observed[adapter_index]
                for observation_id in sorted(baseline.keys() | other.keys()):
                    if (
                        observation_id not in baseline
                        or observation_id not in other
                        or canonical_bytes(baseline[observation_id])
                        != canonical_bytes(other[observation_id])
                    ):
                        raise SimConsumerError(
                            "observation_divergence",
                            "canonical observations differ",
                            step=step,
                            action_id=generated.action.id,
                            adapter_id=adapter.id,
                            observation_id=observation_id,
                        )
            checkpoints.append(
                SimConsumerCheckpoint(step, generated.action.id, digests)
            )
        evidence = tuple(
            SimConsumerAdapterEvidence(
                adapter.id,
                adapter.kind,
                adapter.service_id,
                adapter.reducer_id,
                adapter.production_reducer_id,
                adapter.protocol_id,
                adapter.external_port,
            )
            for adapter in self._adapters
        )
        return SimConsumerRunResult(
            scenario_digest,
            tuple(adapter.id for adapter in self._adapters),
            evidence,
            tuple(checkpoints),
        )


def _validate_scenario(scenario: SimConsumerScenario) -> None:
    if (
        len(scenario.seed) != 32
        or not _stable_id(scenario.generator_path)
        or not _stable_id(scenario.generator_version)
    ):
        raise _invalid("scenario needs a 32-byte seed and stable generator identity")
    ids: set[str] = set()
    for generated in scenario.actions:
        action = generated.action
        if (
            not all(
                _stable_id(value)
                for value in (action.id, action.actor_id, action.kind, action.version)
            )
            or action.id in ids
        ):
            raise _invalid("scenario action identities must be unique and stable")
        ids.add(action.id)
