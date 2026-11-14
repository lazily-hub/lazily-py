"""Canonical generic effect-CAS corpus replay (``#lzeffectcas``).

``egress/effect_cas.json`` is replayed through the pure :class:`EffectCasCore`
and through each reactive shell (``EffectCas`` / ``ThreadSafeEffectCas`` /
``AsyncEffectCas``), all via the public API.

What keeps this from going green while testing nothing:

* The fixture must exist (a failure, never a skip), and every scenario is
  iterated through ``scenarios()``, which books it as replayed only once its
  payload is read; the session's scenario ledger fails on a scenario on disk that
  no run replayed.
* Each flavor counts the steps it EXECUTED and asserts that equals the steps the
  corpus DECLARES; ``_run_step`` raises on an unknown op instead of skipping it.
* Every step's ``returns`` and every scenario's ``final`` are compared whole, so
  a field the corpus adds fails the comparison.
* Invariant 2 of the spec ("a stale generation, unknown attempt, or
  non-matching receipt changes no state and no reader version") is asserted per
  step: the transition's reported change — and, for the shells, the snapshot
  reader's cache validity — must move exactly when the outcome is a
  state-changing one.
* ``superseded`` steps drive a compare-and-apply backend at ``backend_before``
  with the saved attempt, so the "zero backend mutation" claim is checked against
  ``backend_after`` rather than read off the fixture.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest
from conformance_assert import corpus_subdir, instrument, scenarios

import lazily


_NAME = "egress/effect_cas.json"
_FIXTURE = corpus_subdir("egress") / "effect_cas.json"

#: Outcomes that change observable state. Every other outcome must change none.
_STATE_CHANGING = frozenset(
    {
        ("upsert", "accepted"),
        ("claim", "claimed"),
        ("applied", "step_applied"),
        ("applied", "completed"),
        ("applied", "superseded"),
        ("failure", "superseded"),
        ("failure", "retrying"),
        ("failure", "rejected"),
        ("reconnect", "advanced"),
    }
)


def _load() -> dict[str, Any]:
    assert _FIXTURE.is_file(), f"canonical effect-CAS fixture missing at {_FIXTURE}"
    return instrument(json.loads(_FIXTURE.read_text()), name=_NAME)


# ---------------------------------------------------------------------------
# Flavor adapters: each returns (outcome, invalidated)
# ---------------------------------------------------------------------------


class _CoreFlavor:
    name = "core"

    def __init__(self, generation: int) -> None:
        self.core: lazily.EffectCasCore[str, int, str, str, str] = lazily.EffectCasCore(
            generation
        )

    def call(self, method: str, *args: Any) -> tuple[Any, bool]:
        change, outcome = getattr(self.core, method)(*args)
        return outcome, change.state

    def snapshot(self) -> lazily.EffectCasSnapshot:
        return self.core.snapshot()


class _ShellFlavor:
    name = "shell"
    cls: type[Any] = lazily.EffectCas

    def __init__(self, generation: int) -> None:
        self.shell = self.cls({}, generation)
        self.shell.snapshot()

    def call(self, method: str, *args: Any) -> tuple[Any, bool]:
        assert self.shell.state_is_valid(), "the snapshot reader was not warm"
        outcome = getattr(self.shell, method)(*args)
        invalidated = not self.shell.state_is_valid()
        self.shell.snapshot()
        return outcome, invalidated

    def snapshot(self) -> lazily.EffectCasSnapshot:
        return self.shell.snapshot()


class _ThreadSafeFlavor(_ShellFlavor):
    name = "thread-safe"
    cls = lazily.ThreadSafeEffectCas


class _AsyncFlavor(_ShellFlavor):
    name = "async"
    cls = lazily.AsyncEffectCas


FLAVORS = (_CoreFlavor, _ShellFlavor, _ThreadSafeFlavor, _AsyncFlavor)


# ---------------------------------------------------------------------------
# Outcome projections onto the fixture's `returns` shape
# ---------------------------------------------------------------------------


def _upsert_value(o: lazily.EffectCasUpsert) -> dict[str, Any]:
    if o.kind is lazily.EffectCasUpsertKind.ALREADY_APPLIED:
        return {"upsert": "already_applied", "applied_through": o.applied_through}
    if o.kind is lazily.EffectCasUpsertKind.STALE_EPOCH:
        return {"upsert": "stale_epoch", "current": o.current}
    return {"upsert": str(o.kind)}


def _claim_value(o: lazily.EffectCasClaim) -> dict[str, Any]:
    if o.kind is lazily.EffectCasClaimKind.CLAIMED:
        attempt = o.attempt
        assert attempt is not None
        return {
            "claim": "claimed",
            "identity": attempt.identity.effect.as_str(),
            "step": attempt.identity.step,
            "attempt": attempt.identity.attempt,
            "expected": attempt.expected,
            "payload": attempt.payload,
        }
    if o.kind is lazily.EffectCasClaimKind.WAITING:
        return {"claim": "waiting", "retry_at": o.retry_at}
    if o.kind is lazily.EffectCasClaimKind.STALE_GENERATION:
        return {"claim": "stale_generation", "current": o.current}
    return {"claim": str(o.kind)}


def _applied_value(o: lazily.EffectCasApplied) -> dict[str, Any]:
    if o.kind is lazily.EffectCasAppliedKind.STEP_APPLIED:
        return {"applied": "step_applied", "next_step": o.next_step}
    if o.kind is lazily.EffectCasAppliedKind.COMPLETED:
        return {"applied": "completed", "applied_through": o.applied_through}
    if o.kind is lazily.EffectCasAppliedKind.STALE_GENERATION:
        return {"applied": "stale_generation", "current": o.current}
    return {"applied": str(o.kind)}


def _failure_value(o: lazily.EffectCasFailure) -> dict[str, Any]:
    if o.kind is lazily.EffectCasFailureKind.RETRYING:
        return {"failure": "retrying", "retry_at": o.retry_at}
    if o.kind is lazily.EffectCasFailureKind.STALE_GENERATION:
        return {"failure": "stale_generation", "current": o.current}
    return {"failure": str(o.kind)}


def _reconnect_value(o: lazily.EffectCasReconnect) -> dict[str, Any]:
    if o.kind is lazily.EffectCasReconnectKind.ADVANCED:
        return {
            "reconnect": "advanced",
            "generation": o.generation,
            "requeued": o.requeued,
            "superseded": o.superseded,
        }
    if o.kind is lazily.EffectCasReconnectKind.UNCHANGED:
        return {"reconnect": "unchanged", "generation": o.generation}
    return {"reconnect": "stale_generation", "current": o.current}


class _Backend:
    """A compare-and-apply backend holding one opaque authority token."""

    def __init__(self, authority: int) -> None:
        self.authority = authority
        self.mutations = 0

    def observe(self, _key: str) -> int:
        return self.authority

    def compare_and_apply(
        self, attempt: lazily.EffectAttempt[str, int, str, str]
    ) -> lazily.EffectApply:
        if attempt.expected != self.authority:
            return lazily.EffectApply.superseded(self.authority)
        self.authority += 1
        self.mutations += 1
        return lazily.EffectApply.applied(f"r{self.authority}", self.authority)


def _run_step(
    flavor: Any,
    attempts: dict[str, lazily.EffectAttempt[str, int, str, str]],
    step: Any,
    where: str,
) -> tuple[dict[str, Any], bool]:
    op = step["op"]
    kind = op["type"]

    def attempt() -> lazily.EffectAttempt[str, int, str, str]:
        label = op["attempt"]
        assert label in attempts, f"{where}: attempt label {label!r} was never saved"
        return attempts[label]

    if kind == "upsert":
        intent = lazily.EffectIntent(
            identity=lazily.EffectIdentity(op["identity"]),
            epoch=op["epoch"],
            key=op["key"],
            expected=op["expected"],
            plan=lazily.EffectPlan.of(op["plan"]),
        )
        outcome, changed = flavor.call("upsert_desired", intent)
        returned = _upsert_value(outcome)
    elif kind == "claim":
        outcome, changed = flavor.call("claim", op["key"], op["generation"], op["now"])
        returned = _claim_value(outcome)
        if "save" in step:
            assert outcome.attempt is not None, f"{where}: a saved claim must succeed"
            attempts[step["save"]] = outcome.attempt
    elif kind == "applied":
        outcome, changed = flavor.call(
            "record_applied",
            op["key"],
            attempt().identity,
            op["receipt"],
            op["authority"],
        )
        returned = _applied_value(outcome)
    elif kind == "superseded":
        backend = _Backend(op["backend_before"])
        verdict = backend.compare_and_apply(attempt())
        assert verdict.kind is lazily.EffectApplyKind.SUPERSEDED, (
            f"{where}: the attempt's expected authority matched the backend"
        )
        assert verdict.observed == op["observed"], f"{where}: observed authority"
        assert backend.authority == op["backend_after"], f"{where}: backend mutated"
        assert backend.mutations == 0, f"{where}: a mismatch mutated the backend"
        outcome, changed = flavor.call(
            "record_outcome", op["key"], attempt().identity, verdict, 0
        )
        returned = _failure_value(outcome)
    elif kind == "retryable":
        outcome, changed = flavor.call(
            "record_retryable",
            op["key"],
            attempt().identity,
            op["error"],
            op["retry_at"],
        )
        returned = _failure_value(outcome)
    elif kind == "rejected":
        outcome, changed = flavor.call(
            "record_rejected", op["key"], attempt().identity, op["error"]
        )
        returned = _failure_value(outcome)
    elif kind == "reconnect":
        outcome, changed = flavor.call("reconnect", op["generation"])
        returned = _reconnect_value(outcome)
    else:
        raise AssertionError(f"{where}: unknown effect-CAS op {kind!r}")
    return returned, changed


def _final_value(snapshot: lazily.EffectCasSnapshot) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, state in snapshot.keys.items():
        terminal = state.terminal
        out[key] = {
            "pending_identity": None
            if state.pending is None
            else state.pending.intent.identity.as_str(),
            "inflight_identity": None
            if state.inflight is None
            else state.inflight.identity.effect.as_str(),
            "applied_through": state.applied_through,
            "terminal": None if terminal is None else str(terminal.kind),
            "receipt_count": 0 if terminal is None else len(terminal.receipts),
            "retry_at": None if state.retry is None else state.retry.retry_at,
        }
    return out


def _replay(flavor_cls: Any) -> tuple[int, int, int, int]:
    """Returns (scenarios replayed, scenarios declared, steps executed, steps
    declared)."""
    fixture = _load()
    assert fixture["kind"] == "EffectCas"
    assert fixture["model"] == "EffectCasCore"
    replayed = 0
    executed = 0
    declared_steps = 0
    for scenario in scenarios(fixture):
        flavor = flavor_cls(scenario["generation"])
        steps = scenario["steps"]
        assert steps, f"{scenario['id']}: a scenario with no steps is vacuous"
        declared_steps += len(steps)
        attempts: dict[str, lazily.EffectAttempt[str, int, str, str]] = {}
        for index, step in enumerate(steps):
            where = (
                f"{flavor.name} {scenario['id']} step {index} ({step['op']['type']})"
            )
            returned, changed = _run_step(flavor, attempts, step, where)
            assert returned == step["returns"], (
                f"{where}: returns {returned!r}, expected {step['returns']!r}"
            )
            verb, verdict = next(iter(returned.items()))
            assert changed is ((verb, verdict) in _STATE_CHANGING), (
                f"{where}: outcome {verb}={verdict} reported state change {changed}"
            )
            executed += 1
        assert _final_value(flavor.snapshot()) == scenario["final"], (
            f"{flavor.name} {scenario['id']}: final state"
        )
        replayed += 1
    return replayed, len(fixture["scenarios"]), executed, declared_steps


@pytest.mark.parametrize("flavor_cls", FLAVORS, ids=lambda f: f.name)
def test_effect_cas_corpus_replays_exactly(flavor_cls: Any) -> None:
    replayed, declared_scenarios, executed, declared_steps = _replay(flavor_cls)
    assert replayed == declared_scenarios > 0, (
        f"replayed {replayed} of {declared_scenarios} scenarios"
    )
    assert executed == declared_steps > 0, (
        f"executed {executed} of {declared_steps} steps"
    )


# ---------------------------------------------------------------------------
# Library properties beyond the corpus
# ---------------------------------------------------------------------------

Core = lazily.EffectCasCore


def _intent(
    identity: str, epoch: int, key: str, expected: int, steps: list[str]
) -> lazily.EffectIntent[str, int, str]:
    return lazily.EffectIntent(
        identity=lazily.EffectIdentity(identity),
        epoch=epoch,
        key=key,
        expected=expected,
        plan=lazily.EffectPlan.of(steps),
    )


def _claimed(core: Any, key: str, generation: int, now: int) -> Any:
    _, claim = core.claim(key, generation, now)
    assert claim.kind is lazily.EffectCasClaimKind.CLAIMED, claim
    return claim.attempt


def test_identity_and_plan_reject_empty_values() -> None:
    with pytest.raises(ValueError):
        lazily.EffectIdentity("")
    with pytest.raises(lazily.EffectPlanError):
        lazily.EffectPlan.of([])


def test_retry_waits_on_the_logical_clock_and_newer_desire_wins() -> None:
    core: Any = Core(1)
    core.upsert_desired(_intent("old", 1, "doc", 1, ["write"]))
    old = _claimed(core, "doc", 1, 0)
    assert core.record_retryable("doc", old.identity, "busy", 9)[1] == (
        lazily.EffectCasFailure(lazily.EffectCasFailureKind.RETRYING, retry_at=9)
    )
    assert core.pending_keys(8) == []
    assert core.pending_keys(9) == ["doc"]
    assert core.claim("doc", 1, 8)[1] == lazily.EffectCasClaim(
        lazily.EffectCasClaimKind.WAITING, retry_at=9
    )
    core.upsert_desired(_intent("new", 2, "doc", 2, ["write"]))
    assert _claimed(core, "doc", 1, 8).identity.effect == "new"


def test_upsert_epoch_rules() -> None:
    core: Any = Core(1)
    intent = _intent("a", 2, "doc", 0, ["w"])
    assert core.upsert_desired(intent)[1].kind == "accepted"
    assert core.upsert_desired(intent)[1].kind == "unchanged"
    assert core.upsert_desired(_intent("b", 2, "doc", 0, ["w"]))[1].kind == (
        "epoch_conflict"
    )
    assert core.upsert_desired(_intent("c", 1, "doc", 0, ["w"]))[1] == (
        lazily.EffectCasUpsert(lazily.EffectCasUpsertKind.STALE_EPOCH, current=2)
    )
    attempt = _claimed(core, "doc", 1, 0)
    core.record_applied("doc", attempt.identity, "r", 1)
    assert core.upsert_desired(_intent("d", 2, "doc", 0, ["w"]))[1] == (
        lazily.EffectCasUpsert(
            lazily.EffectCasUpsertKind.ALREADY_APPLIED, applied_through=2
        )
    )


def test_run_once_routes_the_adapter_outcome_outside_the_core() -> None:
    cas: lazily.EffectCas[str, int, str, str, str] = lazily.EffectCas({}, 1)
    cas.upsert_desired(_intent("e", 1, "doc", 5, ["canonical", "snapshot"]))
    backend = _Backend(5)
    first = cas.run_once("doc", 0, backend, 0)
    assert first == lazily.EffectCasApplied(
        lazily.EffectCasAppliedKind.STEP_APPLIED, next_step=1
    )
    second = cas.run_once("doc", 0, backend, 0)
    assert second == lazily.EffectCasApplied(
        lazily.EffectCasAppliedKind.COMPLETED, applied_through=1
    )
    assert backend.mutations == 2, "each step applied exactly once"
    state = cas.state("doc")
    assert state is not None and state.terminal is not None
    assert [r.authority for r in state.terminal.receipts] == [6, 7]
    assert cas.run_once("doc", 0, backend, 0) == lazily.EffectCasClaim(
        lazily.EffectCasClaimKind.EMPTY
    )


def test_durable_image_round_trips_and_recovery_replays_the_same_identity() -> None:
    core: Any = Core(3)
    core.upsert_desired(_intent("plan", 1, "doc", 10, ["canonical", "snapshot"]))
    first = _claimed(core, "doc", 3, 0)
    core.record_applied("doc", first.identity, "r0", 11)
    second = _claimed(core, "doc", 3, 0)
    core.upsert_desired(_intent("other", 1, "aux", 0, ["w"]))
    image = core.durable_state()

    restored: Any = Core.hydrate(image)
    assert restored.snapshot() == core.snapshot()
    assert restored.durable_state() == image
    # The crashed actor's late receipt is fenced once recovery reconnects.
    assert restored.reconnect(4)[1].requeued == 1
    assert restored.record_applied("doc", second.identity, "late", 12)[1] == (
        lazily.EffectCasApplied(lazily.EffectCasAppliedKind.STALE_GENERATION, current=4)
    )
    replay = _claimed(restored, "doc", 4, 0)
    assert replay.identity.effect == second.identity.effect
    assert replay.identity.step == 1 and replay.identity.attempt == 2
    assert replay.expected == 11 and len(replay.prior_receipts) == 1


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (
            lambda image, key: (key, key),
            lazily.EffectCasHydrateError.DUPLICATE_KEY,
        ),
        (
            lambda image, key: (dataclasses.replace(key, key="elsewhere"),),
            lazily.EffectCasHydrateError.KEY_MISMATCH,
        ),
    ],
)
def test_hydrate_refuses_a_corrupt_image(mutate: Any, error: str) -> None:
    core: Any = Core(1)
    core.upsert_desired(_intent("e", 1, "doc", 0, ["w"]))
    image = core.durable_state()
    corrupt = lazily.EffectCasDurableState(
        generation=image.generation, keys=mutate(image, image.keys[0])
    )
    with pytest.raises(lazily.EffectCasHydrateError) as excinfo:
        Core.hydrate(corrupt)
    assert str(excinfo.value) == error


def test_hydrate_refuses_an_attempt_from_the_future() -> None:
    core: Any = Core(5)
    core.upsert_desired(_intent("e", 1, "doc", 0, ["w"]))
    _claimed(core, "doc", 5, 0)
    image = core.durable_state()
    with pytest.raises(lazily.EffectCasHydrateError) as excinfo:
        Core.hydrate(lazily.EffectCasDurableState(generation=4, keys=image.keys))
    assert str(excinfo.value) == lazily.EffectCasHydrateError.FUTURE_GENERATION


def test_thread_safe_shell_serializes_core_access_on_the_shared_lock() -> None:
    import threading

    ts = lazily.ThreadSafeContext()
    cas: lazily.ThreadSafeEffectCas[str, int, str, str, str] = (
        lazily.ThreadSafeEffectCas({}, 1, ts=ts)
    )
    assert cas.thread_safe_context is ts
    done = threading.Event()

    def worker() -> None:
        cas.upsert_desired(_intent("e", 1, "doc", 0, ["w"]))
        done.set()

    with ts.lock:
        thread = threading.Thread(target=worker)
        thread.start()
        assert not done.wait(0.2), "upsert reached the core without the lock"
    thread.join(5)
    assert done.is_set() and cas.pending_keys(0) == ["doc"]


def test_same_epoch_upsert_compares_the_original_intent_not_the_advanced_cursor() -> (
    None
):
    """After a plan step applies, the retained cursor expects the step's returned
    authority; a re-sent ORIGINAL intent is still the same intent."""
    core: Any = Core(1)
    original = _intent("plan", 1, "doc", 10, ["canonical", "snapshot"])
    core.upsert_desired(original)
    first = _claimed(core, "doc", 1, 0)
    core.record_applied("doc", first.identity, "r0", 11)
    assert core.state("doc").pending.expected == 11
    assert core.upsert_desired(original)[1].kind == "unchanged"
    advanced = _intent("plan", 1, "doc", 11, ["canonical", "snapshot"])
    assert core.upsert_desired(advanced)[1].kind == "epoch_conflict"
    _claimed(core, "doc", 1, 0)
    assert core.upsert_desired(original)[1].kind == "unchanged"
