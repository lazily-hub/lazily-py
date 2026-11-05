"""uv.lock must match pyproject.toml, and nothing may repair it first (#lazilylockstale).

`release: prepare 0.45.0` bumped pyproject.toml and left uv.lock recording
lazily 0.44.0. Nothing failed: every `uv run` and every `uv sync` without
`--locked` / `--frozen` re-locks a stale lockfile and exits 0, and CI installed
with `uv sync --frozen`, which reads the lock without comparing it to
pyproject.toml at all. The drift surfaced only as a dirty worktree in every
checkout that ran anything.

The gate is `uv lock --check` (`make lock-check`, and the
`Lockfile is current (make lock-check)` CI step). It is only a gate if it looks
at the lock BEFORE anything rewrites it, so this file pins three things:

* the lock itself is current (statically, and through uv on a scratch copy);
* the gate bites — a pyproject version bump the lock does not record fails it;
* the ordering: `uv lock --check` is the first command `make check` runs, the
  CI step runs before any `uv sync` / `uv run` step, and the CI install uses
  `uv sync --locked`, never `--frozen`.

The ordering checks are functions over Makefile text / workflow data, each
exercised against the real file AND a perturbed one, so a checker that has
quietly stopped matching anything cannot pass.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml


if TYPE_CHECKING:
    from collections.abc import Mapping


ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
LOCK = ROOT / "uv.lock"
MAKEFILE = ROOT / "Makefile"
WORKFLOW = ROOT / ".github" / "workflows" / "precommit.yml"

LOCK_CHECK_COMMAND = "uv lock --check"
LOCK_STEP_NAME = "Lockfile is current (make lock-check)"
GATE_JOB = "precommit"

# Any uv invocation that may re-lock a stale lockfile before the gate sees it.
_RELOCKING_UV = re.compile(r"\buv\s+(?:run|sync)\b")
_UV_SYNC = re.compile(r"\buv\s+sync\b")
_PROJECT_VERSION = re.compile(r'^version = "([^"]+)"$', re.MULTILINE)
_LOCK_REVISION = re.compile(r"^revision = (\d+)$", re.MULTILINE)


def _uv() -> str:
    uv = shutil.which("uv")
    if uv is None:
        if os.environ.get("CI"):
            pytest.fail("uv is not on PATH in CI; the lockfile gate cannot run")
        pytest.skip("uv is not on PATH")
    return uv


# `UV_*` variables that only say WHERE uv's own state lives, never what a
# resolution means (#lazilypyci). Everything else under `UV_` is dropped.
#
# The suite runs under `uv run`, so the inherited environment may carry settings
# that change what `uv lock --check` proves: `UV_FROZEN` / `UV_LOCKED` /
# `UV_OFFLINE`, an index redirect, `UV_PROJECT`, `UV_PYTHON`. Stripping all of
# `UV_*` removed those, and also removed the locators. pyproject.toml sets
# `python-preference = "only-managed"`, so the system interpreter is never a
# candidate; `astral-sh/setup-uv` installs the managed Python under
# `UV_PYTHON_INSTALL_DIR` (`_temp/uv-python-dir`), and without it uv looks only
# in the default managed directory, which is empty on a fresh runner. Every
# scratch check then exited 2 with "No interpreter found ... uv is set to
# offline mode" — CI run 41986502172 — while passing on any machine with a
# managed Python in the default location.
#
# Passing `--python sys.executable` would also have found an interpreter, but
# it bypasses the project's interpreter discovery, so the scratch check would
# no longer run uv the way `make lock-check` and the CI gate step do.
_UV_LOCATORS = frozenset({"UV_PYTHON_INSTALL_DIR", "UV_CACHE_DIR"})


def _uv_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if environ is None else environ
    env = {
        k: v for k, v in source.items() if not k.startswith("UV_") or k in _UV_LOCATORS
    }
    env.pop("VIRTUAL_ENV", None)
    return env


def _scratch_project(tmp_path: Path) -> Path:
    # pyproject.toml carries static metadata (no `dynamic`), so these two files
    # are everything `uv lock` reads for this project.
    tree = tmp_path / "project"
    tree.mkdir()
    shutil.copy2(PYPROJECT, tree / "pyproject.toml")
    shutil.copy2(LOCK, tree / "uv.lock")
    return tree


# `uv lock --check` exits 1 for "the lock needs to be updated" and 2 when uv
# could not reach a verdict at all (no interpreter, unreadable project). Only
# exit 1 is evidence about the lock.
_STALE_EXIT = 1
_STALE_MESSAGE = "needs to be updated"


def _lock_check(tree: Path) -> subprocess.CompletedProcess[str]:
    # --offline: a current lock is verified from the lock alone, so this needs
    # no network and no cache; a stale one fails the same way online or off.
    result = subprocess.run(
        [_uv(), "lock", "--check", "--offline"],
        cwd=tree,
        env=_uv_env(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    # A uv that could not run is neither a pass nor a stale-lock verdict; say so
    # instead of letting the caller report "uv.lock is stale" (#lazilypyci).
    assert result.returncode in (0, _STALE_EXIT), (
        f"`uv lock --check` exited {result.returncode} without a verdict on the lock\n"
        f"{result.stderr}"
    )
    return result


def _locked_lazily(lock: dict[str, Any]) -> dict[str, Any]:
    matches = [
        p
        for p in lock.get("package", [])
        if p.get("name") == "lazily" and p.get("source") == {"editable": "."}
    ]
    assert len(matches) == 1, (
        f"expected one editable lazily package in uv.lock, found {len(matches)}"
    )
    return matches[0]


# ---- the lock is current ---------------------------------------------------


def test_lock_records_the_pyproject_version() -> None:
    project = tomllib.loads(PYPROJECT.read_text())["project"]
    lock = tomllib.loads(LOCK.read_text())
    assert "dynamic" not in project, (
        "pyproject.toml grew dynamic metadata; _scratch_project's two-file copy is no longer the whole input to `uv lock`"
    )
    assert _locked_lazily(lock)["version"] == project["version"], (
        "uv.lock records a different lazily version than pyproject.toml — run `uv lock` and commit it"
    )
    assert lock["requires-python"] == project["requires-python"]


def test_lock_passes_uv_lock_check(tmp_path: Path) -> None:
    result = _lock_check(_scratch_project(tmp_path))
    assert result.returncode == 0, (
        f"uv.lock is stale — run `uv lock` and commit it\n{result.stderr}"
    )


# ---- the gate bites --------------------------------------------------------


def test_lock_check_fails_on_a_version_bump_the_lock_does_not_record(
    tmp_path: Path,
) -> None:
    tree = _scratch_project(tmp_path)
    pyproject = tree / "pyproject.toml"
    text = pyproject.read_text()
    current = _PROJECT_VERSION.search(text)
    assert current is not None
    pyproject.write_text(
        _PROJECT_VERSION.sub(f'version = "{current.group(1)}.post999"', text, count=1)
    )

    result = _lock_check(tree)
    assert result.returncode == _STALE_EXIT, (
        "uv lock --check passed a lock that records the wrong project version"
    )
    assert _STALE_MESSAGE in result.stderr, result.stderr


def test_lock_check_ignores_an_older_revision_number(tmp_path: Path) -> None:
    # The Makefile comment claims a newer uv on CI does not red the build just
    # by writing a newer lock `revision`. Pin the claim rather than trust it.
    tree = _scratch_project(tmp_path)
    lock = tree / "uv.lock"
    text = lock.read_text()
    revision = _LOCK_REVISION.search(text)
    assert revision is not None
    if int(revision.group(1)) <= 1:
        pytest.skip("lock revision is already the lowest")
    lock.write_text(
        _LOCK_REVISION.sub(f"revision = {int(revision.group(1)) - 1}", text, count=1)
    )

    result = _lock_check(tree)
    assert result.returncode == 0, result.stderr


# ---- the scratch check runs uv the way the gate does -----------------------


def test_uv_env_keeps_only_the_uv_locators() -> None:
    inherited = {
        "PATH": "/usr/bin",
        "HOME": "/home/runner",
        "VIRTUAL_ENV": "/repo/.venv",
        "UV_PYTHON_INSTALL_DIR": "/home/runner/work/_temp/uv-python-dir",
        "UV_CACHE_DIR": "/home/runner/work/_temp/setup-uv-cache",
        "UV_FROZEN": "1",
        "UV_LOCKED": "1",
        "UV_OFFLINE": "0",
        "UV_INDEX_URL": "https://example.invalid/simple",
        "UV_DEFAULT_INDEX": "https://example.invalid/simple",
        "UV_PROJECT": "/elsewhere",
        "UV_PYTHON": "3.13",
        "UV_PYTHON_PREFERENCE": "only-system",
    }
    assert _uv_env(inherited) == {
        "PATH": "/usr/bin",
        "HOME": "/home/runner",
        "UV_PYTHON_INSTALL_DIR": "/home/runner/work/_temp/uv-python-dir",
        "UV_CACHE_DIR": "/home/runner/work/_temp/setup-uv-cache",
    }


# ---- nothing repairs the lock before the gate: make ------------------------


def _make_check_order_problems(makefile_text: str, tmp_path: Path) -> list[str]:
    tree = tmp_path / "make"
    tree.mkdir(exist_ok=True)
    (tree / "Makefile").write_text(makefile_text)

    def dry_run(target: str) -> list[str]:
        result = subprocess.run(
            ["make", "-n", "--no-print-directory", target],
            cwd=tree,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    problems = []
    if dry_run("lock-check") != [LOCK_CHECK_COMMAND]:
        problems.append(
            f"`make lock-check` does not run exactly `{LOCK_CHECK_COMMAND}`"
        )
    check_commands = dry_run("check")
    if not check_commands or check_commands[0] != LOCK_CHECK_COMMAND:
        first = check_commands[0] if check_commands else "<nothing>"
        problems.append(f"`make check` runs `{first}` before `{LOCK_CHECK_COMMAND}`")
    return problems


def test_make_check_runs_the_lock_gate_first(tmp_path: Path) -> None:
    assert _make_check_order_problems(MAKEFILE.read_text(), tmp_path) == []


def test_make_order_checker_rejects_a_gate_moved_after_a_uv_run(tmp_path: Path) -> None:
    text = MAKEFILE.read_text()
    moved = text.replace(
        "check: lock-check format-check ", "check: format-check lock-check ", 1
    )
    assert moved != text, (
        "the `check:` rule no longer starts with `lock-check format-check`; update this perturbation"
    )
    problems = _make_check_order_problems(moved, tmp_path)
    assert problems == [
        f"`make check` runs `uv run ruff format --check src/lazily/ tests/` before `{LOCK_CHECK_COMMAND}`"
    ]


def test_make_order_checker_rejects_a_neutered_gate(tmp_path: Path) -> None:
    text = MAKEFILE.read_text()
    neutered = text.replace(
        f"lock-check:\n\t{LOCK_CHECK_COMMAND}\n", "lock-check:\n\tuv lock\n", 1
    )
    assert neutered != text
    assert len(_make_check_order_problems(neutered, tmp_path)) == 2


# ---- nothing repairs the lock before the gate: CI --------------------------


def _ci_lock_problems(workflow: dict[str, Any]) -> list[str]:
    steps = workflow["jobs"][GATE_JOB]["steps"]
    runs = [(step.get("name", "<unnamed>"), str(step.get("run", ""))) for step in steps]

    gate_indices = [i for i, (name, _) in enumerate(runs) if name == LOCK_STEP_NAME]
    if len(gate_indices) != 1:
        return [
            f"expected one `{LOCK_STEP_NAME}` step in job {GATE_JOB}, found {len(gate_indices)}"
        ]
    gate = gate_indices[0]

    problems = []
    if runs[gate][1].strip() != LOCK_CHECK_COMMAND:
        problems.append(
            f"`{LOCK_STEP_NAME}` runs `{runs[gate][1].strip()}`, not `{LOCK_CHECK_COMMAND}`"
        )
    for name, run in runs[:gate]:
        if _RELOCKING_UV.search(run):
            problems.append(
                f"step `{name}` runs uv sync/run before the lockfile gate and can re-lock first"
            )
    for name, run in runs:
        for line in run.splitlines():
            if _UV_SYNC.search(line) and ("--frozen" in line or "--locked" not in line):
                problems.append(
                    f"step `{name}` installs with `{line.strip()}`; use `uv sync --locked`"
                )
    return problems


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text())


def test_ci_checks_the_lock_before_any_uv_sync_or_run() -> None:
    assert _ci_lock_problems(_workflow()) == []


def test_ci_checker_rejects_a_frozen_install() -> None:
    workflow = _workflow()
    steps = workflow["jobs"][GATE_JOB]["steps"]
    syncs = [s for s in steps if _UV_SYNC.search(str(s.get("run", "")))]
    assert syncs, "no `uv sync` step left to perturb"
    syncs[0]["run"] = "uv sync --frozen"
    assert _ci_lock_problems(workflow) == [
        f"step `{syncs[0]['name']}` installs with `uv sync --frozen`; use `uv sync --locked`"
    ]


def test_ci_checker_rejects_a_gate_moved_after_the_install() -> None:
    workflow = _workflow()
    steps = workflow["jobs"][GATE_JOB]["steps"]
    gate = next(i for i, s in enumerate(steps) if s.get("name") == LOCK_STEP_NAME)
    install = next(
        i for i, s in enumerate(steps) if _UV_SYNC.search(str(s.get("run", "")))
    )
    assert gate < install
    steps.insert(install, steps.pop(gate))
    problems = _ci_lock_problems(workflow)
    assert any("before the lockfile gate" in p for p in problems), problems


def test_ci_checker_rejects_a_missing_gate() -> None:
    workflow = _workflow()
    steps = workflow["jobs"][GATE_JOB]["steps"]
    workflow["jobs"][GATE_JOB]["steps"] = [
        s for s in steps if s.get("name") != LOCK_STEP_NAME
    ]
    assert _ci_lock_problems(workflow) == [
        f"expected one `{LOCK_STEP_NAME}` step in job {GATE_JOB}, found 0"
    ]
