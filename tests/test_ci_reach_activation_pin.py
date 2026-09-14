"""WHETHER the workflow and the job run at all, pinned (#verifyworkflowactually).

Every rung in ``scripts/check-ci-reach.sh`` before this one is a claim about what
CI *would* run if it ran. lazily-js measured the level underneath them: a pinned
gate step can exist, be unique, be unconditional and run the gate, inside a job
or a workflow that never runs. Three states left this guard at exit 0 with output
byte-identical to a healthy tree — job-level ``if: false``, job-level
``continue-on-error: true``, and ``on:`` reduced to ``workflow_dispatch`` — and
``scripts/ci-reach.conf``'s claim that precommit.yml "runs on every push to every
branch" was a COMMENT, the one premise the whole audit rested on that nothing
checked.

**Values, not absences.** The tempting rule is "no job-level guard", and it is
wrong: lazily-zig has a legitimate job-level ``continue-on-error: ${{ matrix.zig
== 'master' }}`` for its advisory master leg. So the four pins are set-equalities
over exact values in both directions, the same fails-when-stale property as every
other pin in that file.

**The pin is a literal compared against the workflow.** A test that asserted "the
triggers the guard's own scraper observed" would pin nothing at all — it would be
satisfied by any workflow, because both sides would move together. So the tests
below carry their own verbatim copy of precommit.yml's ``on:`` block and of the
gate job's own keys, compare *that* against the workflow file, and compare the
guard's ``EXPECTED_*`` arrays against the same literal. Two independent literals,
both answerable to the file.

**The finding this work produced rather than closed.** precommit.yml has no
``pull_request:`` trigger, and it is the only counted workflow in the family
without one. See ``test_there_is_no_pull_request_trigger`` for what that does and
does not cost; the trigger set is pinned, so it cannot change unnoticed, and it
is deliberately not *corrected* here — changing ``on:`` is a behaviour change.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest


if TYPE_CHECKING:
    from pathlib import Path

from test_ci_reach_closure_pin import _bash_array
from test_ci_reach_make_failure import CI_REACH_SCRIPT, _run_guard, _scratch_tree
from test_ci_reach_step_pin import WORKFLOW, _write_changed


#: precommit.yml's whole `on:` block, VERBATIM. This is the literal the pins are
#: answerable to: it is asserted to occur exactly once in the workflow, and the
#: guard's EXPECTED_TRIGGERS / EXPECTED_TRIGGER_FILTERS are asserted to describe
#: it. Reading the triggers out of the workflow and comparing them to themselves
#: would pass over any `on:` block at all.
ON_BLOCK = """on:
  push:
    branches:
      - "**"
  workflow_dispatch:
"""

#: The gate job's OWN keys, at the job-key indent, verbatim and in file order.
#: `if:` and `continue-on-error:` are absent, which is the fact
#: EXPECTED_GATE_JOB_GUARDS pins as `<absent>`; a job-level guard would appear
#: here as a fifth key.
GATE_JOB = "precommit"
GATE_JOB_OWN_KEYS = ["runs-on", "strategy", "env", "steps"]

#: The matrix line, verbatim. All three legs are BLOCKING — there is no job-level
#: `continue-on-error`, so none of them is advisory.
MATRIX_LINE = "        python-version: [ '3.12', '3.13', '3.14' ]\n"

JOB_LINE = "  precommit:\n"
BRANCHES = '    branches:\n      - "**"\n'
PEER_STEP = """      - name: Interop peer self-check (make test-interop-peer)
        run: uv run poe interop_peer
"""

#: The per-rung report blocks, addressed by the pin each one names. Reverting
#: exactly one and nothing else is how the rungs below are falsified.
_RUNG_BLOCK = (
    r'if \[ -n "\$act_missing" \] \|\| \[ -n "\$act_surplus" \]; then\n'
    r'\tact_report "{label}".*?\n\tstatus=1\nfi\n'
)


@pytest.fixture(name="tree")
def _tree(tmp_path: Path) -> Path:
    if shutil.which("make") is None:  # pragma: no cover - make is a hard dep here
        raise AssertionError(
            "no `make` on PATH, so this guard cannot be exercised at all. That is "
            "a broken environment, not a reason to skip."
        )
    return _scratch_tree(tmp_path)


def _healthy_activation(tree: Path) -> subprocess.CompletedProcess[str]:
    """Refuse to measure anything against a scratch tree that is not green.

    A row that expects GREEN passes vacuously otherwise, and a row that expects
    RED is then measuring whatever was already wrong. The shared scratchpad this
    family works in has already produced one binding's Makefile restored into
    another's tree, so this is checked rather than assumed.
    """
    result = _run_guard(tree)
    assert result.returncode == 0, (
        f"the UNMUTATED scratch tree already exits {result.returncode}, so nothing "
        f"below is a measurement of the perturbation.\n{result.stdout}{result.stderr}"
    )
    return result


def _mutate(tree: Path, relative: str, old: str, new: str, count: int = 1) -> None:
    """Apply a perturbation, ERRORING if it did not land.

    A skipped mutation is indistinguishable from a survived one, which is this
    whole guard's subject. The occurrence count says the pattern was FOUND;
    `_write_changed` says the write LANDED.
    """
    path = tree / relative
    source = path.read_text(encoding="utf-8")
    assert source.count(old) == count, (
        f"expected {count} occurrence(s) of the perturbation target in "
        f"{relative}; found {source.count(old)}. The perturbation has to land on "
        f"the real text or this test proves nothing."
    )
    _write_changed(path, source.replace(old, new, count))


def _revert_rung(tree: Path, pin: str) -> None:
    """Neuter ONE activation rung's report, leaving every other rung in place."""
    path = tree / "scripts" / "check-ci-reach.sh"
    source = path.read_text(encoding="utf-8")
    pattern = re.compile(_RUNG_BLOCK.format(label=re.escape(pin)), re.S)
    hits = pattern.findall(source)
    assert len(hits) == 1, (
        f"expected exactly one report block naming `{pin}` in the guard; found "
        f"{len(hits)}. Falsification reverts one rung and nothing else, so a "
        f"pattern that matches zero or two blocks reverts the wrong thing."
    )
    _write_changed(path, pattern.sub(":\n", source, count=1))
    assert _run_guard(tree).returncode == 0, (
        f"the guard with only `{pin}`'s report reverted is not green on a clean "
        f"tree, so every falsification row below would be measuring a broken "
        f"script rather than a missing rung."
    )


def _revert_floor(tree: Path) -> None:
    """Neuter the independent activation floor while keeping output variables bound."""
    path = tree / "scripts" / "check-ci-reach.sh"
    source = path.read_text(encoding="utf-8")
    start = source.index("# THE ACTIVATION FLOOR:")
    end = source.index('if [ "$unreached_count" -gt 0 ]; then', start)
    assert start < end, "the activation floor is not where this test expects it."
    _write_changed(path, source[:start] + 'floor_gating=""\n\n' + source[end:])
    assert _run_guard(tree).returncode == 0, (
        "the guard with only the activation floor reverted is not green on a "
        "clean tree, so the pre-fix measurement below would be invalid."
    )


def _pin_rows(name: str) -> list[str]:
    """An `EXPECTED_*` array as canonical whitespace-collapsed rows."""
    rows = []
    for entry in _bash_array(name):
        stripped = entry.strip().strip('"')
        assert stripped, f"{name} has a blank entry, which pins nothing."
        rows.append(" ".join(stripped.split()))
    assert rows, f"{name} is empty, so it pins nothing at all."
    return sorted(rows)


def _workflow_text() -> str:
    return (CI_REACH_SCRIPT.parent.parent / WORKFLOW).read_text(encoding="utf-8")


# --------------------------------------------------------------- the literals


def test_the_pinned_triggers_are_the_workflows_own_on_block() -> None:
    """EXPECTED_TRIGGERS and EXPECTED_TRIGGER_FILTERS against a LITERAL.

    Both halves matter and they are different assertions. First, `ON_BLOCK` is
    the workflow's `on:` block verbatim — asserted against the file, so this test
    fails on any edit to it, including one the guard's scraper would read the same
    way. Second, the guard's arrays are asserted to describe that literal. A test
    that instead ran the guard's own scraper and compared the result to the pin
    would be satisfied by every possible workflow: the two sides would move
    together, which is the shape this family has already paid for.
    """
    source = _workflow_text()
    assert source.count(ON_BLOCK) == 1, (
        f"precommit.yml's `on:` block is not the pinned literal. Found "
        f"{source.count(ON_BLOCK)} occurrence(s) of:\n{ON_BLOCK}\n"
        f"Whatever it is now, it decides when every gate in this repo runs — "
        f"update ON_BLOCK, EXPECTED_TRIGGERS and EXPECTED_TRIGGER_FILTERS in the "
        f"same commit, and read test_there_is_no_pull_request_trigger first."
    )
    # Derived from the literal above by splitting it, NOT by asking the guard.
    triggers = [
        line.strip().rstrip(":")
        for line in ON_BLOCK.splitlines()[1:]
        if re.fullmatch(r"  [a-z_]+:", line)
    ]
    assert triggers == ["push", "workflow_dispatch"], triggers
    assert _pin_rows("EXPECTED_TRIGGERS") == sorted(
        f"{WORKFLOW} {trigger}" for trigger in triggers
    ), (
        "EXPECTED_TRIGGERS does not describe the `on:` block pinned above. The "
        "guard would then be asserting a trigger set the workflow does not have, "
        "in whichever direction — a missing row reads as a deleted trigger and a "
        "surplus row as an added one."
    )
    assert _pin_rows("EXPECTED_TRIGGER_FILTERS") == [f"{WORKFLOW} push branches=**"], (
        "EXPECTED_TRIGGER_FILTERS does not describe the `on:` block pinned above. "
        "The `branches:` filter is what makes `push` cover every branch, and so "
        "every same-repo PR head; a narrowed one gates fewer commits than this "
        "repo's excuse list claims."
    )


def test_the_pinned_gate_job_and_its_guards_are_the_workflows_own() -> None:
    """EXPECTED_GATE_JOBS and EXPECTED_GATE_JOB_GUARDS against a LITERAL.

    `GATE_JOB_OWN_KEYS` is the job's own keys read off the file at the job-key
    indent, and the absence of `if:` and `continue-on-error:` from that list IS
    the fact the guard pins as `<absent>`. Pinning the key list rather than
    asserting "no guard is present" is deliberate: the list fails when a job-level
    guard appears AND when one of the four real keys disappears, and the second
    one matters — a job that lost its `steps:` runs no gate at all.
    """
    source = _workflow_text()
    assert source.count(JOB_LINE) == 1, (
        f"expected one `{JOB_LINE.strip()}` job key; found {source.count(JOB_LINE)}."
    )
    body = source.split(JOB_LINE, 1)[1]
    own_keys = [
        match.group(1)
        for match in re.finditer(r"^    ([A-Za-z][A-Za-z0-9_-]*):", body, re.M)
    ]
    assert own_keys == GATE_JOB_OWN_KEYS, (
        f"the gate job's own keys are {own_keys}, not {GATE_JOB_OWN_KEYS}. An "
        f"added `if:` or `continue-on-error:` decides whether every step in this "
        f"job runs or whether its failure counts; a removed key may mean the job "
        f"no longer runs the gates at all."
    )
    assert _pin_rows("EXPECTED_GATE_JOBS") == [f"{WORKFLOW} {GATE_JOB}"], (
        "EXPECTED_GATE_JOBS does not name the one job holding the pinned gate "
        "steps, so the guard is asserting that the gates live somewhere they do "
        "not."
    )
    assert _pin_rows("EXPECTED_GATE_JOB_GUARDS") == [
        f"{WORKFLOW} {GATE_JOB} continue-on-error=<absent>",
        f"{WORKFLOW} {GATE_JOB} if=<absent>",
    ], (
        "EXPECTED_GATE_JOB_GUARDS does not match the job's key list above. "
        "`<absent>` is spelled as a VALUE rather than left as a missing row so "
        "that absent -> present is a changed value with a name, and so that a "
        "legitimate guard (lazily-zig's advisory matrix leg) can be pinned here "
        "instead of being refused."
    )


def test_the_activation_pins_are_declared_non_empty_and_canonical() -> None:
    """Source-level, because an empty or deleted pin cannot fail behaviourally.

    A pin of zero entries is satisfied by nothing in the direction that matters,
    and a deleted array takes its own diagnostics with it — the guard would print
    OK exactly as it did before #verifyworkflowactually. Sorted and de-duplicated
    is asserted rather than asked for in a comment: a duplicate is how a
    hand-merge keeps a row it meant to replace.
    """
    source = CI_REACH_SCRIPT.read_text(encoding="utf-8")
    for name in (
        "EXPECTED_TRIGGERS",
        "EXPECTED_TRIGGER_FILTERS",
        "EXPECTED_GATE_JOBS",
        "EXPECTED_GATE_JOB_GUARDS",
    ):
        entries = _bash_array(name)
        assert entries, f"`{name}` is empty, so it pins nothing."
        rows = [" ".join(e.strip().strip('"').split()) for e in entries]
        assert rows == sorted(rows), (
            f"`{name}` is not sorted, which turns a one-line diff of the pin into "
            f"a puzzle. Canonical order: {sorted(rows)}"
        )
        assert len(set(rows)) == len(rows), (
            f"`{name}` has a duplicate row, so one of the two is dead and a "
            f"reader cannot tell which."
        )
        assert f"{name}=(" in source
    # And the guard refuses an empty one by name rather than reporting OK.
    assert "is empty, so this guard pins nothing about" in source, (
        "the guard no longer refuses an EMPTY activation pin. An empty pin can "
        "only mismatch by reporting every discovered trigger as unpinned, which "
        "reads like a configuration accident rather than a missing pin."
    )


# ------------------------------------------------------------- the three states


@pytest.mark.parametrize(
    ("label", "injected"),
    [
        ("if: false", "    if: false\n"),
        ("a never-true if:", "    if: github.event_name == 'schedule'\n"),
        ("continue-on-error: true", "    continue-on-error: true\n"),
    ],
)
def test_a_job_level_condition_or_discarded_failure_is_refused(
    tree: Path, label: str, injected: str
) -> None:
    """The two states js measured at the JOB level, plus the honest form of one.

    `if: false` and `if: github.event_name == 'schedule'` differ only in how
    obvious they are, which is why the guard does not inspect the value's meaning:
    a job it cannot prove runs is not reach. `continue-on-error: true` is the
    other shape — the job runs, its failure stops failing the build, which is
    reach without enforcement, the same defect as a repairing formatter in CI one
    level up.
    """
    _healthy_activation(tree)
    _mutate(tree, WORKFLOW, JOB_LINE, JOB_LINE + injected)
    result = _run_guard(tree)
    assert result.returncode == 1, (
        f"job-level `{label}` was ACCEPTED. Every gate step is still pinned, "
        f"unique and unconditional, and none of them runs.\n"
        f"{result.stdout}{result.stderr}"
    )
    assert "EXPECTED_GATE_JOB_GUARDS" in result.stderr, (
        f"job-level `{label}` was refused, but not by the job-guard pin — so the "
        f"diagnostic sends the reader somewhere else.\n{result.stderr}"
    )


def test_reducing_the_trigger_to_workflow_dispatch_is_refused(tree: Path) -> None:
    """The third state js measured, and the one with no step-level tell at all.

    Nothing about the steps changes. The workflow simply stops running unless a
    human clicks it, and before this pin the guard printed its healthy verdict
    byte for byte.
    """
    _healthy_activation(tree)
    _mutate(tree, WORKFLOW, ON_BLOCK, "on:\n  workflow_dispatch:\n")
    result = _run_guard(tree)
    assert result.returncode == 1, (
        f"`on:` reduced to workflow_dispatch was ACCEPTED, so the audited "
        f"workflow need never run.\n{result.stdout}{result.stderr}"
    )
    assert "EXPECTED_TRIGGERS" in result.stderr, result.stderr
    assert f"{WORKFLOW}  push" in result.stderr, (
        f"the finding does not name the trigger that left.\n{result.stderr}"
    )


@pytest.mark.parametrize(
    ("label", "old", "new", "names"),
    [
        (
            'branches narrowed to ["main"]',
            BRANCHES,
            '    branches:\n      - "main"\n',
            "branches=main",
        ),
        (
            "a paths: filter introduced",
            BRANCHES,
            BRANCHES + '    paths:\n      - "src/**"\n',
            "paths=src/**",
        ),
        (
            "a pull_request: trigger added",
            "  workflow_dispatch:\n",
            "  pull_request:\n  workflow_dispatch:\n",
            "pull_request",
        ),
    ],
)
def test_a_changed_trigger_or_filter_is_refused_in_both_directions(
    tree: Path, label: str, old: str, new: str, names: str
) -> None:
    """Narrowed, introduced, and ADDED — the pin is set equality, not a floor.

    The `paths:` row is the one that matters most and the reason this is a
    fails-when-introduced check: no binding in this family has a `paths:` filter
    today, and one that excluded the Makefile would mean a gate-retiring edit did
    not even trigger the workflow that would have caught it.

    The ADDED case is here on purpose even though it is an improvement: adding
    `pull_request:` is exactly the edit that would close the gap
    `test_there_is_no_pull_request_trigger` reports, and it reds this guard until
    the pin moves with it. A pin that only fired on removals would let the trigger
    set grow silently, and "grew silently" is how a `paths:`-narrowed trigger
    arrives.
    """
    _healthy_activation(tree)
    _mutate(tree, WORKFLOW, old, new)
    result = _run_guard(tree)
    assert result.returncode == 1, (
        f"{label} was ACCEPTED, so when the gate set runs is not pinned after "
        f"all.\n{result.stdout}{result.stderr}"
    )
    assert names in result.stderr, (
        f"{label} was refused without naming `{names}`, so the reader has to "
        f"diff the workflow to find out what moved.\n{result.stderr}"
    )


@pytest.mark.parametrize("label", ["renamed", "one step moved out"])
def test_a_gate_step_that_changes_job_is_refused(tree: Path, label: str) -> None:
    """A gate step keeps its name, its uniqueness and its command — and moves.

    Every rung above this one is satisfied by both shapes: the step resolves, it
    is unconditional, its anchors are inside it, no other member's anchors are.
    What changes is the JOB, and a job can be skipped, advisory, or gated on a
    condition of its own.
    """
    _healthy_activation(tree)
    if label == "renamed":
        _mutate(tree, WORKFLOW, JOB_LINE, "  gates:\n")
    else:
        _mutate(tree, WORKFLOW, PEER_STEP, "")
        path = tree / WORKFLOW
        path.write_text(
            path.read_text(encoding="utf-8")
            + "\n  interop:\n    runs-on: ubuntu-latest\n    steps:\n"
            + "      - uses: actions/checkout@v4\n"
            + PEER_STEP,
            encoding="utf-8",
        )
    result = _run_guard(tree)
    assert result.returncode == 1, (
        f"a gate step {label} was ACCEPTED.\n{result.stdout}{result.stderr}"
    )
    assert "EXPECTED_GATE_JOBS" in result.stderr, result.stderr


# ------------------------------------------------------------- the accounting


def test_each_activation_rung_is_falsified_alone(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Which rung catches which fault, MEASURED, including the one that catches none.

    Reverting a rung and re-running the fault is the only form of this that means
    anything — a rung's description is not evidence that it fires. Two results
    here contradict what the four pins read as:

    * `EXPECTED_GATE_JOBS` is the sole catcher of NOTHING. Every one of the nine
      attacks is still refused with only it reverted. Its remaining property is
      the vacuity floor's: with `EXPECTED_GATE_JOB_GUARDS` also gone, the two job
      moves go green, so it fails closed when its partner is deleted. That is the
      second rung in this guard with that shape, and it is recorded in the guard's
      header rather than left for the next reader to re-measure.
    * `on:` reduced to `workflow_dispatch` is caught by TWO rungs, not one:
      dropping `push` drops its `branches:` row with it, so the filter pin fires
      as well.

    The matrix is asserted whole. A per-rung "it fires" assertion would pass over
    an accounting that had quietly become wrong in the other direction.
    """
    cases: dict[str, tuple[str, str, str]] = {
        # name: (relative path, old, new)
        "job-if-false": (WORKFLOW, JOB_LINE, JOB_LINE + "    if: false\n"),
        "job-coe": (WORKFLOW, JOB_LINE, JOB_LINE + "    continue-on-error: true\n"),
        "dispatch-only": (WORKFLOW, ON_BLOCK, "on:\n  workflow_dispatch:\n"),
        "narrow-branches": (WORKFLOW, BRANCHES, '    branches:\n      - "main"\n'),
        "paths-filter": (WORKFLOW, BRANCHES, BRANCHES + '    paths:\n      - "x/**"\n'),
        "add-pull-request": (
            WORKFLOW,
            "  workflow_dispatch:\n",
            "  pull_request:\n  workflow_dispatch:\n",
        ),
        "rename-job": (WORKFLOW, JOB_LINE, "  gates:\n"),
    }
    #: 1 = still refused with that rung gone (another pin or the independent
    #: floor catches it), 0 = green again (that rung is the sole catcher).
    #: Measured, then pinned.
    expected = {
        "EXPECTED_TRIGGERS": {
            "job-if-false": 1,
            "job-coe": 1,
            "dispatch-only": 1,
            "narrow-branches": 1,
            "paths-filter": 1,
            "add-pull-request": 0,
            "rename-job": 1,
        },
        "EXPECTED_TRIGGER_FILTERS": {
            "job-if-false": 1,
            "job-coe": 1,
            "dispatch-only": 1,
            "narrow-branches": 0,
            "paths-filter": 1,
            "add-pull-request": 1,
            "rename-job": 1,
        },
        "EXPECTED_GATE_JOBS": {
            "job-if-false": 1,
            "job-coe": 1,
            "dispatch-only": 1,
            "narrow-branches": 1,
            "paths-filter": 1,
            "add-pull-request": 1,
            "rename-job": 1,
        },
        "EXPECTED_GATE_JOB_GUARDS": {
            "job-if-false": 1,
            "job-coe": 1,
            "dispatch-only": 1,
            "narrow-branches": 1,
            "paths-filter": 1,
            "add-pull-request": 1,
            "rename-job": 1,
        },
    }
    measured: dict[str, dict[str, int]] = {}
    base = tmp_path_factory.mktemp("falsify")
    for pin in expected:
        measured[pin] = {}
        for case, (relative, old, new) in cases.items():
            tree = _scratch_tree(base / f"{pin}-{case}")
            _healthy_activation(tree)
            _revert_rung(tree, pin)
            _mutate(tree, relative, old, new)
            measured[pin][case] = _run_guard(tree).returncode
    assert measured == expected, (
        f"the measured rung/fault accounting changed.\nmeasured: {measured}\n"
        f"pinned:   {expected}\n"
        f"A row that moved from 0 to 1 means another rung started catching that "
        f"fault, which makes the one named here less load-bearing than the "
        f"guard's header says. A row that moved from 1 to 0 means a rung stopped "
        f"catching something and the fault now rests on one place. Either way the "
        f"table in scripts/check-ci-reach.sh is the thing to fix, not this "
        f"assertion."
    )


def test_reverting_all_pins_and_the_floor_restores_every_pre_fix_green(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The four pins plus the floor ARE the new coverage for these attacks.

    With all five reverted, all seven attacks exit 0 on an otherwise complete
    guard. That reproduces the pre-fix verdict from the current tree rather than
    from a remembered measurement. The floor is reverted separately because its
    purpose is precisely to keep three attacks red after their matching pins are
    edited to agree with the weakened workflow.
    """
    base = tmp_path_factory.mktemp("revert-all")
    for case, (old, new) in {
        "job-if-false": (JOB_LINE, JOB_LINE + "    if: false\n"),
        "job-coe": (JOB_LINE, JOB_LINE + "    continue-on-error: true\n"),
        "dispatch-only": (ON_BLOCK, "on:\n  workflow_dispatch:\n"),
        "narrow-branches": (BRANCHES, '    branches:\n      - "main"\n'),
        "paths-filter": (BRANCHES, BRANCHES + '    paths:\n      - "x/**"\n'),
        "add-pull-request": (
            "  workflow_dispatch:\n",
            "  pull_request:\n  workflow_dispatch:\n",
        ),
        "rename-job": (JOB_LINE, "  gates:\n"),
    }.items():
        tree = _scratch_tree(base / case)
        _healthy_activation(tree)
        for pin in (
            "EXPECTED_TRIGGERS",
            "EXPECTED_TRIGGER_FILTERS",
            "EXPECTED_GATE_JOBS",
            "EXPECTED_GATE_JOB_GUARDS",
        ):
            _revert_rung(tree, pin)
        _revert_floor(tree)
        _mutate(tree, WORKFLOW, old, new)
        result = _run_guard(tree)
        assert result.returncode == 0, (
            f"with all four activation pins and the floor reverted, `{case}` is still "
            f"refused — so something else in this guard catches it and the "
            f"pre-fix measurement recorded in the header is wrong.\n"
            f"{result.stdout}{result.stderr}"
        )


# ------------------------------------------------------------- the scraper


def _scrape(tree: Path, function: str, *files: str) -> list[str]:
    """Run one of the guard's activation scrapers, straight out of the script."""
    result = subprocess.run(
        [
            "bash",
            "-c",
            "set -euo pipefail\n"
            f'eval "$(sed -n "/^{function}() {{/,/^}}$/p" "$1")"\n'
            f'{function} "${{@:2}}"\n',
            "_",
            str(tree / "scripts" / "check-ci-reach.sh"),
            *files,
        ],
        cwd=tree,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"{function} failed.\n{result.stderr}"
    return sorted(line for line in result.stdout.splitlines() if line.strip())


def test_the_activation_scraper_reads_the_shapes_it_claims(tree: Path) -> None:
    """The scraper is the haystack for all four pins, so its emptiness is fatal.

    A scraper that produced nothing for a shape it did not understand would
    report the whole pinned set as missing — which reads like a deleted `on:`
    block — and, if a pin were ever emptied, as agreement. So the three `on:`
    spellings it claims to read are exercised here, and the UNPARSED escape hatch
    is exercised too: an unrecognised shape has to announce itself rather than
    scrape to silence.

    The job scraper's separation is the other half. A step's own `if:` must NOT be
    reported as the job's — the step map already refuses that one, and a scraper
    that conflated them would make the job pin fire on a legitimate step
    condition.
    """
    shapes = tree / "shapes"
    shapes.mkdir()
    (shapes / "scalar.yml").write_text("on: push\njobs:\n  j:\n    runs-on: u\n")
    (shapes / "flow.yml").write_text(
        "on: [push, pull_request]\njobs:\n  j:\n    runs-on: u\n"
    )
    (shapes / "block.yml").write_text(
        '"on":\n  push:\n    branches: ["**"]\n    paths-ignore:\n      - docs/**\n'
        "  pull_request:\n    types: [opened, synchronize]\n"
        "  workflow_dispatch:\n    inputs:\n      level:\n        required: false\n"
        "permissions:\n  contents: read\n"
        "jobs:\n  j:\n    runs-on: u\n    if: false\n    continue-on-error: true\n"
        "    steps:\n      - name: s\n        if: always()\n        run: echo hi\n"
    )
    (shapes / "weird.yml").write_text("on:\n  - push\njobs:\n  j:\n    runs-on: u\n")

    assert _scrape(tree, "wf_activation", "shapes/scalar.yml") == [
        "trigger\tshapes/scalar.yml\tpush"
    ]
    assert _scrape(tree, "wf_activation", "shapes/flow.yml") == [
        "trigger\tshapes/flow.yml\tpull_request",
        "trigger\tshapes/flow.yml\tpush",
    ]
    assert _scrape(tree, "wf_activation", "shapes/block.yml") == [
        "filter\tshapes/block.yml\tpull_request\ttypes=opened,synchronize",
        "filter\tshapes/block.yml\tpush\tbranches=**",
        "filter\tshapes/block.yml\tpush\tpaths-ignore=docs/**",
        "filter\tshapes/block.yml\tworkflow_dispatch\tinputs=<mapping>",
        "trigger\tshapes/block.yml\tpull_request",
        "trigger\tshapes/block.yml\tpush",
        "trigger\tshapes/block.yml\tworkflow_dispatch",
    ], (
        "the block-mapping scraper changed. `branches:`, `paths-ignore:` and "
        "`types:` are all activation filters and all have to be pinnable; a "
        "nested mapping becomes `<mapping>` so it is pinned by hand rather than "
        "flattened into something that reads like a filter value."
    )
    weird = _scrape(tree, "wf_activation", "shapes/weird.yml")
    assert any(row.startswith("UNPARSED\t") for row in weird), (
        f"a sequence-valued `on:` scraped to silence instead of announcing "
        f"itself: {weird}. Silence here is the state where the pin compares "
        f"against nothing."
    )

    guards = _scrape(tree, "wf_job_guards", "shapes/block.yml")
    assert guards == [
        "shapes/block.yml\tj\tcontinue-on-error\ttrue",
        "shapes/block.yml\tj\tif\tfalse",
    ], (
        f"the job-guard scraper changed: {guards}. The step's own `if: always()` "
        f"must not appear here — it is the step map's business, and a scraper "
        f"that reported it as the job's would fire this pin on a legitimate step "
        f"condition."
    )
    # And the scraper finds the real workflow's real job guard, so its silence on
    # precommit.yml is a measurement rather than a blind spot.
    live = _scrape(tree, "wf_job_guards", WORKFLOW)
    assert live == [], (
        f"precommit.yml now carries a job-level guard: {live}. That is the "
        f"state EXPECTED_GATE_JOB_GUARDS pins, so the pin has to move with it."
    )


# ------------------------------------------------- what activation still leaves


def test_there_is_no_pull_request_trigger() -> None:
    """The finding, pinned as a fact rather than left in a commit message.

    precommit.yml runs on `push` with `branches: ["**"]` and on
    `workflow_dispatch`. It has NO `pull_request:` trigger, and it is the only
    counted workflow in this family without one — the other eight bindings are
    `push` + `pull_request`.

    WHAT PUSH DOES COVER. A same-repo pull request is gated: the push to its head
    branch is itself a run, and GitHub files that run's checks under the PR's head
    sha. Measured, not reasoned — all four merged PRs in this repo carry three
    green `precommit (3.1x)` checks each, and every `precommit.yml` run in the
    last hundred was a `push` event.

    WHAT IT DOES NOT. A FORK pull request: its push happens in the fork, so
    nothing runs here, and there is no branch protection demanding a check that
    would notice. Nor the `refs/pull/N/merge` commit that `pull_request` tests —
    so a PR green on its own head and broken against a moved base is caught only
    once the merge reaches main.

    WHY IT IS NOT FIXED HERE. Changing `on:` is a behaviour change, not a guard
    change, and this repo's risk today is latent: 0 forks, every PR so far
    same-repo and from the owner. So it is pinned and reported. If the trigger is
    added, this test and EXPECTED_TRIGGERS both fail, which is the reviewable form
    of that decision rather than a silent widening.
    """
    source = _workflow_text()
    on_block = source[source.index("on:") : source.index("jobs:")]
    assert "pull_request" not in on_block, (
        "precommit.yml now has a `pull_request:` trigger. That closes the gap "
        "this test reports — good — so update ON_BLOCK, EXPECTED_TRIGGERS, "
        "EXPECTED_TRIGGER_FILTERS and scripts/ci-reach.conf's paragraph about it "
        "in the same commit, and delete this assertion rather than inverting it."
    )
    assert 'branches:\n      - "**"' in on_block, (
        'the `branches: ["**"]` filter is gone, and it is the only reason a '
        "same-repo PR head is gated at all. Without it — and without a "
        "`pull_request:` trigger — a PR would be covered by nothing."
    )


def test_the_gate_job_matrix_legs_are_all_blocking() -> None:
    """The same defect one level down, and how far the pins reach into it.

    The gate job runs under a matrix of three interpreters. NO leg is advisory,
    because there is no job-level `continue-on-error` at all — so all three are
    blocking, and "the only blocking leg was removed" cannot happen here without
    a job-level `continue-on-error` appearing, which
    EXPECTED_GATE_JOB_GUARDS names in the absent -> present direction.

    What no pin in that guard sees is NARROWING the matrix — dropping 3.13 and
    3.14 loses two interpreters' worth of coverage without de-gating anything, so
    every reach verdict stays true. That is pinned here instead, as a literal, for
    the same reason the intra-step residual is: widening it should be a reviewable
    edit and not a discovery.
    """
    source = _workflow_text()
    assert source.count(MATRIX_LINE) == 1, (
        f"the gate job's matrix is not the pinned literal. Found "
        f"{source.count(MATRIX_LINE)} occurrence(s) of:\n{MATRIX_LINE}"
        f"Dropping a leg loses an interpreter from every gate in this repo while "
        f"leaving check-ci-reach green, because reach is a property of the steps "
        f"and not of the matrix."
    )
    assert "continue-on-error" not in source, (
        "precommit.yml now carries a `continue-on-error`, so one of the matrix "
        "legs may be advisory and the claim that all three are blocking is "
        "stale. EXPECTED_GATE_JOB_GUARDS pins the job-level value; a step-level "
        "one is refused by the gate-step pin."
    )


def test_the_clean_tree_verdict_gained_exactly_two_ok_lines(tree: Path) -> None:
    """Every existing magnitude unchanged, and the new rungs announce themselves.

    The activation pins are reported with the other set-equalities, after the
    reach verdicts, for the reason the vacuity floor moved last: a workflow that
    does not run does not make a single reach verdict wrong, and pre-empting them
    would hide which gate is in which step while telling the reader the workflow
    is dead. So the healthy output is the old one plus the pin and floor lines.
    """
    result = _healthy_activation(tree)
    assert result.stderr == "", result.stderr
    lines = result.stdout.splitlines()
    assert lines[-4:] == [
        "check-ci-reach: OK — 8 gate(s) reached INSIDE the CI step pinned for each",
        "check-ci-reach: OK — 8 target(s) reached by CI, 0 excused, 1 carrying no gate",
        "check-ci-reach: OK — 2 trigger(s), 1 trigger filter(s) and 1 gate job(s) "
        "pinned to exact values",
        "check-ci-reach: OK — .github/workflows/precommit.yml gates on push — "
        "unfiltered by path, every branch",
    ], (
        f"the healthy verdict changed. The first two lines are the magnitudes "
        f"every other ci-reach test is written against (8 reached, 0 excused, 1 "
        f"carrying no gate, 8 gates step-scoped); the last two are the activation "
        f"pins' and independent floor's own lines.\n{result.stdout}"
    )
