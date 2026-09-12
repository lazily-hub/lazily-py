"""A broken Makefile must not be announced as OK (#lzgrepcpipefail).

``scripts/check-ci-reach.sh`` derives every verdict from ``make -n``, read
through ``"$MAKE_BIN" -n "$@" 2>/dev/null | grep -v ... | join_continuations
|| true``. That ``|| true`` is right for the ``grep``: a recipe whose every
line is make's own chatter leaves the filter with nothing to print, and zero
commands is a legitimate measurement — the guard's "carrying no gate" verdict,
for a target that cannot fail a build and so cannot hide one.

It is also indiscriminate. make's OWN failure exits nonzero through the same
pipeline, ``2>/dev/null`` hides what it said, and the empty stdout is then read
as "this recipe runs no checkable command". The target drops out of the reach
requirement and the guard prints OK.

Measured on the guard as it stood before the sanity gate landed. Giving
``test:`` a prerequisite with no rule makes ``make -n test`` exit 2 with
"No rule to make target ... needed by 'test'", and the guard printed::

    no gate  test      recipe runs no checkable command
    check-ci-reach: OK - 7 target(s) reached by CI, 0 excused, 2 carrying no gate

and exited 0. That is a FALSE GREEN, not a fail-closed misdiagnosis: the pytest
target — which carries conformance rungs 2-4 — had quietly stopped being
required in CI, and nothing said so.

These tests drive the real script in a subprocess against a scratch copy of the
real Makefile and the real workflow, rather than restating its logic in Python:
the thing under test is a bash file, and a Python restatement would be a second
definition to keep in step. The scratch copy is also why the perturbation is
safe — the repo's own Makefile is never written.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
CI_REACH_SCRIPT = REPO_ROOT / "scripts" / "check-ci-reach.sh"

#: Everything the guard reads out of the working tree. Copied whole so the
#: scratch run is the real run: the real closure, the real workflow bodies, the
#: real excuse list.
_INPUTS = (
    Path("Makefile"),
    Path("scripts/check-ci-reach.sh"),
    Path("scripts/ci-reach.conf"),
    Path(".github/workflows/precommit.yml"),
)

#: A prerequisite with no rule and no file. make refuses the whole target rather
#: than dry-running it, which is the state under test. Anything make cannot
#: build would do; a missing generated file is simply the shape this happens in.
_UNBUILDABLE = "build/generated-fixtures-that-no-rule-makes.json"


def _scratch_tree(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    for relative in _INPUTS:
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / relative, destination)
    return root


def _run_guard(tree: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(tree / "scripts" / "check-ci-reach.sh")],
        cwd=tree,
        capture_output=True,
        text=True,
    )


def _break_a_closure_target(tree: Path) -> None:
    """Give `test:` a prerequisite make has no rule for.

    `test` is chosen because it is the target that runs the suite, so a reader
    of a failure here sees the cost: losing it from the reach requirement loses
    conformance rungs 2-4 from CI's obligations.
    """
    makefile = tree / "Makefile"
    source = makefile.read_text(encoding="utf-8")
    rule = "\ntest:\n"
    assert source.count(rule) == 1, (
        f"expected exactly one `test:` rule line in the Makefile to perturb; "
        f"found {source.count(rule)}. The perturbation has to land on a target "
        f"that is really in `make check`'s closure or this test proves nothing."
    )
    makefile.write_text(
        source.replace(rule, f"\ntest: {_UNBUILDABLE}\n", 1), encoding="utf-8"
    )


@pytest.fixture(name="tree")
def _tree(tmp_path: Path) -> Path:
    if shutil.which("make") is None:  # pragma: no cover - make is a hard dep here
        raise AssertionError(
            "no `make` on PATH, so this guard cannot be exercised at all. That is "
            "a broken environment, not a reason to skip: check-ci-reach.sh IS a "
            "make consumer."
        )
    return _scratch_tree(tmp_path)


def test_the_scratch_tree_reproduces_the_real_verdict(tree: Path) -> None:
    """Premise, not finding: the copy behaves like the checkout it came from.

    Every assertion below is about a PERTURBED copy of this tree, so a copy that
    did not already report OK would make those assertions meaningless. If this
    fires on its own, look at `make ci-reach` in the checkout first — the
    perturbation tests are downstream of it.
    """
    result = _run_guard(tree)
    assert result.returncode == 0, (
        f"the unperturbed scratch copy did not report OK, so the perturbation "
        f"tests below have no baseline to move away from.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "check-ci-reach: OK" in result.stdout, (
        f"the scratch copy exited 0 without reaching its verdict line.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_a_make_failure_is_refused_and_named(tree: Path) -> None:
    """The false green, driven end to end."""
    _break_a_closure_target(tree)

    premise = subprocess.run(
        ["make", "-n", "check"], cwd=tree, capture_output=True, text=True
    )
    assert premise.returncode != 0, (
        f"`make -n check` still succeeds against the perturbed Makefile, so the "
        f"state this test is about was never produced.\n"
        f"stdout:\n{premise.stdout}\nstderr:\n{premise.stderr}"
    )

    result = _run_guard(tree)

    assert result.returncode != 0, (
        f"the guard reported OK over a Makefile make itself refuses: every "
        f"recipe read as empty, so every target was excused as carrying no gate "
        f"(#lzgrepcpipefail).\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "FAILED" in result.stderr, (
        f"the guard failed without saying that MAKE is what failed, which is the "
        f"whole difference between this and an ordinary reachability miss.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert _UNBUILDABLE in result.stderr, (
        f"the guard did not pass make's own diagnostic through, so a reader is "
        f"told the Makefile is broken without being told where.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_the_failure_is_not_reported_as_a_missing_gate(tree: Path) -> None:
    """Fail-closed is not enough; the SUBJECT has to be right.

    A guard that refused here by some other route — reporting `test` as
    unreached, say — would still send the reader to the workflow file looking
    for a step that is already there. The finding is about the Makefile.
    """
    _break_a_closure_target(tree)

    result = _run_guard(tree)
    combined = result.stdout + result.stderr

    assert "no gate  test" not in combined, (
        f"`test` was classified as carrying no checkable command. It carries the "
        f"whole suite; make merely refused to dry-run it (#lzgrepcpipefail).\n"
        f"{combined}"
    )
    assert "check-ci-reach: OK" not in result.stdout, (
        f"the guard reached its OK verdict line anyway.\n{combined}"
    )
