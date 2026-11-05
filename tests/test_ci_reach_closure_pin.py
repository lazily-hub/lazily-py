"""WHICH targets `make check` must run, pinned in three parts (#pinreachclosure).

``scripts/check-ci-reach.sh`` reports how many targets CI reaches. Nothing used
to pin which targets those are, and the hole was measured on this Makefile at
exit 0 three separate ways:

1. **A dropped prerequisite.** Delete ``test-interop-peer`` from ``check:``'s
   prerequisite list and the guard prints
   ``OK - 7 target(s) reached by CI, 0 excused, 1 carrying no gate`` and exits 0.
   The interop peer is the single cross-binding wire-compatibility gate and the
   original #lzinteroppeerci casualty; it left the build's obligations with no
   trace but a 7 where an 8 had been.

2. **A make conditional.** ``prereqs_of`` reads Makefile SOURCE TEXT — the first
   line matching ``^check:``, scanned by awk, with no knowledge of conditionals.
   Wrap the prerequisite list in a dead ``ifeq (0,1)`` branch and the line awk
   reads is not the line make parses. Measured here: ``make -n check`` ran no
   ``poe ty`` at all and the guard's output was byte-identical (``cmp -s``, both
   streams) to the healthy run, ``reached type-check`` included. A names-only pin
   passes that state **by construction** — the awk closure is the same constant in
   both — which is why the make-derived oracle, not the pin, is load-bearing.

3. **A neutered recipe.** Keep the name, replace the recipe with ``true``.
   Membership is intact and the oracle is satisfied (the commands are gone from
   both sides, so the subset holds trivially); the target is silently reclassified
   as "carrying no gate", which is the one classification exempt from every
   requirement in the guard. The verdict was
   ``OK - 7 target(s) reached by CI, 0 excused, 2 carrying no gate`` — the same
   line the script's own header records as the #lzgrepcpipefail false green,
   reached by an entirely different route.

Out of scope, and tested nowhere because it is not closed: swapping a target's
recipe for a gate CI already runs. Measured here too — ``type-check:`` running
``uv run ruff check --no-fix src/lazily/ tests/`` produced output byte-identical
to healthy, exit 0, and defeats all three pins. Closing it needs a per-target
recipe anchor inside the guard, a second spelling of every recipe, which the
script's header records as the mistake that already cost lazily-cpp a hardcoded
duplicate path plus a hand-written equality assertion.

These tests drive the real script in a subprocess against a scratch copy of the
real Makefile, conf and workflow, for the reason the sibling module gives: the
thing under test is a bash file, and a Python restatement of its logic would be a
second definition to keep in step.
"""

from __future__ import annotations

import functools
import os
import re
import subprocess
from typing import TYPE_CHECKING

import pytest


if TYPE_CHECKING:
    from pathlib import Path

# The scratch tree and the subprocess runner are shared with the make-failure
# module rather than restated here. Two definitions of "a copy of everything the
# guard reads" would drift, and a stale one silently narrows what these tests
# exercise — the copy IS the premise of every assertion below.
from test_ci_reach_make_failure import (
    CI_REACH_SCRIPT,
    REPO_ROOT,
    _run_guard,
    _scratch_tree,
)


# ------------------------------------------------------------ the make oracle
#
# What `make check` runs is make's to say, not a literal's. These tests used to
# carry the `check:` line and the gate count as hand-copied literals; when
# c346950 added `core-evidence-check` as a 13th gate, the guard's own pins moved
# with it and these literals did not, so five tests went red on a count and a
# string that described no Makefile at all. Everything below is derived from
# make's printed database (`make -pqrR`) — the rules make PARSED, conditionals
# and all — and the guard's hand-maintained pins are compared against it, so a
# gate added or dropped in one place and not the other fails naming the target.


def _make_database(root: Path) -> dict[str, tuple[list[str], list[str]]]:
    """make's own parse of `root`'s Makefile: target -> (prerequisites, recipe).

    `-p` prints the database, `-q` runs no recipe, `-rR` drops the built-in
    rules and variables so only this Makefile's targets appear. `-q` exits 1
    whenever anything is out of date — every phony target always is — and 2 on
    a real error, so 0 and 1 are the healthy codes and 2 is refused rather than
    parsed. MAKEFLAGS and friends are dropped so that running under
    `make -k check` (or a jobserver) cannot change what is printed.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"MAKEFLAGS", "MFLAGS", "MAKELEVEL", "MAKEOVERRIDES"}
    }
    result = subprocess.run(
        ["make", "-pqrR"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode in (0, 1), (
        f"`make -pqrR` in {root} exited {result.returncode}, so make could not "
        f"parse the Makefile and there is no database to derive the closure "
        f"from.\nstderr:\n{result.stderr}"
    )
    lines = result.stdout.splitlines()
    assert "# Files" in lines, (
        f"`make -pqrR` printed no `# Files` section, so this parser is reading a "
        f"database shape it does not know and would derive an empty closure.\n"
        f"{result.stdout[:2000]}"
    )
    database: dict[str, tuple[list[str], list[str]]] = {}
    not_a_target = False
    current: str | None = None
    for line in lines[lines.index("# Files") + 1 :]:
        if line.startswith("# files hash-table stats"):
            break
        if line == "# Not a target:":
            not_a_target = True
            continue
        if not line:
            current, not_a_target = None, False
            continue
        if line.startswith("\t"):
            if current is not None:
                database[current][1].append(line[1:])
            continue
        if line.startswith("#"):
            continue
        header = re.match(r"^([^:#=\s]+):(?![:=])(.*)$", line)
        if header is None or not_a_target:
            continue
        current = header.group(1)
        prerequisites = [p for p in header.group(2).split() if p != "|"]
        database[current] = (prerequisites, [])
    return database


def _make_closure(root: Path, target: str = "check") -> set[str]:
    """`target` plus every target make would build on its way there."""
    database = _make_database(root)
    assert target in database, (
        f"make's database for {root} has no `{target}` rule, so there is no "
        f"closure to derive. Targets make knows: {sorted(database)}"
    )
    closure: set[str] = set()
    pending = [target]
    while pending:
        name = pending.pop()
        if name in closure:
            continue
        closure.add(name)
        pending.extend(database.get(name, ([], []))[0])
    return closure


def _make_recipeless(root: Path, target: str = "check") -> set[str]:
    """Members of `target`'s closure for which make holds no recipe at all."""
    database = _make_database(root)
    return {
        name
        for name in _make_closure(root, target)
        if not database.get(name, ([], []))[1]
    }


@functools.cache
def _check_rule() -> str:
    """The `check:` line as make parsed it, rendered as one source line.

    Every perturbation below rewrites this exact string, and asserts it appears
    exactly once first: a perturbation that silently matched nothing would leave
    the tree healthy and the test green for the wrong reason. Rendering it from
    make's database rather than copying it by hand means that assertion now also
    says the Makefile's `check:` line IS the rule make reads — one line, in
    make's order — instead of saying it matches a literal that went stale.
    """
    prerequisites, _ = _make_database(REPO_ROOT)["check"]
    return "check: " + " ".join(prerequisites) + "\n"


#: The gate the conditional and classification attacks land on, and the command
#: that proves whether make really runs it. `type-check` is a one-line recipe
#: delegating to `poe ty`, so "did make run this target" has a single
#: unambiguous string answer.
_VICTIM = "type-check"
_VICTIM_COMMAND = "poe ty"

#: The gate the membership attacks land on. Chosen because the cost of losing it
#: is already on the record: it is the #lzinteroppeerci casualty.
_DROPPED = "test-interop-peer"


@pytest.fixture(name="tree")
def _tree(tmp_path: Path) -> Path:
    return _scratch_tree(tmp_path)


def _rewrite_check_rule(tree: Path, replacement: str) -> None:
    makefile = tree / "Makefile"
    source = makefile.read_text(encoding="utf-8")
    assert source.count(_check_rule()) == 1, (
        f"expected exactly one copy of the `check:` rule line to perturb; found "
        f"{source.count(_check_rule())}. A perturbation that matches nothing leaves "
        f"a healthy tree, and every assertion below would then be measuring the "
        f"healthy run."
    )
    makefile.write_text(source.replace(_check_rule(), replacement, 1), encoding="utf-8")


def _make_n(tree: Path, target: str, **env: str) -> subprocess.CompletedProcess[str]:
    """`make -n <target>`, exit code read directly and never through a pipe."""
    return subprocess.run(
        ["make", "-n", target],
        cwd=tree,
        env=dict(os.environ, **env),
        capture_output=True,
        text=True,
    )


def _healthy(tree: Path) -> subprocess.CompletedProcess[str]:
    result = _run_guard(tree)
    assert result.returncode == 0 and "check-ci-reach: OK" in result.stdout, (
        f"the unperturbed scratch copy did not report OK, so there is no baseline "
        f"to move away from.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


# --------------------------------------------------------------------------- A
#
# The oracle. These are the tests that matter: a test that only checks the
# membership pin passes in the compromised state below.


def test_a_dead_make_conditional_cannot_decouple_the_closure_from_make(
    tree: Path,
) -> None:
    """`make -n check` is the authority on what `make check` runs, not awk.

    The attack needs no environment variable and no cooperation from anything
    outside the Makefile: a branch that is false by construction, whose body awk
    reads and make does not.
    """
    baseline = _healthy(tree)

    _rewrite_check_rule(
        tree,
        "ifeq (0,1)\n"
        + _check_rule()
        + "else\n"
        + _check_rule().replace(f" {_VICTIM}", "")
        + "endif\n",
    )

    # Premise 1: this is NOT the make-failure class. make is perfectly happy.
    root = _make_n(tree, "check")
    assert root.returncode == 0, (
        f"`make -n check` FAILED, so this is the #lzgrepcpipefail state the "
        f"sibling module covers, not the decoupling under test here.\n"
        f"stderr:\n{root.stderr}"
    )
    # Premise 2: make does not run the victim's command for the root...
    assert _VICTIM_COMMAND not in root.stdout, (
        f"`make -n check` still runs `{_VICTIM_COMMAND}`, so the gate never left "
        f"the real build and there is nothing here to catch.\n{root.stdout}"
    )
    # ...while the victim itself still has a perfectly good recipe, which is what
    # makes the awk closure's answer look right.
    member = _make_n(tree, _VICTIM)
    assert member.returncode == 0 and _VICTIM_COMMAND in member.stdout, (
        f"`make -n {_VICTIM}` does not run `{_VICTIM_COMMAND}`, so the two sides "
        f"of the oracle agree and the decoupling was not produced.\n"
        f"stdout:\n{member.stdout}\nstderr:\n{member.stderr}"
    )

    result = _run_guard(tree)
    combined = result.stdout + result.stderr

    assert (result.stdout, result.stderr) != (baseline.stdout, baseline.stderr), (
        f"the guard's output is byte-identical to the healthy run over a Makefile "
        f"whose `check` no longer runs `{_VICTIM}`. That is the measured pre-fix "
        f"state: the awk closure is a constant across both branches, so a pin "
        f"compared against it is satisfied by construction rather than by the "
        f"Makefile being honest (#pinreachclosure).\n{combined}"
    )
    assert result.returncode != 0, (
        f"the guard reported OK over a closure make does not build.\n{combined}"
    )
    assert "does NOT run" in result.stderr, (
        f"the guard failed by some route other than the oracle, so this test is "
        f"not exercising the mechanism it is about.\n{combined}"
    )
    assert _VICTIM in result.stderr, (
        f"the oracle finding does not name `{_VICTIM}`. An unnamed finding sends "
        f"the reader to audit nine targets by hand.\n{combined}"
    )
    assert "check-ci-reach: OK" not in result.stdout, (
        f"the guard reached its OK verdict line anyway.\n{combined}"
    )


def test_the_oracle_is_silent_on_the_honest_branch_of_a_live_conditional(
    tree: Path,
) -> None:
    """Fires on the compromised branch, silent on the honest one — same Makefile.

    A guard whose verdict does not depend on what make would actually do is not
    detecting anything; the pre-fix measurement was exactly that, a verdict
    constant across both branches. Driving both branches of one live conditional
    from the environment pins the dependence itself, which a single perturbed run
    cannot: a check that always failed here would pass that one too.
    """
    _rewrite_check_rule(
        tree,
        "ifeq ($(SKIP_SLOW),)\n"
        + _check_rule()
        + "else\n"
        + _check_rule().replace(f" {_VICTIM}", "")
        + "endif\n",
    )

    honest_root = _make_n(tree, "check")
    skipped_root = _make_n(tree, "check", SKIP_SLOW="1")
    assert _VICTIM_COMMAND in honest_root.stdout, (
        f"the honest branch does not run `{_VICTIM_COMMAND}`, so the conditional "
        f"was planted wrong.\n{honest_root.stdout}"
    )
    assert _VICTIM_COMMAND not in skipped_root.stdout, (
        f"SKIP_SLOW=1 still runs `{_VICTIM_COMMAND}`, so the branch under test "
        f"was never taken.\n{skipped_root.stdout}"
    )

    honest = _run_guard(tree)
    assert honest.returncode == 0, (
        f"the oracle fired on the HONEST branch, where make really does run every "
        f"closure member. A guard that reds on a correct Makefile is a guard "
        f"people delete.\nstdout:\n{honest.stdout}\nstderr:\n{honest.stderr}"
    )

    skipped = _run_guard(tree, SKIP_SLOW="1")
    combined = skipped.stdout + skipped.stderr
    assert skipped.returncode != 0, (
        f"the same Makefile produced OK with `{_VICTIM}` skipped, so the verdict "
        f"does not depend on what make would run (#pinreachclosure).\n{combined}"
    )
    assert "does NOT run" in skipped.stderr and _VICTIM in skipped.stderr, (
        f"the refusal did not come from the oracle naming `{_VICTIM}`.\n{combined}"
    )


def test_a_per_invocation_run_id_does_not_red_the_oracle(tree: Path) -> None:
    """The oracle compares ANCHORS, and this is the test that keeps it that way.

    The obvious spelling compares the two `make -n` outputs line for line.
    lazily-gd measured that shape permanently red on the one target carrying its
    suite: `make -n <target>` and `make -n <root>` are two separate make
    invocations, a `:=` run id is minted once per invocation, so a recipe that
    prints one cannot match itself. This Makefile mints exactly such an id.

    py escapes the raw-line red today only because it delivers the id through
    `export` rather than through recipe text — one edit away from being false. So
    plant the edit and require silence. The failure this pins is not the red
    itself; it is what a red on an honest Makefile provokes, which is someone
    weakening the oracle until it stops seeing the `ifeq` attack above.
    """
    makefile = tree / "Makefile"
    source = makefile.read_text(encoding="utf-8")
    recipe = f"{_VICTIM}:\n\t{_VICTIM_COMMAND}\n"
    assert source.count(recipe) == 1, (
        f"expected exactly one `{_VICTIM}` recipe of the form {recipe!r}; found "
        f"{source.count(recipe)}."
    )
    makefile.write_text(
        source.replace(
            recipe,
            f"{_VICTIM}:\n\t{_VICTIM_COMMAND} --run-id=$(LAZILY_CONFORMANCE_RUN_ID)\n",
            1,
        ),
        encoding="utf-8",
    )

    # Premise: the two invocations really do print different text. Without this
    # the test passes on a Makefile where nothing varies, which is the state it
    # exists to move away from.
    member = _make_n(tree, _VICTIM)
    root = _make_n(tree, "check")
    member_line = next(
        line for line in member.stdout.splitlines() if "--run-id=" in line
    )
    root_line = next(line for line in root.stdout.splitlines() if "--run-id=" in line)
    assert member_line != root_line, (
        f"both invocations printed the same run id, so a raw-line comparison "
        f"would have passed too and this test proves nothing about anchors.\n"
        f"  member: {member_line}\n  root:   {root_line}"
    )

    result = _run_guard(tree)
    assert "does NOT run" not in result.stderr, (
        f"the oracle reported that `make check` does not run `{_VICTIM}`, over a "
        f"Makefile where it demonstrably does. That is lazily-gd's false red: the "
        f"comparison is keyed on text that cannot repeat across two make "
        f"invocations. Fix it by anchoring the comparison, never by loosening "
        f"what the oracle requires.\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


# --------------------------------------------------------------------------- B
#
# The membership pin. Meaningful only on top of A.


def test_a_dropped_prerequisite_is_refused_and_named(tree: Path) -> None:
    """The original defect: a gate leaves and only a count changes."""
    _healthy(tree)
    _rewrite_check_rule(tree, _check_rule().replace(f" {_DROPPED}", ""))

    root = _make_n(tree, "check")
    assert root.returncode == 0, (
        f"`make -n check` FAILED, so this is the make-failure class rather than a "
        f"clean removal.\nstderr:\n{root.stderr}"
    )

    result = _run_guard(tree)
    combined = result.stdout + result.stderr

    assert result.returncode != 0, (
        f"a gate was removed from `check`'s prerequisites and the guard approved "
        f"the build that removed it (#pinreachclosure).\n{combined}"
    )
    assert _DROPPED in result.stderr, (
        f"the guard failed without naming `{_DROPPED}`. Naming it is the entire "
        f"improvement over the pre-fix `OK - 7 target(s) reached`, which was also "
        f"a true statement about seven targets.\n{combined}"
    )
    assert "EXPECTED_CLOSURE_TARGETS" in result.stderr, (
        f"the diagnostic does not name the pin, so a reader cannot tell which of "
        f"the two remedies applies to them.\n{combined}"
    )
    assert "restore" in result.stderr, (
        f"the diagnostic offers no remedy other than editing the pin. A pin that "
        f"can only be satisfied by editing it teaches people to edit it "
        f"reflexively, which is how it stops meaning anything.\n{combined}"
    )
    assert "check-ci-reach: OK" not in result.stdout, (
        f"the guard reached its OK verdict line anyway.\n{combined}"
    )


def test_a_swap_is_refused_in_both_directions(tree: Path) -> None:
    """Drop one, add one — the case a count floor cannot see.

    This is why the pin is set equality and not `MIN_CLOSURE_TARGETS`. The count
    is unchanged, and both halves of the swap have to be reported separately: the
    departure is a lost gate, the arrival is an unrecorded one.
    """
    _healthy(tree)
    _rewrite_check_rule(
        tree,
        _check_rule().replace(f" {_DROPPED}", "").rstrip("\n")
        + " smuggled-in\n"
        + "\nsmuggled-in:\n\tuv run ruff check --no-fix src/lazily/ tests/\n",
    )

    root = _make_n(tree, "check")
    assert root.returncode == 0, f"`make -n check` FAILED.\nstderr:\n{root.stderr}"

    result = _run_guard(tree)
    combined = result.stdout + result.stderr

    assert result.returncode != 0, (
        f"a swap kept the count at eight and the guard approved it.\n{combined}"
    )
    assert _DROPPED in result.stderr, (
        f"the departure of `{_DROPPED}` was not reported.\n{combined}"
    )
    assert "smuggled-in" in result.stderr, (
        f"the arrival was not reported. Reporting only the departure would let the "
        f"next unpinned target be removed in silence.\n{combined}"
    )
    assert "not pinned" in result.stderr, (
        f"the two directions were not reported as different findings, so a reader "
        f"gets one remedy for two problems.\n{combined}"
    )


def test_an_excuse_outside_the_closure_is_refused(tree: Path) -> None:
    """The mirror direction, which `KNOWN_UNCOVERED` has always checked.

    An excuse is only ever consulted while walking the closure, so one naming a
    target outside it is read by nobody and counted by nothing: `0 excused`, no
    complaint, exit 0. It still reads as a claim about what this binding does not
    enforce, which is the one thing the conf file exists to be.
    """
    _healthy(tree)
    conf = tree / "scripts" / "ci-reach.conf"
    conf.write_text(
        conf.read_text(encoding="utf-8")
        + "excuse: bench-scale a real target that is not in check's closure\n"
        + "excuse: not-a-rule-at-all not a rule in the Makefile either\n",
        encoding="utf-8",
    )

    result = _run_guard(tree)
    combined = result.stdout + result.stderr

    assert result.returncode != 0, (
        f"two excuses naming targets outside the closure were ignored in silence "
        f"(#pinreachclosure).\n{combined}"
    )
    assert "bench-scale" in result.stderr, (
        f"an excuse for a real Makefile target outside the closure was not "
        f"reported.\n{combined}"
    )
    assert "not-a-rule-at-all" in result.stderr, (
        f"an excuse for a name matching no rule at all was not reported.\n{combined}"
    )
    assert "excused  bench-scale" not in combined, (
        f"the stray excuse was counted as an excuse.\n{combined}"
    )
    assert "the closure is not the pinned set" not in result.stderr, (
        f"a stray excuse was reported as a closure/pin disagreement. The closure "
        f"and the pin agree here; the conf is what disagrees with the Makefile, "
        f"and sending the reader to the wrong file is the cost.\n{combined}"
    )


def test_the_guard_cannot_be_aimed_at_a_smaller_root(tree: Path) -> None:
    """Pin the ROOT, because every set here is derived from it.

    Measured at exit 0 before the pin: `CI_REACH_ROOT_TARGET=lint` printed
    `OK - 1 target(s) reached by CI, 0 excused, 0 carrying no gate` over a
    Makefile whose `check` still ran eight gates. Nothing about the Makefile had
    to change, and no reachability verdict was wrong — the audit was simply
    pointed somewhere smaller.
    """
    _healthy(tree)

    result = _run_guard(tree, CI_REACH_ROOT_TARGET="lint")
    combined = result.stdout + result.stderr

    assert result.returncode != 0, (
        f"the guard audited `lint` and called it OK.\n{combined}"
    )
    assert "lint" in result.stderr and "check" in result.stderr, (
        f"the refusal does not name both the root it was given and the root it is "
        f"pinned to, so the reader cannot see which one is wrong.\n{combined}"
    )
    assert "check-ci-reach: OK" not in result.stdout, (
        f"the guard reached its OK verdict line anyway.\n{combined}"
    )
    assert "reached  lint" not in result.stdout, (
        f"the guard produced per-target verdicts for the wrong root before "
        f"refusing. Every one of them describes a closure nobody pinned.\n"
        f"{combined}"
    )


# --------------------------------------------------------------------------- C
#
# The classification pin.


def test_a_neutered_recipe_is_refused_and_named(tree: Path) -> None:
    """Keeping the name and emptying the recipe moves the target, not the graph.

    "Carrying no gate" is a real and necessary verdict — a mkdir-only reset step
    cannot fail a build, so it cannot hide one — which is exactly what makes it a
    good hiding place: it is the one classification exempt from every requirement
    in this guard. Membership is untouched, and the oracle is satisfied because
    the commands are gone from both sides of its subset test.
    """
    baseline = _healthy(tree)

    makefile = tree / "Makefile"
    source = makefile.read_text(encoding="utf-8")
    recipe = f"{_VICTIM}:\n\t{_VICTIM_COMMAND}\n"
    assert source.count(recipe) == 1, (
        f"expected exactly one `{_VICTIM}` recipe of the form {recipe!r}; found "
        f"{source.count(recipe)}. The perturbation has to actually empty a real "
        f"gate or this test proves nothing."
    )
    makefile.write_text(
        source.replace(recipe, f"{_VICTIM}:\n\ttrue\n", 1), encoding="utf-8"
    )

    root = _make_n(tree, "check")
    assert root.returncode == 0, (
        f"`make -n check` FAILED, so this is the make-failure class.\n"
        f"stderr:\n{root.stderr}"
    )
    assert _VICTIM_COMMAND not in root.stdout, (
        f"`make -n check` still runs `{_VICTIM_COMMAND}`, so the gate was not "
        f"emptied.\n{root.stdout}"
    )
    member = _make_n(tree, _VICTIM)
    assert member.returncode == 0, (
        f"`make -n {_VICTIM}` FAILED, which would make this the readability class "
        f"instead.\nstderr:\n{member.stderr}"
    )

    result = _run_guard(tree)
    combined = result.stdout + result.stderr

    assert result.returncode != 0, (
        f"an emptied gate was reported as carrying no gate and the run passed — "
        f"the same verdict line this script's header records as the "
        f"#lzgrepcpipefail false green, reached another way.\n{combined}"
    )
    assert "EXPECTED_NO_GATE_TARGETS" in result.stderr, (
        f"the guard failed by some route other than the classification pin.\n{combined}"
    )
    assert _VICTIM in result.stderr, (
        f"the classification finding does not name `{_VICTIM}`.\n{combined}"
    )
    assert "restore the recipe" in result.stderr, (
        f"the diagnostic does not offer restoring the recipe, so the only remedy "
        f"on offer is ratifying the emptied gate.\n{combined}"
    )
    assert (result.stdout, result.stderr) != (baseline.stdout, baseline.stderr), (
        f"output identical to healthy.\n{combined}"
    )


# ------------------------------------------------------- the pins as source text


def _bash_array(name: str) -> list[str]:
    """Entries of a `NAME=( ... )` array in the guard, straight from source."""
    source = CI_REACH_SCRIPT.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(name)}=\(\n(.*?)^\)$", source, re.M | re.S)
    assert match is not None, (
        f"no `{name}=( ... )` array found in {CI_REACH_SCRIPT}. Either the pin "
        f"was deleted — in which case the closure is unpinned again and the "
        f"behavioural tests above are the only thing left — or it was reshaped "
        f"and this pattern is now looking at nothing, which would make every "
        f"assertion below vacuously true."
    )
    return [
        line.strip()
        for line in match.group(1).splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_the_pins_are_declared_non_empty_and_canonical() -> None:
    """Source-level, because an empty or absent pin cannot fail behaviourally.

    A pin of zero entries is satisfied by nothing at all in the direction that
    matters, and a deleted pin takes its own diagnostics with it — the guard would
    print OK exactly as it did before #pinreachclosure. Sorted and de-duplicated
    is asserted rather than asked for in a comment: a duplicate is how a
    hand-merge of two closure edits keeps a name it meant to replace, and the
    array's whole value is that a reader can diff it at a glance.
    """
    source = CI_REACH_SCRIPT.read_text(encoding="utf-8")
    assert 'EXPECTED_ROOT_TARGET="check"' in source, (
        f"the root is not pinned to `check` in {CI_REACH_SCRIPT}. Every set the "
        f"guard computes is derived from the root, so an unpinned root makes the "
        f"other two pins statements about whatever it happens to point at."
    )

    for name, expected_member in (
        ("EXPECTED_CLOSURE_TARGETS", "test-interop-peer"),
        ("EXPECTED_NO_GATE_TARGETS", "check"),
    ):
        entries = _bash_array(name)
        assert entries, f"`{name}` is empty, so it pins nothing."
        assert entries == sorted(set(entries)), (
            f"`{name}` is not sorted and de-duplicated: {entries}"
        )
        assert expected_member in entries, (
            f"`{name}` does not contain `{expected_member}`, which this test uses "
            f"to confirm it is reading the array it thinks it is."
        )


def test_the_reported_accounting_partitions_the_pinned_closure(tree: Path) -> None:
    """Every pinned target gets exactly one verdict, and the numbers say so.

    The OK line reports three tallies. On a healthy tree they must partition the
    pinned closure exactly — if they do not, some pinned target was audited and
    then left out of the report, which is the shape a future `continue` with no
    counter would take. The no-gate tally is pinned against its own array for the
    same reason.
    """
    result = _healthy(tree)
    match = re.search(
        r"OK .* (\d+) target\(s\) reached by CI, (\d+) excused, "
        r"(\d+) carrying no gate",
        result.stdout,
    )
    assert match is not None, (
        f"could not parse the OK line's tallies, so this test would assert "
        f"nothing.\n{result.stdout}"
    )
    reached, excused, nogate = (int(g) for g in match.groups())

    closure_pin = _bash_array("EXPECTED_CLOSURE_TARGETS")
    nogate_pin = _bash_array("EXPECTED_NO_GATE_TARGETS")

    assert reached + excused + nogate == len(closure_pin), (
        f"the guard reported {reached} reached + {excused} excused + {nogate} "
        f"carrying no gate = {reached + excused + nogate}, but "
        f"EXPECTED_CLOSURE_TARGETS pins {len(closure_pin)} targets. Either a "
        f"pinned target got no verdict, or a verdict was issued for something "
        f"outside the pin."
    )
    assert nogate == len(nogate_pin), (
        f"{nogate} target(s) were classified as carrying no gate, but "
        f"EXPECTED_NO_GATE_TARGETS pins {len(nogate_pin)}."
    )


def test_the_guard_pins_are_the_closure_make_builds() -> None:
    """The guard's hand-kept pins against make's own database, by name.

    `EXPECTED_CLOSURE_TARGETS`, `EXPECTED_NO_GATE_TARGETS` and the target column
    of `EXPECTED_GATE_STEPS` are three hand-maintained lists. Inside the guard
    they are compared with an awk scan of the Makefile and with `make -n`; here
    they are compared with the closure make itself derives from its parsed
    rules, so a gate added to or dropped from `check` without the matching pin
    edit fails naming the target — in both directions — rather than as a count
    one higher or lower than some literal somewhere expected.
    """
    closure = _make_closure(REPO_ROOT)
    recipeless = _make_recipeless(REPO_ROOT)
    gates = closure - recipeless

    for name, pinned, derived in (
        (
            "EXPECTED_CLOSURE_TARGETS",
            set(_bash_array("EXPECTED_CLOSURE_TARGETS")),
            closure,
        ),
        (
            "EXPECTED_NO_GATE_TARGETS",
            set(_bash_array("EXPECTED_NO_GATE_TARGETS")),
            recipeless,
        ),
        (
            "EXPECTED_GATE_STEPS",
            {row.strip('"').split()[0] for row in _bash_array("EXPECTED_GATE_STEPS")},
            gates,
        ),
    ):
        built_not_pinned = sorted(derived - pinned)
        pinned_not_built = sorted(pinned - derived)
        assert not built_not_pinned and not pinned_not_built, (
            f"`{name}` in {CI_REACH_SCRIPT} does not describe what make builds for "
            f"`check` (from `make -pqrR`).\n"
            f"  make builds it, the pin omits it:  {built_not_pinned}\n"
            f"  pinned, make does not build it:    {pinned_not_built}\n"
            f"Add or remove the gate in the Makefile's `check:` rule and in the "
            f"guard's pins together; one without the other is an unreviewed change "
            f"to what CI is held to."
        )
