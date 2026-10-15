#!/usr/bin/env python3
"""Execute the Core fixture runner and emit a fail-closed evidence report."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


BINDING_ID = "python"
PROSE_KEYS = {"comment", "description", "note", "notes", "why"}
RUNNER = (
    sys.executable,
    "-m",
    "pytest",
    "tests/test_reactive_graph_conformance.py",
    "-q",
)
FLAVORS = {
    "Context": "single_threaded",
    "ThreadSafeContext": "thread_safe",
    "AsyncContext": "async",
}


def fail(message: str) -> None:
    raise SystemExit(f"core conformance: {message}")


def required_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        fail(f"{name} is required")
    return Path(value).resolve()


def run_runner(
    corpus: Path, observation: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["LAZILY_SPEC_CONFORMANCE_DIR"] = str(corpus)
    env["LAZILY_CORE_FOCUSED_RUN"] = "1"
    env.pop("LAZILY_CORE_REACTIVE_REPORT_PATH", None)
    if observation is not None:
        observation.write_text("", encoding="utf-8")
        env["LAZILY_CORE_REACTIVE_REPORT_PATH"] = str(observation)
    return subprocess.run(
        RUNNER,
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
    )


def fixture_digest(profile: dict[str, Any], corpus: Path) -> str:
    digest = hashlib.sha256()
    for fixture in profile["required_fixtures"]:
        rel = fixture["path"]
        path = corpus / rel
        if not path.is_file():
            fail(f"required fixture is absent: {path}")
        digest.update(rel.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def matches(node: Any, segments: list[str]) -> list[dict[str, Any]]:
    if not segments:
        return [node] if isinstance(node, dict) else []
    head, *tail = segments
    if head == "*":
        values = (
            node
            if isinstance(node, list)
            else list(node.values())
            if isinstance(node, dict)
            else []
        )
        return [item for value in values for item in matches(value, tail)]
    if isinstance(node, dict) and head in node:
        return matches(node[head], tail)
    if isinstance(node, list) and head.isdigit() and int(head) < len(node):
        return matches(node[int(head)], tail)
    return []


def mutant(value: Any) -> Any:
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, float):
        return value + 1.0
    if isinstance(value, str):
        return value + "__core_mutant__"
    if isinstance(value, list):
        return [*value, "__core_mutant__"]
    if isinstance(value, dict):
        return {**value, "__core_mutant__": True}
    return "__core_mutant__"


def mutate_route(document: dict[str, Any], pointer: str) -> int:
    segments = [
        part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:]
    ]
    changed = 0
    for block in matches(document, segments):
        for key in sorted(block):
            if key in PROSE_KEYS and isinstance(block[key], str):
                continue
            block[key] = mutant(block[key])
            changed += 1
            break
    return changed


def pointer_escape(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def main() -> None:
    profile_path = required_path("LAZILY_CORE_PROFILE_PATH")
    spec_root = required_path("LAZILY_CORE_SPEC_ROOT")
    report_path = required_path("LAZILY_CORE_REPORT_PATH")
    reports_root = required_path("LAZILY_CORE_REPORTS_ROOT")
    if os.environ.get("LAZILY_CORE_BINDING_ID", BINDING_ID) != BINDING_ID:
        fail("LAZILY_CORE_BINDING_ID does not name python")

    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    binding = next(
        (item for item in profile["bindings"] if item["id"] == BINDING_ID), None
    )
    if binding is None:
        fail("profile does not contain the python binding")
    corpus = spec_root / "conformance"
    observed_digest = fixture_digest(profile, corpus)
    if observed_digest != profile["fixture_corpus"]["digest"]:
        fail("Core fixture corpus digest does not match the profile")

    assertion_schema = json.loads(
        (spec_root / "schemas/assertion-blocks.json").read_text(encoding="utf-8")
    )
    routes: dict[str, list[str]] = {
        fixture["path"]: [] for fixture in profile["required_fixtures"]
    }
    for route in assertion_schema["x-fixture-routes"]:
        if route["fixture"] in routes:
            routes[route["fixture"]].append(route["pointer"])

    with tempfile.TemporaryDirectory(prefix="lazily-core-python-") as scratch_name:
        scratch = Path(scratch_name)
        observation = scratch / "reactive-runs.jsonl"
        baseline = run_runner(corpus, observation)
        if baseline.returncode != 0:
            sys.stderr.write(baseline.stdout)
            sys.stderr.write(baseline.stderr)
            fail("baseline Core fixture runner failed")
        records = [
            json.loads(line)
            for line in observation.read_text(encoding="utf-8").splitlines()
            if line
        ]
        expected_models = {"Context", "ThreadSafeContext", "AsyncContext"}
        if {record["model"] for record in records} != expected_models:
            fail("runner did not report all three execution flavors")

        evidence_dir = reports_root / BINDING_ID
        evidence_dir.mkdir(parents=True, exist_ok=True)
        evidence_path = evidence_dir / "assertion-evidence.json"
        claims: dict[str, Any] = {}
        fixtures = []
        runtime_skips = []

        for required in profile["required_fixtures"]:
            rel = required["path"]
            name = Path(rel).name
            executed = sorted(
                record["model"] for record in records if name in record["replayed"]
            )
            skipped = sorted(
                (record["model"], record["skipped"][name])
                for record in records
                if name in record["skipped"]
            )
            status = (
                "passed"
                if len(executed) == len(records)
                else "partial"
                if executed
                else "skipped"
            )
            if skipped:
                if not executed:
                    reasons = sorted({reason for _, reason in skipped})
                    runtime_skips.append(
                        {"fixture": rel, "scenario": "*", "reason": "; ".join(reasons)}
                    )
                else:
                    runtime_skips.extend(
                        {
                            "fixture": rel,
                            "scenario": "*",
                            "execution_flavor": FLAVORS[model],
                            "reason": reason,
                        }
                        for model, reason in skipped
                    )

            blocks = []
            if executed:
                for route in routes[rel]:
                    mutation_root = scratch / "mutant"
                    if mutation_root.exists():
                        shutil.rmtree(mutation_root)
                    shutil.copytree(corpus, mutation_root)
                    fixture_path = mutation_root / rel
                    document = json.loads(fixture_path.read_text(encoding="utf-8"))
                    mutations = mutate_route(document, route)
                    if mutations == 0:
                        fail(f"{rel}#{route} has no machine assertion to perturb")
                    fixture_path.write_text(
                        json.dumps(document, indent=2) + "\n", encoding="utf-8"
                    )
                    probe = run_runner(mutation_root)
                    if probe.returncode == 0:
                        fail(f"negative control survived for {rel}#{route}")
                    claim_id = f"{rel}#{route}"
                    claims[claim_id] = {
                        "status": "mutation-killed",
                        "models": executed,
                        "mutated_blocks": mutations,
                        "mutation": (
                            "changed the first non-prose assertion in every concrete "
                            "block matched by this route"
                        ),
                        "observed_exit_code": probe.returncode,
                        "runner": list(RUNNER),
                    }
                    blocks.append(
                        {
                            "id": route,
                            "consumed": True,
                            "discriminability_evidence": (
                                f"{BINDING_ID}/assertion-evidence.json#/claims/{pointer_escape(claim_id)}"
                            ),
                        }
                    )

            fixtures.append(
                {
                    "path": rel,
                    "scenario_ids": required["scenario_ids"] if executed else [],
                    "proof_classes": required["proof_classes"],
                    "assertion_blocks": blocks,
                    "status": status,
                }
            )

        evidence_path.write_text(
            json.dumps(
                {"schema_version": 1, "binding_id": BINDING_ID, "claims": claims},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        report = {
            "schema_version": 1,
            "profile_id": profile["profile_id"],
            "binding_id": BINDING_ID,
            "fixture_corpus_digest": observed_digest,
            "fixtures": fixtures,
            "runtime_skips": runtime_skips,
            "fixtureless_claims": [],
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
