#!/usr/bin/env python3
"""Execute the Core fixture runner and emit a fail-closed evidence report."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn


if TYPE_CHECKING:
    from collections.abc import Callable


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


def fail(message: str) -> NoReturn:
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


def pointer_escape(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def matches(
    node: Any, segments: list[str], path: str = ""
) -> list[tuple[dict[str, Any], str]]:
    """Every concrete assertion block a route pointer resolves to, with its path."""
    if not segments:
        return [(node, path)] if isinstance(node, dict) else []
    head, *tail = segments
    if head == "*":
        if isinstance(node, list):
            children = [(str(index), value) for index, value in enumerate(node)]
        elif isinstance(node, dict):
            children = list(node.items())
        else:
            children = []
        return [
            found
            for key, value in children
            for found in matches(value, tail, f"{path}/{pointer_escape(key)}")
        ]
    if isinstance(node, dict) and head in node:
        return matches(node[head], tail, f"{path}/{pointer_escape(head)}")
    if isinstance(node, list) and head.isdigit() and int(head) < len(node):
        return matches(node[int(head)], tail, f"{path}/{head}")
    return []


def route_segments(pointer: str) -> list[str]:
    return [
        part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:]
    ]


def mutate_leaf(value: Any) -> Any:
    """The replacement for one scalar assertion leaf, or ``None`` if not mutable.

    Only scalars are perturbed. Growing a map or a list fabricates a node id or a
    scenario label the replay never created, and the runner then fails on its own
    lookup, which says nothing about whether the assertion discriminates.
    """
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1_000_003
    if isinstance(value, float):
        return value + 1_000_003.0
    if isinstance(value, str):
        return value + "__core_mutant__"
    if value is None:
        return "__core_mutant__"
    return None


def assertion_leaves(value: Any, path: str) -> list[tuple[Any, Any, str]]:
    """Every mutable scalar leaf beneath an assertion block, in document order."""
    if isinstance(value, dict):
        children = [
            (key, child)
            for key, child in value.items()
            if not (key in PROSE_KEYS and isinstance(child, str))
        ]
    elif isinstance(value, list):
        children = list(enumerate(value))
    else:
        return []
    leaves: list[tuple[Any, Any, str]] = []
    for key, child in children:
        child_path = f"{path}/{pointer_escape(str(key))}"
        if isinstance(child, (dict, list)):
            leaves.extend(assertion_leaves(child, child_path))
        elif mutate_leaf(child) is not None:
            leaves.append((value, key, child_path))
    return leaves


def route_leaves(document: Any, pointer: str) -> list[tuple[Any, Any, str]]:
    return [
        leaf
        for block, path in matches(document, route_segments(pointer))
        for leaf in assertion_leaves(block, path)
    ]


# A mutant only counts as killed when the runner rejected it through its library
# divergence report: `_run_corpus` prints one `DIVERGENCE <model>/<fixture>...
# got X, want Y` line per check whose observed library value disagreed with the
# (mutated) expectation, then fails the divergence ledger. Any other non-zero exit
# (an unknown node id on the runner's own lookup, a KeyError on a renamed scenario
# label, an unconsumed-key ledger failure at session finish) proves the runner READ
# the leaf, not that the assertion over the library discriminates, so it is
# recorded as non-discriminating and the next leaf is tried.
DIVERGENCE_LINE = re.compile(
    r"^\s*DIVERGENCE (?:Context|ThreadSafeContext|AsyncContext)/"
    r"(?P<fixture>[^\s#\[]+)(?:\[\d+\])?#\S+ \S+ got .*, want .*$",
    re.MULTILINE,
)
MAX_MUTANTS_PER_ROUTE = 8


def divergence_lines(output: str, fixture_name: str) -> list[str]:
    """The runner's library-divergence lines that name ``fixture_name``."""
    return [
        match.group(0).strip()
        for match in DIVERGENCE_LINE.finditer(output)
        if match.group("fixture") == fixture_name
    ]


def failure_excerpt(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("E ")]
    return "\n".join(lines[:5])[:1000] if lines else output[-1000:]


def kill_route(
    corpus: Path,
    rel: str,
    route: str,
    mutation_root: Path,
    runner: Callable[[Path], subprocess.CompletedProcess[str]],
) -> dict[str, Any]:
    """Mutate one assertion leaf at a time until a library divergence rejects it."""
    pristine = json.loads((corpus / rel).read_text(encoding="utf-8"))
    leaf_count = len(route_leaves(pristine, route))
    if leaf_count == 0:
        fail(f"{rel}#{route} has no machine assertion to perturb")
    fixture_name = Path(rel).name
    not_discriminating: list[dict[str, Any]] = []
    for attempt in range(min(leaf_count, MAX_MUTANTS_PER_ROUTE)):
        # A fresh parse per attempt: exactly one leaf differs from the corpus.
        document = json.loads(json.dumps(pristine))
        container, key, leaf_path = route_leaves(document, route)[attempt]
        before = container[key]
        container[key] = mutate_leaf(before)
        detail = {"json_pointer": leaf_path, "before": before, "after": container[key]}
        if mutation_root.exists():
            shutil.rmtree(mutation_root)
        shutil.copytree(corpus, mutation_root)
        (mutation_root / rel).write_text(
            json.dumps(document, indent=2) + "\n", encoding="utf-8"
        )
        probe = runner(mutation_root)
        if probe.returncode == 0:
            fail(f"negative control survived for {rel}#{leaf_path}")
        output = probe.stdout + probe.stderr
        divergences = divergence_lines(output, fixture_name)
        if divergences:
            return {
                "mutation": detail,
                "rejected_by": "divergence",
                "observed_exit_code": probe.returncode,
                "divergences": divergences[:10],
                "non_discriminating_mutants": not_discriminating,
            }
        not_discriminating.append(
            {
                "mutation": detail,
                "rejected_by": "other_failure",
                "observed_exit_code": probe.returncode,
                "output_excerpt": failure_excerpt(output),
            }
        )
    fail(
        f"no mutated assertion leaf of {rel}#{route} was rejected by a library "
        f"divergence ({len(not_discriminating)} mutant(s) failed for another "
        f"reason: {[item['mutation']['json_pointer'] for item in not_discriminating]})"
    )


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
                    kill = kill_route(
                        corpus, rel, route, scratch / "mutant", run_runner
                    )
                    claim_id = f"{rel}#{route}"
                    claims[claim_id] = {
                        "status": "mutation-killed",
                        "models": executed,
                        "mutation": kill["mutation"],
                        "rejected_by": kill["rejected_by"],
                        "divergences": kill["divergences"],
                        "non_discriminating_mutants": kill[
                            "non_discriminating_mutants"
                        ],
                        "observed_exit_code": kill["observed_exit_code"],
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
