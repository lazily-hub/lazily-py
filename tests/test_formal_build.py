"""lazily-formal integration — gates the Python test suite on the Lean formal
model building (all theorems proved).

This makes ``lazily-formal`` part of the lazily-py test suite: if the sibling
``lazily-formal`` Lean repository and the ``lake`` build tool are co-located,
this module runs ``lake build`` and fails the suite if any theorem regresses
(a proof no longer checks). The formal model is the universal behavioral
reference behind the lazily-spec conformance fixtures and the lazily-py
property tests in ``test_statechart_properties.py``,
``test_thread_safe_properties.py``, ``test_async_slot_properties.py``, etc.

When ``lake`` or ``lazily-formal`` is absent (e.g. a standalone PyPI sdist
checkout without the monorepo siblings), the test is skipped — the formal model
is a development-time guarantee, not a runtime dependency.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


_FORMAL_ROOT = Path(__file__).resolve().parents[2] / "lazily-formal"


def _lake_available() -> bool:
    return shutil.which("lake") is not None and _FORMAL_ROOT.is_dir()


@pytest.mark.skipif(
    not _lake_available(),
    reason="lake toolchain or lazily-formal sibling not present (formal gating is dev-time only)",
)
def test_lazily_formal_builds_all_theorems() -> None:
    """``lake build`` succeeds — every Lean theorem in lazily-formal checks.

    Covers: StateMachine, StateChart (parallel-region confluence,
    single-region refinement), Reactive (PartialEq/memo/signal guards),
    ThreadSafe (batch-flush coalescing), Collection/Tree (independent signals,
    atomic move), Reconciliation (LIS move-minimization), AsyncSlotState
    (stale-completion discard), AsyncEffect (cleanup-before-body, disposal).
    """
    result = subprocess.run(
        ["lake", "build"],
        cwd=_FORMAL_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, (
        "lazily-formal `lake build` failed — a theorem regressed:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
