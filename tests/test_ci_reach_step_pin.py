"""WHICH CI step runs each gate, pinned (#reversereachdirection).

``scripts/check-ci-reach.sh`` used to ask whether *some* command in a flat set of
every ``run:`` body contained a target's anchors. "Some CI command anywhere" is a
weaker claim than it reads as, and it fails open two ways.

**The repoint.** Swap a target's recipe for a command CI already runs somewhere
and every other rung in that guard holds: membership is unchanged, the
make-derived oracle sees the new command on both sides, the classification is
still "carries a gate", and the anchor matches a real step. Measured on this
Makefile — ``type-check:`` running ``uv run poe run_readme``, the ``README
examples`` step, which no ``check`` member runs and whose own workflow comment
says so — the guard's output was **byte-identical** to the healthy run, ``reached
type-check`` included, at exit 0, while ``make -n check`` ran ``poe ty`` zero
times. The type gate was gone and the report said eight reached.

**The superset.** A narrow step's command can be a superset of a broad target's
anchor, so deleting the broad target's own step leaves the anchor findable in a
step that runs something else. lazily-rs has this live: 8 of its 46 members are
contained in another step's command and deleting its entire default-feature test
job passed byte-identically. In lazily-py it is not live, and that was measured
rather than reasoned: every member's anchor set lands in exactly one step, a
different one per member. The margin is thin — one token on six of the nine
anchors — so ``test_deleting_a_members_own_ci_step_is_refused`` is the test that
matters here. It asserts the *consequence* for every pinned gate, which is the
only form of this check that would have caught rs: an assertion over the mapping
alone is satisfied by a mapping whose members are all laundered by other steps.

What the pin does NOT close, stated rather than implied. The JOB and TRIGGER
levels underneath it are closed elsewhere — by the four activation pins
(#verifyworkflowactually), whose matrix and falsification live in
``tests/test_ci_reach_activation_pin.py``; this file keeps only the assertion
that the two levels stay SEPARATE, because a step-level ``if:`` is invisible to a
job pin and a job-level one is invisible to the step rung. What is still open
here is a recipe weakened INSIDE its own pinned step. Anchors match as subsequences and extra CI-side tokens are
allowed by design, so dropping ``--no-fix`` from ``lint``'s recipe still matches
the ``Lint (make lint)`` step. Only a per-recipe-content pin would close that,
and that one is declined — its churn is recipe-rate, which is how a guard becomes
the passes-when-stale check this family has already removed once.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest


if TYPE_CHECKING:
    from pathlib import Path

from test_ci_reach_closure_pin import _bash_array, _healthy, _make_n
from test_ci_reach_make_failure import CI_REACH_SCRIPT, _run_guard, _scratch_tree


WORKFLOW = ".github/workflows/precommit.yml"

#: The step-scoped lookup, verbatim. Reverting exactly this and nothing else is
#: how the tests below falsify the pin: if an attack still fails with this gone,
#: some other rung was doing the catching and the pin is decoration.
_SCOPED_LOOKUP = """			if [ -n "$target_step" ]; then
				anchor_reached_in_step "$a" "$target_step_wf" "$target_step_job" "$target_step_idx" && continue
			else
				anchor_reached "$a" && continue
			fi
"""
_FLAT_LOOKUP = """			anchor_reached "$a" && continue
"""

#: The victim for the repoint attacks. `type-check` is a one-line recipe
#: delegating to `poe ty`, so "did make run this gate" has a single unambiguous
#: string answer, and its anchor is the SHORTEST in the closure (two tokens) —
#: the member with the least margin against every other step.
_VICTIM = "type-check"
_VICTIM_RECIPE = "type-check:\n\tpoe ty\n"

#: A real step in the real workflow that no `check` member runs. Its own comment
#: in the workflow says so ("Not part of `make check`"), which is what makes it
#: the honest form of the repoint: the attacker does not have to invent a step.
_ORPHAN_STEP_COMMAND = "uv run poe run_readme"

#: The other half of the repoint: point one member at ANOTHER member's gate.
_SIBLING_GATE_COMMAND = "uv run poe interop_peer"


def _pin() -> dict[str, str]:
    """`EXPECTED_GATE_STEPS` as {target: step name}, straight from the guard."""
    entries = _bash_array("EXPECTED_GATE_STEPS")
    mapping: dict[str, str] = {}
    for entry in entries:
        stripped = entry.strip().strip('"')
        target, _, step = stripped.partition(" ")
        step = step.strip()
        assert target and step, (
            f"EXPECTED_GATE_STEPS entry {entry!r} is not `<target> <step name>`. "
            f"An entry these tests cannot parse is one they cannot exercise."
        )
        assert target not in mapping, (
            f"EXPECTED_GATE_STEPS names `{target}` twice, so one of the two pins "
            f"is dead and a reader cannot tell which."
        )
        mapping[target] = step
    assert mapping, "EXPECTED_GATE_STEPS is empty, so it pins no step at all."
    return mapping


PIN = _pin()


@pytest.fixture(name="tree")
def _tree(tmp_path: Path) -> Path:
    if shutil.which("make") is None:  # pragma: no cover - make is a hard dep here
        raise AssertionError(
            "no `make` on PATH, so this guard cannot be exercised at all. That is "
            "a broken environment, not a reason to skip."
        )
    return _scratch_tree(tmp_path)


def _write_changed(path: Path, new: str) -> None:
    """Write `new` to `path` and ERROR if the file did not actually change.

    lazily-dart's mutation harness had a quoting bug that silently skipped five
    mutations and reported them as byte-identical greens. A skipped mutation is
    indistinguishable from a survived one, which is this whole guard's subject,
    so every perturbation below goes through here: the file is hashed before and
    after, and an unchanged tree is an ERROR rather than a result. The
    count-exactly-once assertions each helper already makes say the pattern was
    FOUND; this says the write LANDED.
    """
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_text(new, encoding="utf-8")
    after = hashlib.sha256(path.read_bytes()).hexdigest()
    assert before != after, (
        f"{path.name} is byte-identical after the perturbation — the mutation "
        f"did not apply, so whatever the guard reports next is a measurement of "
        f"the healthy tree. This is an ERROR, not a result."
    )


def _workflow_lines(tree: Path) -> list[str]:
    return (tree / WORKFLOW).read_text(encoding="utf-8").splitlines(keepends=True)


def _step_bounds(tree: Path, name: str) -> tuple[int, int]:
    """Half-open line range of the `- name: <name>` step, its comments excluded.

    Asserted to match exactly once. A perturbation that silently matched nothing
    would leave the workflow healthy, and every assertion downstream would then
    be measuring the healthy run.
    """
    lines = _workflow_lines(tree)
    hits = [
        (i, len(m.group(1)))
        for i, line in enumerate(lines)
        if (m := re.match(r"^(\s*)- name: (.*)$", line.rstrip("\n")))
        and m.group(2) == name
    ]
    assert len(hits) == 1, (
        f"expected exactly one `- name: {name}` step in {WORKFLOW}; found "
        f"{len(hits)}. The perturbation has to land on the real step or this "
        f"test proves nothing."
    )
    start, marker = hits[0]
    end = len(lines)
    for j in range(start + 1, len(lines)):
        text = lines[j]
        if not text.strip() or text.lstrip().startswith("#"):
            continue
        if len(text) - len(text.lstrip(" ")) <= marker:
            end = j
            break
    return start, end


def _delete_step(tree: Path, name: str) -> str:
    lines = _workflow_lines(tree)
    start, end = _step_bounds(tree, name)
    removed = "".join(lines[start:end])
    assert "run:" in removed, (
        f"the block deleted for step {name!r} contains no `run:`, so nothing "
        f"about CI reach changed:\n{removed}"
    )
    _write_changed(tree / WORKFLOW, "".join(lines[:start] + lines[end:]))
    return removed


def _rename_step(tree: Path, name: str, new_name: str) -> None:
    path = tree / WORKFLOW
    source = path.read_text(encoding="utf-8")
    old = f"- name: {name}\n"
    assert source.count(old) == 1, (
        f"expected exactly one `{old.strip()}` line; found {source.count(old)}."
    )
    _write_changed(path, source.replace(old, f"- name: {new_name}\n", 1))


def _repoint(tree: Path, command: str) -> None:
    """Give `_VICTIM` a recipe that runs `command` instead of its own gate."""
    makefile = tree / "Makefile"
    source = makefile.read_text(encoding="utf-8")
    assert source.count(_VICTIM_RECIPE) == 1, (
        f"expected exactly one `{_VICTIM}` recipe of the form "
        f"{_VICTIM_RECIPE!r}; found {source.count(_VICTIM_RECIPE)}."
    )
    _write_changed(
        makefile, source.replace(_VICTIM_RECIPE, f"{_VICTIM}:\n\t{command}\n", 1)
    )


def _gut_recipe(tree: Path, target: str) -> None:
    """Replace one target's recipe with `true`, keeping its name and its edges.

    Only the recipe lines belonging to that rule, located from the rule line
    rather than by rewriting every tab-indented line in the file — this Makefile
    has tab-indented continuations that are not recipes at all, and a blanket
    substitution turns it into one make refuses to parse, which measures the
    readability class instead of this one.
    """
    makefile = tree / "Makefile"
    lines = makefile.read_text(encoding="utf-8").splitlines(keepends=True)
    hits = [i for i, line in enumerate(lines) if line.startswith(f"{target}:")]
    assert len(hits) == 1, (
        f"expected exactly one `{target}:` rule line; found {len(hits)}."
    )
    start = hits[0] + 1
    end = start
    while end < len(lines) and lines[end].startswith("\t"):
        end += 1
    assert end > start, (
        f"`{target}` has no recipe lines to empty, so gutting it changes nothing."
    )
    _write_changed(makefile, "".join([*lines[:start], "\ttrue\n", *lines[end:]]))


def _unscope(tree: Path) -> None:
    """Revert ONLY the step-scoped lookup, leaving every other rung in place."""
    path = tree / "scripts" / "check-ci-reach.sh"
    source = path.read_text(encoding="utf-8")
    assert source.count(_SCOPED_LOOKUP) == 1, (
        f"the step-scoped lookup is not in the guard verbatim "
        f"({source.count(_SCOPED_LOOKUP)} matches), so this test would revert "
        f"nothing and pass by measuring the fixed script twice."
    )
    _write_changed(path, source.replace(_SCOPED_LOOKUP, _FLAT_LOOKUP, 1))


def _edit_pin(tree: Path, old: str, new: str) -> None:
    path = tree / "scripts" / "check-ci-reach.sh"
    source = path.read_text(encoding="utf-8")
    assert source.count(old) == 1, (
        f"expected exactly one {old!r} in the guard; found {source.count(old)}."
    )
    _write_changed(path, source.replace(old, new, 1))


# --------------------------------------------------------------------------- A
#
# The superset shape. This is the test that matters: it asserts the CONSEQUENCE
# for every pinned gate, which is the only form that would have caught lazily-rs.


@pytest.mark.parametrize(("target", "step"), sorted(PIN.items()))
def test_deleting_a_members_own_ci_step_is_refused(
    tree: Path, target: str, step: str
) -> None:
    """Every gate has to lose its verdict when its OWN step goes.

    `anchor_reached` searches a flat set built from every `run:` body, so a
    member whose anchor happens to be a subsequence of a NARROWER step's command
    survives the deletion of the step that actually runs it. lazily-rs has 8 such
    members out of 46, and deleting its whole default-feature test job passed
    byte-identically.

    Parametrised from `EXPECTED_GATE_STEPS` rather than from a list written here,
    so a gate added to the pin joins this sweep without anyone remembering to add
    it. A mapping assertion cannot replace this: a mapping every one of whose
    members is laundered by some other step satisfies the mapping perfectly.
    """
    _healthy(tree)
    removed = _delete_step(tree, step)

    result = _run_guard(tree)
    combined = result.stdout + result.stderr

    assert result.returncode != 0, (
        f"deleting the `{step}` step — the only CI step that runs `{target}` — "
        f"left this guard green. Its anchors were found in some OTHER step's "
        f"command, so that gate can be dropped from CI without a word. This is "
        f"the shape lazily-rs has live.\ndeleted:\n{removed}\n{combined}"
    )
    assert target in combined, (
        f"the guard failed but never named `{target}`, so a reader cannot tell "
        f"which gate left CI.\n{combined}"
    )


# --------------------------------------------------------------------------- B
#
# The repoint, both halves, plus the falsification that the pin is what closes it.


@pytest.mark.parametrize(
    ("label", "command"),
    [
        ("a step no member runs", _ORPHAN_STEP_COMMAND),
        ("another member's gate", _SIBLING_GATE_COMMAND),
    ],
)
def test_a_recipe_repointed_at_another_step_is_refused(
    tree: Path, label: str, command: str
) -> None:
    """The gate's name stays, its recipe becomes a command CI runs elsewhere.

    Both halves are the same mechanism from the guard's point of view: the
    anchors are no longer in the step this gate is pinned to.
    """
    _healthy(tree)
    _repoint(tree, command)

    root = _make_n(tree, "check")
    assert root.returncode == 0, (
        f"`make -n check` FAILED, so this is the make-failure class.\n{root.stderr}"
    )
    assert "poe ty" not in root.stdout, (
        f"`make -n check` still runs `poe ty`, so the gate was not actually "
        f"repointed and this test is measuring a healthy tree.\n{root.stdout}"
    )
    assert command in root.stdout, (
        f"`make -n check` does not run `{command}`, so the repoint did not take.\n"
        f"{root.stdout}"
    )

    result = _run_guard(tree)
    combined = result.stdout + result.stderr
    assert result.returncode != 0, (
        f"`{_VICTIM}` repointed at {label} (`{command}`) passed. The gate is gone "
        f"from `make check` and the guard reported it reached.\n{combined}"
    )
    assert _VICTIM in combined and PIN[_VICTIM] in combined, (
        f"the failure names neither the member nor its pinned step, so a reader "
        f"cannot tell what moved.\n{combined}"
    )
    assert command in combined, (
        f"the failure does not name the anchor that is absent from the pinned "
        f"step, which is the one fact that identifies the swap.\n{combined}"
    )


def test_the_repoint_returns_when_only_the_step_scoping_is_reverted(
    tree: Path,
) -> None:
    """Falsification: without the scoped lookup, the attack passes again.

    Every other rung stays — the oracle, both membership directions, the
    classification pin, the pin's own declaration and resolution. If the attack
    still failed with only these five lines reverted, something else was doing
    the catching and the pin would be decoration.

    It also shows what the pin's mere PRESENCE is worth on its own, which is
    nothing: the reverted guard still prints "N gate(s) reached INSIDE the CI step
    pinned for each" while checking no such thing.
    """
    _healthy(tree)
    _unscope(tree)
    reverted_healthy = _run_guard(tree)
    assert reverted_healthy.returncode == 0, (
        f"reverting the scoped lookup reddened a HEALTHY tree, so this test "
        f"cannot distinguish the attack from the revert.\n"
        f"{reverted_healthy.stdout}{reverted_healthy.stderr}"
    )

    _repoint(tree, _ORPHAN_STEP_COMMAND)
    result = _run_guard(tree)
    assert result.returncode == 0, (
        f"the repoint was refused with the step scoping reverted, so some other "
        f"rung catches it and the scoping is not what closes this.\n"
        f"{result.stdout}{result.stderr}"
    )
    assert "reached  type-check" in result.stdout, (
        f"the reverted guard failed to reproduce the documented false green.\n"
        f"{result.stdout}"
    )


def test_step_scoping_reddens_nothing_on_a_healthy_tree(tree: Path) -> None:
    """Stricter must not mean redder here.

    Step-scoped reach is strictly stronger than the flat check, so it can red a
    state that is legitimate today — a member whose anchors are spread across two
    CI steps would be one. Measured instead of assumed: on a healthy tree the
    scoped and flat verdicts are identical apart from nothing at all.
    """
    scoped = _healthy(tree)
    _unscope(tree)
    flat = _run_guard(tree)
    assert (flat.returncode, flat.stdout, flat.stderr) == (
        scoped.returncode,
        scoped.stdout,
        scoped.stderr,
    ), (
        "step-scoping changed the verdict on a healthy tree, which means it "
        "reddens (or greens) something legitimate. Do NOT loosen the check to "
        "make this pass — either the mapping has to name every step a member's "
        "anchors are spread across, or the design does not fit this binding.\n"
        f"scoped:\n{scoped.stdout}{scoped.stderr}\nflat:\n{flat.stdout}{flat.stderr}"
    )


# --------------------------------------------------------------------------- C
#
# The pin has to resolve to exactly one step, and the mapping has to be total.


def test_a_renamed_step_is_refused_by_name(tree: Path) -> None:
    """A pin naming no step must say the STEP is gone, not that a command is.

    This is the one failure mode where the diagnostic is the whole value. With
    step-scoped reach, a pin whose step was renamed searches an empty haystack
    and fails anyway — but reporting it as a missing anchor sends the reader
    looking through recipes for a command that is right there.
    """
    _healthy(tree)
    _rename_step(tree, PIN[_VICTIM], "Typecheck")

    result = _run_guard(tree)
    combined = result.stdout + result.stderr
    assert result.returncode != 0, f"a pin naming no CI step passed.\n{combined}"
    assert "EXPECTED_GATE_STEPS" in result.stderr, (
        f"the guard failed by some route other than the step pin.\n{combined}"
    )
    assert PIN[_VICTIM] in result.stderr and "no run: step" in result.stderr, (
        f"the diagnostic does not say that the pinned step name matches nothing.\n"
        f"{combined}"
    )
    assert "move the pin with it" in result.stderr, (
        f"the diagnostic offers no remedy for a renamed step.\n{combined}"
    )


def test_a_duplicated_step_name_is_refused_rather_than_resolved(tree: Path) -> None:
    """Two steps with one name is the flat check again, wearing a pin.

    Step names are not unique family-wide (lazily-rs: 69 `run:` steps, 65
    distinct names), so a bare name can resolve to two steps — and if the guard
    unions them, a member repointed at the twin passes. That was a live defect in
    the first cut of this pin, found by measurement: resolution keyed on
    (workflow, job) collapsed two same-named steps in one job into a single
    haystack, and the repoint below exited 0. The key carries a step ORDINAL now.
    """
    _healthy(tree)
    path = tree / WORKFLOW
    source = path.read_text(encoding="utf-8")
    orphan = f"      - name: README examples\n        run: {_ORPHAN_STEP_COMMAND}\n"
    assert source.count(orphan) == 1, (
        f"expected exactly one `README examples` step to rename; found "
        f"{source.count(orphan)}."
    )
    _write_changed(
        path,
        source.replace(
            orphan,
            f"      - name: {PIN[_VICTIM]}\n        run: {_ORPHAN_STEP_COMMAND}\n",
            1,
        ),
    )
    # And repoint the victim INTO the twin, so a guard that unions the two would
    # not merely pass — it would pass while the gate is gone.
    _repoint(tree, _ORPHAN_STEP_COMMAND)

    result = _run_guard(tree)
    combined = result.stdout + result.stderr
    assert result.returncode != 0, (
        f"two CI steps share the pinned name, the victim's recipe was repointed "
        f"at the second one, and the guard passed — the pin resolved to a union "
        f"of both steps, which is the flat check with extra steps.\n{combined}"
    )
    assert "occurs in 2 places" in result.stderr, (
        f"the guard failed by some route other than the ambiguity refusal, so "
        f"the ambiguity itself may still be unhandled.\n{combined}"
    )
    assert "will not pick one" in result.stderr, (
        f"the diagnostic does not say the guard refuses to choose.\n{combined}"
    )


def test_an_unpinned_gate_is_refused(tree: Path) -> None:
    """A gate with no pin falls back to the flat check, so it must not be silent.

    The fallback is deliberate — it keeps an unpinned target's failure to ONE
    finding, the missing pin, instead of a MISSING verdict whose named cause is
    wrong. It is only safe because this direction is a hard failure.
    """
    _healthy(tree)
    entries = _bash_array("EXPECTED_GATE_STEPS")
    victim_entry = next(e for e in entries if e.strip('"').startswith(_VICTIM + " "))
    _edit_pin(tree, f"\t{victim_entry}\n", "")

    result = _run_guard(tree)
    combined = result.stdout + result.stderr
    assert result.returncode != 0, (
        f"`{_VICTIM}` carries a gate and is pinned to no CI step, and the guard "
        f"passed — its anchors are back to matching any run: body anywhere.\n"
        f"{combined}"
    )
    assert "EXPECTED_GATE_STEPS does not pin" in result.stderr, (
        f"the guard failed by some other route.\n{combined}"
    )
    assert _VICTIM in result.stderr, (
        f"the finding does not name the unpinned gate.\n{combined}"
    )


def test_a_pin_for_a_target_outside_the_closure_is_refused(tree: Path) -> None:
    """The mirror of the stray excuse: a pin nobody consults still reads as one."""
    _healthy(tree)
    entries = _bash_array("EXPECTED_GATE_STEPS")
    victim_entry = next(e for e in entries if e.strip('"').startswith(_VICTIM + " "))
    _edit_pin(
        tree,
        f"\t{victim_entry}\n",
        f'\t{victim_entry}\n\t"zzz-not-a-target          Install dependencies"\n',
    )

    result = _run_guard(tree)
    combined = result.stdout + result.stderr
    assert result.returncode != 0, (
        f"a pin for a target that is not in `make check`'s closure passed, so "
        f"this mapping can name gates that were never gated.\n{combined}"
    )
    assert "zzz-not-a-target" in result.stderr, (
        f"the finding does not name the stray pin.\n{combined}"
    )


# --------------------------------------------------------------------------- D
#
# The pin as source text, and the mechanism accounting it is allowed to claim.


def test_the_pin_is_non_empty_canonical_and_total() -> None:
    """Source-level, because an absent pin cannot fail behaviourally.

    A deleted `EXPECTED_GATE_STEPS` takes its own diagnostics with it and the
    guard's reach check silently reverts to "some CI command anywhere" — which is
    exactly the state the repoint attack passes in.
    """
    source = CI_REACH_SCRIPT.read_text(encoding="utf-8")
    assert "EXPECTED_GATE_STEPS=(" in source, (
        f"no `EXPECTED_GATE_STEPS=( ... )` array in {CI_REACH_SCRIPT}; reach is "
        f"unpinned to any step again."
    )
    assert "anchor_reached_in_step" in source, (
        f"`anchor_reached_in_step` is gone from {CI_REACH_SCRIPT}, so the pin is "
        f"declared and never consulted — the array would be documentation."
    )

    targets = list(PIN)
    assert targets == sorted(targets), (
        f"EXPECTED_GATE_STEPS is not sorted by target: {targets}. Sorted order is "
        f"what makes a one-line diff of this pin readable."
    )

    closure = set(_bash_array("EXPECTED_CLOSURE_TARGETS"))
    no_gate = set(_bash_array("EXPECTED_NO_GATE_TARGETS"))
    assert set(targets) == closure - no_gate, (
        f"EXPECTED_GATE_STEPS does not cover every gate-carrying target in the "
        f"pinned closure. pinned steps: {sorted(targets)}; closure minus "
        f"no-gate: {sorted(closure - no_gate)}. A gate missing from this mapping "
        f"is matched against every run: body in CI instead of one step."
    )


def test_the_vacuity_floor_is_restated_by_both_pins_and_still_fails_closed(
    tree: Path,
) -> None:
    """The accounting, as a test, so the claim in the header cannot rot.

    Four mechanisms overlap in this area. The floor ("nothing was verified") is
    now restated twice: an all-recipes-gutted Makefile is caught by the
    classification pin AND by the gate-step domain check, each naming all eight
    targets. The floor's only remaining property is that it survives both of them
    being deleted, and that is what is asserted here — together with the reason it
    now runs LAST, which is that it was pre-empting the two diagnostics that say
    which gates went.
    """
    _healthy(tree)
    for target in PIN:
        _gut_recipe(tree, target)

    result = _run_guard(tree)
    combined = result.stdout + result.stderr
    assert result.returncode != 0, f"an all-gutted Makefile passed.\n{combined}"

    assert "EXPECTED_NO_GATE_TARGETS" in result.stderr, (
        f"the classification pin did not fire on an all-gutted Makefile.\n{combined}"
    )
    assert "EXPECTED_GATE_STEPS" in result.stderr, (
        f"the gate-step domain check did not fire on an all-gutted Makefile, so "
        f"the floor is not restated by it after all.\n{combined}"
    )
    assert "nothing was verified" in result.stderr, (
        f"the vacuity floor did not fire.\n{combined}"
    )

    ordering = result.stderr
    assert ordering.index("EXPECTED_GATE_STEPS") < ordering.index(
        "nothing was verified"
    ), (
        f"the vacuity floor printed BEFORE the named findings, which is the "
        f"ordering that made it pre-empt them.\n{combined}"
    )
    for target in PIN:
        assert target in result.stderr, (
            f"`{target}` lost its gate and was not named anywhere.\n{combined}"
        )


def test_ci_invokes_make_zero_times_so_every_gate_is_anchor_reached() -> None:
    """The premise of the whole mapping, asserted rather than remembered.

    This design is meaningless where CI's instruction is `make <target>`: there is
    no independent CI-side spelling to cross-check, so a step name pinned for
    such a target asserts nothing (lazily-gd is excluded for exactly that reason).
    Here CI spells every gate out, and the count is ZERO — measured from `run:`
    bodies, not from workflow text. A naive grep for `make` in this workflow
    reports 16 hits: eight in comments, seven in step NAMES, and one inside an
    `echo "::error::..."` string. That is the exact trap this guard's own header
    documents, and it is why the assertion below reads the scraped command stream.
    """
    scrape = subprocess.run(
        [
            "bash",
            "-c",
            "set -euo pipefail\n"
            'source_lines=$(sed -n "/^ci_step_commands() {/,/^}$/p" "$1")\n'
            'eval "$source_lines"\n'
            'ci_step_commands "$2" | cut -f5-\n',
            "_",
            str(CI_REACH_SCRIPT),
            WORKFLOW,
        ],
        cwd=CI_REACH_SCRIPT.parents[1],
        capture_output=True,
        text=True,
    )
    assert scrape.returncode == 0, (
        f"could not scrape the workflow with the guard's own scraper, so this "
        f"test would assert nothing.\n{scrape.stderr}"
    )
    commands = [c for c in scrape.stdout.splitlines() if c.strip()]
    assert commands, "the scraper returned no commands at all."

    invocations = [c for c in commands if re.match(r"^\s*(\S+=\S+\s+)*make\b", c)]
    assert not invocations, (
        f"CI now invokes make directly: {invocations}. A target reached that way "
        f"has no independent CI-side spelling, so EXPECTED_GATE_STEPS must refuse "
        f"a pin for it — check that the refusal fires and re-read the "
        f"applicability argument in the guard's header before pinning it anyway."
    )


# --------------------------------------------------------------------------- E
#
# Findings ported in from siblings mid-session. Each one is a shape that passed
# at exit 0 in the binding that found it, and each is measured here rather than
# argued away.


def _consolidate_the_two_ruff_steps(tree: Path) -> None:
    """Merge `Format` and `Lint` into one step and pin BOTH members to it.

    A plausible tidy-up, which is what makes it the interesting case: nothing
    about it looks like an attack.
    """
    path = tree / WORKFLOW
    source = path.read_text(encoding="utf-8")
    fmt = (
        "      - name: Format (make format)\n"
        "        run: uv run ruff format --check src/lazily/ tests/\n"
    )
    lint = (
        "      - name: Lint (make lint)\n"
        "        run: uv run ruff check --no-fix src/lazily/ tests/\n"
    )
    assert source.count(fmt) == 1 and source.count(lint) == 1, (
        f"expected one `Format` step and one `Lint` step; found "
        f"{source.count(fmt)} and {source.count(lint)}."
    )
    merged = (
        "      - name: Ruff (format + lint)\n"
        "        run: |\n"
        "          uv run ruff format --check src/lazily/ tests/\n"
        "          uv run ruff check --no-fix src/lazily/ tests/\n"
    )
    _write_changed(path, source.replace(fmt, merged, 1).replace(lint, "", 1))
    for target in ("format-check", "lint"):
        entry = next(
            e
            for e in _bash_array("EXPECTED_GATE_STEPS")
            if e.strip('"').startswith(target + " ")
        )
        _edit_pin(tree, f"\t{entry}\n", f'\t"{target}  Ruff (format + lint)"\n')


def test_a_step_pinned_for_one_gate_that_runs_another_is_refused(tree: Path) -> None:
    """Step scoping cannot see a repoint BETWEEN two gates sharing a step.

    lazily-dart demonstrated this as the collision rung's own case, and it
    reproduces here: consolidate the two ruff steps, pin both members to the
    merged one, repoint `format-check:` at lint's command, and every anchor is
    inside its own pinned step while `make -n check` runs `ruff format --check`
    zero times. Measured at exit 0 with only the step map in place.

    So the two are two PROPERTIES, not two spellings of one, and the refusal
    lands on the CONSOLIDATION rather than waiting for the repoint — the state in
    which the attack becomes invisible is the state that gets refused.
    """
    _healthy(tree)
    _consolidate_the_two_ruff_steps(tree)

    result = _run_guard(tree)
    combined = result.stdout + result.stderr
    assert result.returncode != 0, (
        f"two gates pinned to one CI step passed, so either recipe can become "
        f"the other's command with every anchor still inside its own pinned "
        f"step.\n{combined}"
    )
    assert "also run ANOTHER gate's" in result.stderr, (
        f"the guard failed by some route other than the collision rung.\n{combined}"
    )
    assert "format-check" in result.stderr and "lint" in result.stderr, (
        f"the finding does not name both colliding gates.\n{combined}"
    )
    assert "Split the step" in result.stderr, (
        f"the diagnostic offers no remedy, and the remedy is the whole cost of "
        f"this rung.\n{combined}"
    )


def test_the_collision_case_is_invisible_to_step_scoping_alone(tree: Path) -> None:
    """Falsification: the collision rung is not a second spelling of the map.

    Remove ONLY the collision rung, leave the step map and every other rung, and
    the consolidation-plus-repoint passes at exit 0 while the format gate runs
    zero times. If it still failed, the rung would be redundant and should go.
    """
    _healthy(tree)
    path = tree / "scripts" / "check-ci-reach.sh"
    source = path.read_text(encoding="utf-8")
    start = source.index('gs_collisions=""')
    end = source.index("# ACTIVATION: does the workflow run", start)
    assert start < end, "the collision rung is not where this test expects it."
    _write_changed(path, source[:start] + source[end:])
    assert "also run ANOTHER gate" not in path.read_text(encoding="utf-8"), (
        "the collision rung survived the excision, so this test would measure "
        "the fixed guard twice."
    )

    _consolidate_the_two_ruff_steps(tree)
    _repoint_format_check_at_lint(tree)

    result = _run_guard(tree)
    assert result.returncode == 0, (
        f"the consolidation-plus-repoint was refused with the collision rung "
        f"removed, so something else catches it and the rung is redundant.\n"
        f"{result.stdout}{result.stderr}"
    )
    root = _make_n(tree, "check")
    assert "ruff format" not in root.stdout, (
        f"`make -n check` still runs `ruff format`, so the gate was not actually "
        f"lost and this test proves nothing.\n{root.stdout}"
    )


def _repoint_format_check_at_lint(tree: Path) -> None:
    makefile = tree / "Makefile"
    source = makefile.read_text(encoding="utf-8")
    old = "format-check:\n\tuv run ruff format --check src/lazily/ tests/\n"
    assert source.count(old) == 1, (
        f"expected exactly one `format-check` recipe; found {source.count(old)}."
    )
    _write_changed(
        makefile,
        source.replace(
            old, "format-check:\n\tuv run ruff check --no-fix src/lazily/ tests/\n", 1
        ),
    )


def test_an_unnamed_run_step_in_a_listed_workflow_is_refused(tree: Path) -> None:
    """A step with no name is one no pin can name, so it is refused up front.

    lazily-zig's scraper let an unnamed step INHERIT the previous step's name,
    crediting its command to a step that did not run it — worse than no name.
    lazily-dart synthesised a `<unnamed@file:line>` placeholder, pinned it, and
    it resolved and passed. This refusal is what leaves neither anything to
    attach to.
    """
    _healthy(tree)
    path = tree / WORKFLOW
    source = path.read_text(encoding="utf-8")
    old = f"      - name: README examples\n        run: {_ORPHAN_STEP_COMMAND}\n"
    assert source.count(old) == 1, (
        f"expected one `README examples` step to strip the name from; found "
        f"{source.count(old)}."
    )
    _write_changed(
        path, source.replace(old, f"      - run: {_ORPHAN_STEP_COMMAND}\n", 1)
    )

    result = _run_guard(tree)
    combined = result.stdout + result.stderr
    assert result.returncode != 0, (
        f"an unnamed run: step in a listed workflow passed.\n{combined}"
    )
    assert "with no name" in result.stderr, (
        f"the guard failed by some route other than the unnamed-step refusal.\n"
        f"{combined}"
    )
    assert "Add a name:" in result.stderr, (
        f"the diagnostic offers no remedy.\n{combined}"
    )


def test_an_unnamed_step_does_not_inherit_the_previous_steps_name(tree: Path) -> None:
    """The scraper property the refusal above rests on, asserted directly.

    The refusal makes an unnamed step a hard failure, so this is the assertion
    that the FALLBACK it protects against does not exist either — a future
    loosening of the refusal must not land on a scraper that silently attributes
    the command to the step above it.
    """
    path = tree / WORKFLOW
    source = path.read_text(encoding="utf-8")
    named = f"      - name: {PIN[_VICTIM]}\n        run: uv run poe ty\n"
    assert source.count(named) == 1, (
        f"expected one `{PIN[_VICTIM]}` step; found {source.count(named)}."
    )
    _write_changed(
        path, source.replace(named, named + f"      - run: {_ORPHAN_STEP_COMMAND}\n", 1)
    )

    scrape = subprocess.run(
        [
            "bash",
            "-c",
            "set -euo pipefail\n"
            'eval "$(sed -n "/^ci_step_commands() {/,/^}$/p" "$1")"\n'
            'ci_step_commands "$2"\n',
            "_",
            str(tree / "scripts" / "check-ci-reach.sh"),
            WORKFLOW,
        ],
        cwd=tree,
        capture_output=True,
        text=True,
    )
    assert scrape.returncode == 0, (
        f"could not run the guard's own scraper.\n{scrape.stderr}"
    )
    rows = [line.split("\t") for line in scrape.stdout.splitlines() if line]
    # Fields: workflow, job, step ordinal, step NAME, step guards, command.
    orphans = [r for r in rows if r[5].strip() == _ORPHAN_STEP_COMMAND]
    assert len(orphans) == 2, (
        f"expected the inserted unnamed step plus the real `README examples` "
        f"step to both run `{_ORPHAN_STEP_COMMAND}`; found {len(orphans)}."
    )
    inserted = min(orphans, key=lambda r: int(r[2]))
    assert inserted[3] == "", (
        f"the unnamed step was labelled {inserted[3]!r} instead of empty — it "
        f"inherited a name, which credits its command to a step that did not run "
        f"it. This is lazily-zig's finding."
    )


def test_no_ci_step_anchor_ends_in_a_wildcard(tree: Path) -> None:
    """A tripwire, not a rung: this binding has no wildcards to absorb anything.

    An unresolvable variable reference becomes an ANY token, which matches
    exactly one token on the other side. When ANY lands at the END of a step's
    anchor, that step is a superset of every anchor that is a subsequence of its
    prefix plus one free token — lazily-zig measured 4 of its 7 gates deletable
    from CI with a byte-identical OK at exit 0 that way, including the interop
    peer and the reachability guard's own step.

    Here there are ZERO wildcards on either side, and that is why the deletion
    sweep above is 8 for 8 rather than lucky. py's only interpolation in a run
    body is `${{ matrix.python-version }}`, which the normalizer reduces to the
    literal token `matrix.python-version`, and `LAZILY_CONFORMANCE_RUN_ID` is
    delivered through `env:` / `export` and never spelled in a command.

    A trailing wildcard is not itself illegitimate, so this is a tripwire rather
    than a refusal: step-scoping already confines the absorption to the ONE
    member pinned to that step, which makes it a special case of the open hole
    this pin does not close (a recipe weakened inside its own pinned step). If
    this test ever fires, re-read that bound before assuming the gate is covered.
    """
    dump = subprocess.run(
        [
            "bash",
            "-c",
            "set -euo pipefail\n"
            'eval "$(sed -n "/^ci_step_commands() {/,/^}$/p" "$1")"\n'
            'eval "$(sed -n "/^anchors() {/,/^}$/p" "$1")"\n'
            'ci_step_commands "$2" | cut -f5- | anchors\n',
            "_",
            str(tree / "scripts" / "check-ci-reach.sh"),
            WORKFLOW,
        ],
        cwd=tree,
        capture_output=True,
        text=True,
    )
    assert dump.returncode == 0, (
        f"could not scrape and normalize the workflow.\n{dump.stderr}"
    )
    ci_anchors = [a for a in dump.stdout.splitlines() if a.strip()]
    assert ci_anchors, "the normalizer produced no anchors, so this asserts nothing."

    any_token = "\001any"
    trailing = [a for a in ci_anchors if a.split(" ")[-1] == any_token]
    assert not trailing, (
        f"CI step anchor(s) now END in a wildcard: {trailing}. Such a step "
        f"absorbs any anchor that is a subsequence of its prefix plus one token, "
        f"for the member pinned to it. Check that member's gate is really run "
        f"there before trusting its verdict."
    )


def test_the_reach_mode_is_pinned_by_the_domain_and_needs_no_second_set(
    tree: Path,
) -> None:
    """The reach MODE is already pinned, so a separate mode set would be empty.

    lazily-zig added `EXPECTED_MAKE_INVOKED_TARGETS` after finding that deleting
    a make-invoked member's CI step made `make_invokes` fail and the member fell
    through to the flat check, where a wildcard absorbed it. Here the gate-step
    DOMAIN is defined as {carries a gate} minus {excused} minus {make-invoked}
    and pinned set-equal to `EXPECTED_GATE_STEPS`, so both mode transitions are
    already hard failures — an explicit make-invoked set would pin the complement
    of an already-pinned set, and in this binding it would be EMPTY, which is the
    shape this guard refuses everywhere else.

    Both directions are measured, because "already covered" is exactly the claim
    that is worth nothing unasserted.
    """
    _healthy(tree)
    path = tree / WORKFLOW
    source = path.read_text(encoding="utf-8")
    old = f"      - name: {PIN[_VICTIM]}\n        run: uv run poe ty\n"
    assert source.count(old) == 1, (
        f"expected one `{PIN[_VICTIM]}` step; found {source.count(old)}."
    )
    _write_changed(
        path,
        source.replace(
            old, f"      - name: {PIN[_VICTIM]}\n        run: make {_VICTIM}\n", 1
        ),
    )

    kept = _run_guard(tree)
    assert kept.returncode != 0, (
        f"`{_VICTIM}` flipped to make-invocation reach with its step pin kept, "
        f"and the guard passed — the pin now asserts nothing and says so to "
        f"nobody.\n{kept.stdout}{kept.stderr}"
    )
    assert "by running `make" in kept.stderr, (
        f"the guard failed by some route other than the make-invocation "
        f"refusal.\n{kept.stdout}{kept.stderr}"
    )

    # Now do what that diagnostic asks — drop the pin — and confirm the other
    # direction: deleting the step puts the member back in the domain, unpinned.
    entry = next(
        e
        for e in _bash_array("EXPECTED_GATE_STEPS")
        if e.strip('"').startswith(_VICTIM + " ")
    )
    _edit_pin(tree, f"\t{entry}\n", "")
    dropped = _run_guard(tree)
    assert dropped.returncode == 0, (
        f"a make-invoked member with no step pin was refused, so the remedy the "
        f"guard prints does not work.\n{dropped.stdout}{dropped.stderr}"
    )

    _delete_step(tree, PIN[_VICTIM])
    deleted = _run_guard(tree)
    assert deleted.returncode != 0, (
        f"the make-invoked member's own CI step was deleted, `make_invokes` no "
        f"longer holds, and the guard passed — the member fell through to the "
        f"flat check unpinned. This is lazily-zig's finding.\n"
        f"{deleted.stdout}{deleted.stderr}"
    )
    assert "does not pin" in deleted.stderr and _VICTIM in deleted.stderr, (
        f"the finding does not name the now-unpinned gate.\n"
        f"{deleted.stdout}{deleted.stderr}"
    )


def test_a_conditional_or_failure_discarding_pinned_step_is_refused(
    tree: Path,
) -> None:
    """Reach is not enforcement, and a step that may not run is not reach.

    lazily-js found the level this sits at: a pinned step can exist, be unique,
    be unconditional and run the gate, in a JOB or a WORKFLOW that never runs.
    The STEP-level halves are closable only because the step map gave the guard a
    handle on the step at all, so they are closed here. The job and trigger
    levels are NOT — see `test_the_job_and_trigger_levels_are_not_protected`.
    """
    _healthy(tree)
    path = tree / WORKFLOW
    original = path.read_text(encoding="utf-8")
    named = f"      - name: {PIN[_VICTIM]}\n        run: uv run poe ty\n"
    assert original.count(named) == 1, (
        f"expected one `{PIN[_VICTIM]}` step; found {original.count(named)}."
    )

    for attribute, word in (
        ("if: false", "if"),
        ("continue-on-error: true", "continue-on-error"),
    ):
        injected = (
            f"      - name: {PIN[_VICTIM]}\n"
            f"        {attribute}\n"
            f"        run: uv run poe ty\n"
        )
        _write_changed(path, original.replace(named, injected, 1))
        result = _run_guard(tree)
        combined = result.stdout + result.stderr
        assert result.returncode != 0, (
            f"the pinned step carries `{attribute}` and the guard reported the "
            f"gate reached. A step that may not run, or whose failure is "
            f"discarded, is reach without enforcement.\n{combined}"
        )
        assert word in result.stderr and PIN[_VICTIM] in result.stderr, (
            f"the finding names neither `{word}` nor the step.\n{combined}"
        )
        assert "excuse the target" in result.stderr, (
            f"the diagnostic does not offer the reviewable remedy — losing the "
            f"pin and taking an excuse.\n{combined}"
        )

    # `continue-on-error: false` is the default and must NOT red: a rung that
    # refuses the default state is one someone will delete rather than satisfy.
    _write_changed(
        path,
        original.replace(
            named,
            f"      - name: {PIN[_VICTIM]}\n"
            f"        continue-on-error: false\n"
            f"        run: uv run poe ty\n",
            1,
        ),
    )
    benign = _run_guard(tree)
    assert benign.returncode == 0, (
        f"`continue-on-error: false` — the DEFAULT — was refused, so this rung "
        f"reddens a legitimate state.\n{benign.stdout}{benign.stderr}"
    )


def test_the_job_and_trigger_levels_are_closed_but_not_by_this_rung(
    tree: Path,
) -> None:
    """The residual js named is CLOSED, and this file is not what closes it.

    This test used to assert the hole was OPEN — deliberately, so the gap was a
    measured fact with a name — and it said that closing the hole later should
    fail here loudly rather than leave a test that quietly asserted it. That is
    what happened: the four activation pins
    (``EXPECTED_TRIGGERS``/``_TRIGGER_FILTERS``/``_GATE_JOBS``/``_GATE_JOB_GUARDS``,
    #verifyworkflowactually) now refuse all three states, and
    ``tests/test_ci_reach_activation_pin.py`` owns the full matrix and the
    per-rung falsification.

    What remains here is the SEPARATION, which is this file's business. The
    step-level rung and the job-level pins are not substitutes: a step's own
    ``if:`` is invisible to a job pin, and a job-level one is invisible to the
    step rung. So each of the three states is asserted to be refused *by the
    activation pins* and NOT by the step-guard rung — if the step rung ever
    started catching them, the two would be conflated and a reader chasing a
    failure would be sent to the wrong half of the guard.
    """
    _healthy(tree)
    path = tree / WORKFLOW
    original = path.read_text(encoding="utf-8")
    job_line = "  precommit:\n"
    assert original.count(job_line) == 1, (
        f"expected one `{job_line.strip()}` job key; found {original.count(job_line)}."
    )

    cases: list[tuple[str, str, str]] = [
        (
            "job-level if: false",
            original.replace(job_line, f"{job_line}    if: false\n", 1),
            "EXPECTED_GATE_JOB_GUARDS",
        ),
        (
            "job-level continue-on-error: true",
            original.replace(job_line, f"{job_line}    continue-on-error: true\n", 1),
            "EXPECTED_GATE_JOB_GUARDS",
        ),
    ]
    trigger = 'on:\n  push:\n    branches:\n      - "**"\n  workflow_dispatch:\n'
    assert original.count(trigger) == 1, (
        f"expected one `on:` block of the pinned shape; found "
        f"{original.count(trigger)}."
    )
    cases.append(
        (
            "`on:` reduced to workflow_dispatch",
            original.replace(trigger, "on:\n  workflow_dispatch:\n", 1),
            "EXPECTED_TRIGGERS",
        )
    )

    for label, mutated, pin in cases:
        _write_changed(path, mutated)
        result = _run_guard(tree)
        assert result.returncode == 1, (
            f"{label} was ACCEPTED. The activation pins are supposed to refuse "
            f"it, and this file's WHAT IT DOES NOT PROVE section no longer lists "
            f"the job and trigger levels as open.\n{result.stdout}{result.stderr}"
        )
        assert pin in result.stderr, (
            f"{label} was refused, but `{pin}` is not in the diagnostic — so "
            f"something else caught it and the accounting in "
            f"scripts/check-ci-reach.sh names the wrong rung.\n{result.stderr}"
        )
        assert "the CI step pinned for" not in result.stderr, (
            f"{label} was refused by the STEP-guard rung, which conflates two "
            f"levels that must stay separate: a step-level `if:` is invisible to "
            f"a job pin and a job-level one is invisible to the step rung. A "
            f"reader chasing this failure would be sent to the wrong half of the "
            f"guard.\n{result.stderr}"
        )
        path.write_text(original, encoding="utf-8")


def test_the_intra_step_residual_is_one_step_wide(tree: Path) -> None:
    """How wide the "repoint inside your own pinned step" residual is: 1 of 8.

    Scoping narrows the haystack to one step, and a step carrying more than ONE
    anchor still offers a choice inside itself. Measured: `Test (make test)` is
    the only pinned step with two anchors (`uv run poe conformance_manifest` and
    `uv run poe test`), and the residual is live there — drop `uv run poe test`
    from `test:`'s recipe and the guard prints `reached test` at exit 0 while
    `make check` runs the suite, and conformance rungs 2-4 with it, zero times.

    The number is pinned so that widening it is a reviewable edit. Splitting that
    step in two would close this instance; it is not done, because the general
    form needs the declined per-recipe-content pin and a one-off split would buy
    one member's worth of it while reading as the whole thing.
    """
    _healthy(tree)
    dump = subprocess.run(
        [
            "bash",
            "-c",
            "set -euo pipefail\n"
            'eval "$(sed -n "/^ci_step_commands() {/,/^}$/p" "$1")"\n'
            'eval "$(sed -n "/^anchors() {/,/^}$/p" "$1")"\n'
            'ci_step_commands "$2" | while IFS= read -r row; do\n'
            '  key=$(printf %s "$row" | cut -f4)\n'
            '  printf %s "$row" | cut -f6- | anchors | sed "s|^|$key\\t|"\n'
            "done\n",
            "_",
            str(tree / "scripts" / "check-ci-reach.sh"),
            WORKFLOW,
        ],
        cwd=tree,
        capture_output=True,
        text=True,
    )
    assert dump.returncode == 0, f"could not scrape per-step anchors.\n{dump.stderr}"
    per_step: dict[str, set[str]] = {}
    for row in dump.stdout.splitlines():
        if not row.strip():
            continue
        step, _, anchor = row.partition("\t")
        per_step.setdefault(step, set()).add(anchor)
    assert per_step, "no per-step anchors were produced, so this asserts nothing."

    pinned_multi = {
        step: sorted(anchors)
        for step, anchors in per_step.items()
        if step in set(PIN.values()) and len(anchors) > 1
    }
    assert list(pinned_multi) == ["Test (make test)"], (
        f"the set of PINNED CI steps carrying more than one anchor changed: "
        f"{pinned_multi}. Each one is a step inside which a recipe can be "
        f"repointed at a sibling command and stay green, so this number is the "
        f"width of the residual. If a step gained a second anchor, either split "
        f"it or widen this assertion deliberately."
    )
    assert len(pinned_multi["Test (make test)"]) == 2, (
        f"`Test (make test)` now carries "
        f"{len(pinned_multi['Test (make test)'])} anchors, not 2."
    )

    # And the residual is live, not theoretical.
    makefile = tree / "Makefile"
    source = makefile.read_text(encoding="utf-8")
    recipe = "test:\n\tuv run poe conformance_manifest\n\tuv run poe test\n"
    assert source.count(recipe) == 1, (
        f"expected one `test:` recipe of the pinned shape; found "
        f"{source.count(recipe)}."
    )
    _write_changed(
        makefile,
        source.replace(recipe, "test:\n\tuv run poe conformance_manifest\n", 1),
    )
    root = _make_n(tree, "check")
    assert root.returncode == 0 and "uv run poe test" not in root.stdout, (
        f"`make -n check` still runs the suite, so the residual was not "
        f"exercised.\n{root.stdout}{root.stderr}"
    )
    result = _run_guard(tree)
    assert result.returncode == 0, (
        f"the intra-step repoint was REFUSED, which means the residual "
        f"documented here and in the guard's header is closed and both should "
        f"say so.\n{result.stdout}{result.stderr}"
    )
