from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "api" / "core-v1.json"
CHECKER = ROOT / "scripts" / "check-core-api.py"


def run_checker(snapshot: Path = SNAPSHOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), "--snapshot", str(snapshot)],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )


def test_core_api_snapshot_matches_real_public_surface() -> None:
    result = run_checker()
    assert result.returncode == 0, result.stderr


def test_core_api_gate_rejects_a_mutated_snapshot(tmp_path: Path) -> None:
    snapshot = json.loads(SNAPSHOT.read_text())
    snapshot["surface"] = snapshot["surface"][1:]
    mutated = tmp_path / "core-v1.json"
    mutated.write_text(json.dumps(snapshot))

    result = run_checker(mutated)

    assert result.returncode == 1
    assert "Core API snapshot does not match" in result.stderr


@pytest.mark.parametrize("endpoint", ["oldest_supported", "current"])
def test_same_distribution_supports_declared_core_generation(endpoint: str) -> None:
    snapshot = json.loads(SNAPSHOT.read_text())
    compatibility = snapshot["module_compatibility"]
    generation = compatibility[f"{endpoint}_core_api_generation"]

    assert generation == snapshot["core_api_generation"] == 1
    assert compatibility["independently_shipped"] is False
