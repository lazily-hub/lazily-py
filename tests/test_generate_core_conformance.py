"""The Core report generator credits a negative control only on a library divergence.

A mutated fixture leaf that makes the runner fail on its OWN bookkeeping (an
unknown node id, a renamed scenario label, an unconsumed-key ledger) proves the
runner read the leaf, not that the assertion over the library discriminates.
These tests drive ``kill_route`` with a scripted runner so the over-credit cannot
come back.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/generate-core-conformance.py"
_SPEC = importlib.util.spec_from_file_location("generate_core_conformance", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
gen: Any = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gen)

REL = "reactive-graph/pair.json"
FIXTURE = {
    "expected": {
        "observationally_equal": ["left", "right"],
        "note": "prose is never perturbed",
        "final_state": {"dependents_of": {"topic": 1}},
    }
}
DIVERGENCE = (
    "  DIVERGENCE Context/pair.json[0]#expected:final.dependents_of.topic"
    " — got 1, want 1000004\n"
)
LABEL_CRASH = "E               ValueError: list.index(x): x not in list\n"


def _corpus(tmp_path: Path) -> Path:
    corpus = tmp_path / "corpus"
    (corpus / "reactive-graph").mkdir(parents=True)
    (corpus / REL).write_text(json.dumps(FIXTURE), encoding="utf-8")
    return corpus


def _scripted(outputs: list[str]) -> tuple[Any, list[Any]]:
    """A runner that fails every probe with the next scripted output."""
    seen: list[Any] = []

    def runner(root: Path) -> subprocess.CompletedProcess[str]:
        seen.append(json.loads((root / REL).read_text(encoding="utf-8")))
        return subprocess.CompletedProcess([], 1, outputs[len(seen) - 1], "")

    return runner, seen


def test_leaves_are_scalars_in_document_order_without_prose() -> None:
    leaves = [path for _, _, path in gen.route_leaves(FIXTURE, "/expected")]
    assert leaves == [
        "/expected/observationally_equal/0",
        "/expected/observationally_equal/1",
        "/expected/final_state/dependents_of/topic",
    ]


def test_divergence_lines_must_name_the_mutated_fixture() -> None:
    assert gen.divergence_lines(DIVERGENCE, "pair.json") == [DIVERGENCE.strip()]
    assert gen.divergence_lines(DIVERGENCE, "other.json") == []
    assert gen.divergence_lines(LABEL_CRASH, "pair.json") == []


def test_runner_bookkeeping_failure_is_not_a_kill(tmp_path: Path) -> None:
    """The cs defect: a renamed label crashes the runner's own lookup."""
    runner, seen = _scripted([LABEL_CRASH, LABEL_CRASH, DIVERGENCE])
    kill = gen.kill_route(
        _corpus(tmp_path), REL, "/expected", tmp_path / "mutant", runner
    )
    assert kill["rejected_by"] == "divergence"
    assert kill["mutation"]["json_pointer"] == (
        "/expected/final_state/dependents_of/topic"
    )
    assert [
        m["mutation"]["json_pointer"] for m in kill["non_discriminating_mutants"]
    ] == [
        "/expected/observationally_equal/0",
        "/expected/observationally_equal/1",
    ]
    # Exactly one leaf differs from the corpus per attempt.
    assert seen[0]["expected"]["observationally_equal"] == [
        "left__core_mutant__",
        "right",
    ]
    assert seen[1]["expected"]["observationally_equal"] == [
        "left",
        "right__core_mutant__",
    ]
    assert seen[2]["expected"]["final_state"] == {"dependents_of": {"topic": 1000004}}


def test_route_without_a_divergence_kill_fails_the_report(tmp_path: Path) -> None:
    runner, seen = _scripted([LABEL_CRASH] * 3)
    with pytest.raises(SystemExit, match="rejected by a library divergence"):
        gen.kill_route(_corpus(tmp_path), REL, "/expected", tmp_path / "mutant", runner)
    assert len(seen) == 3


def test_a_surviving_mutant_fails_the_report(tmp_path: Path) -> None:
    def runner(root: Path) -> subprocess.CompletedProcess[str]:
        del root
        return subprocess.CompletedProcess([], 0, "", "")

    with pytest.raises(SystemExit, match="negative control survived"):
        gen.kill_route(_corpus(tmp_path), REL, "/expected", tmp_path / "mutant", runner)
