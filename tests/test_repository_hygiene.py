"""Repository-only packaging hygiene checks."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_local_agent_worktrees_are_not_tracked() -> None:
    """Machine-local worktrees cannot become package or source-archive inputs."""
    result = subprocess.run(
        ["git", "ls-files", "--", ".claude/worktrees"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 and "not a git repository" in result.stderr:
        pytest.skip("repository metadata is unavailable")

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
