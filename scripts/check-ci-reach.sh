#!/usr/bin/env bash
# CI-reachability guard (#lzcheckcireachguard).
#
# Fails the build when `make check` runs a gate that CI never reaches. That is the
# drift this guard exists for: someone adds a target to `check`, it passes locally
# forever, and no CI job ever executes it — which is exactly how #lzinteroppeerci
# happened. The interop peer, the single cross-binding wire-compatibility gate, was
# in every binding's `check` and in no binding's workflow, for months.
#
# It also exists because the obvious hand-audit is WRONG. Grepping the workflows
# for "make check" reported all nine bindings as covered; every one of those hits
# was a COMMENT. Comments are the reason this is a script and not a convention:
# only `run:` bodies count here, and comment lines inside them are stripped before
# anything is matched.
#
# WHAT IT PROVES
#
#   For every target in `check`'s prerequisite closure, THE CI `run:` STEP PINNED
#   FOR IT invokes the same program with the same distinguishing flags.
#
#   "The step pinned for it", not "some step somewhere" — that was the claim
#   until #reversereachdirection, and it is weaker than it reads as. See WHICH CI STEP
#   RUNS IT below for the two ways it failed open and what each one cost.
#
#   On its own that sentence is true of whatever `check` happens to require
#   today, which is not much of a claim: delete a target from its prerequisite
#   list and every surviving member is still reached, so the guard prints
#   `OK - N-1 target(s) reached by CI` and never names the one that left. Three
#   pins, added together under #pinreachclosure, make the SET the claim is about
#   hold still:
#
#     1. The ORACLE. For every target in the closure, `make -n <target>`'s
#        commands all appear in `make -n check`'s. This is the load-bearing one:
#        the closure is read from Makefile source text by awk and cannot see a
#        make conditional, so without asking make the other two pins are pinning
#        a set that need not describe anything make builds.
#     2. `EXPECTED_CLOSURE_TARGETS`, set-equal to the closure, both directions
#        named — so a dropped gate is a required, reviewable edit rather than a
#        count that quietly decrements.
#     3. `EXPECTED_NO_GATE_TARGETS`, set-equal to the targets classified as
#        carrying no gate — so a gate cannot be emptied while keeping its name,
#        which leaves membership intact and moves the target into the one
#        classification that is exempt from every requirement here.
#
#   A fourth pin, `EXPECTED_GATE_STEPS` (#reversereachdirection), is about the CI
#   side rather than the closure: which STEP runs each gate. It brings a rung of
#   its own, the anchor-collision check, for the one case step-scoping cannot see.
#
#   Four more, added under #verifyworkflowactually, are about neither the closure
#   nor the steps but about ACTIVATION — whether the workflow and the job run at
#   all. Everything above them is a claim about what CI would run IF it ran, and
#   `ci-reach.conf`'s "runs on every push" was a COMMENT holding the whole audit
#   up: with it untrue, every pin above stays green while no gate runs on any
#   push. `EXPECTED_TRIGGERS`, `EXPECTED_TRIGGER_FILTERS`, `EXPECTED_GATE_JOBS`
#   and `EXPECTED_GATE_JOB_GUARDS` pin that layer as exact values in both
#   directions. Read their declarations for the measurements; read the table below
#   for which of them is load-bearing for what, including the one that is the sole
#   catcher of nothing.
#
#   Each of these was measured on this Makefile at exit 0 before the pin it
#   answers existed; the cases are recorded above each one. They divide cleanly,
#   and the division is worth keeping straight when reading a failure:
#
#     the ORACLE               the EXECUTED set matches the modelled one
#     the MEMBERSHIP pin       the MODELLED set has not changed unnoticed
#     the CLASSIFICATION pin   no modelled member has been hollowed out
#     the GATE-STEP pin        each member's gate is still run by the CI step
#                              claimed to run it
#     the COLLISION rung       no pinned step runs a SECOND member's gate
#     the TRIGGER pin          the workflow's `on:` keys are the pinned ones
#     the TRIGGER-FILTER pin   and so are the filters under each of them
#     the GATE-JOB pin         the gate steps are in the pinned job(s)
#     the JOB-GUARD pin        and that job's `if:` / `continue-on-error:` values
#                              are the pinned ones
#     the VACUITY floor        the audit did not evaluate nothing
#
#   TEN MECHANISMS, and the last four are #verifyworkflowactually. They are
#   ACTIVATION pins: everything above them is about what CI would run IF it ran,
#   and until they existed nothing here had an opinion about whether it runs. Each
#   one's measured coverage — including the one that catches nothing on its own —
#   is in the table below; do not infer it from the descriptions.
#
#   None of them says the set is CORRECT.
#
#   WHICH RUNG IS LOAD-BEARING FOR WHICH FAULT. Measured by deleting rungs one at
#   a time and re-running the fault, never by reading their descriptions — two of
#   the rows below contradict what the descriptions suggest.
#
#     dead `ifeq` decoupling the closure   ORACLE only. The membership pin passes
#                                          by construction — it is pinned equal to
#                                          the same awk constant the compromised
#                                          state leaves untouched.
#     a dropped prerequisite               MEMBERSHIP only.
#     ONE recipe neutered                  CLASSIFICATION and GATE-STEP, each
#                                          sufficient ALONE. Measured: delete
#                                          either and `type-check: true` is still
#                                          caught and named; delete BOTH and it
#                                          exits 0. The vacuity floor does not
#                                          fire at all here — seven members still
#                                          carry gates.
#     ALL recipes neutered                 CLASSIFICATION, GATE-STEP and the
#                                          FLOOR, all three. Measured: with the
#                                          floor deleted the other two name all
#                                          eight targets; with all three deleted
#                                          it exits 0.
#     a recipe repointed at a step no       GATE-STEP only, and it is the whole
#     member runs, or at another            reason that pin exists. Falsified by
#     member's gate                         reverting the scoped lookup alone: the
#                                          attack exits 0 again.
#     two gates consolidated into one       COLLISION only. NOT the gate-step
#     CI step, then one repointed at        pin — every anchor is inside its own
#     the other                             pinned step. Measured at exit 0 with
#                                          the collision rung removed and
#                                          everything else in place, while
#                                          `make -n check` ran `ruff format`
#                                          zero times.
#     a pinned step carrying `if:` or       GATE-STEP's guard rung, and only
#     `continue-on-error: true`             because the step map gave this guard
#                                          a handle on the step at all
#     `on:` reduced to `workflow_dispatch` TRIGGERS and TRIGGER-FILTER, each
#                                          sufficient ALONE — dropping `push`
#                                          drops its `branches:` row with it.
#                                          Measured: revert either and it is
#                                          still refused; revert both and it
#                                          exits 0.
#     a trigger ADDED (`pull_request:`, no TRIGGERS only, and it is the only
#     filters of its own)                  fault that isolates it. It is also
#                                          the edit that would close the pull-
#                                          request gap below: it reds this guard
#                                          on purpose, so widening the trigger
#                                          set is a reviewable edit and not a
#                                          silent one.
#     `branches: ["**"]` narrowed, or a    TRIGGER-FILTER only.
#     `paths:` filter introduced
#     job-level `if: false`, a never-true  JOB-GUARD only. All three are the
#     job-level `if:`, or job-level        same row moving from `<absent>` to a
#     `continue-on-error: true`            value.
#     a gate step moved to another job, or JOB-GUARD alone is sufficient, and
#     the gate job renamed                 GATE-JOB alone is too. Measured:
#                                          revert either and both attacks are
#                                          still refused; revert BOTH and they
#                                          exit 0. So GATE-JOB is the sole
#                                          catcher of NOTHING — see the third
#                                          conclusion below for why it is kept.
#     a recipe repointed at a SIBLING       NOTHING. Residual, one step wide
#     command in its OWN pinned step        here. See WHAT IT DOES NOT PROVE.
#     a member's reach MODE flipping        GATE-STEP, both directions, because
#     between anchors and `make <t>`        the domain it is pinned set-equal to
#                                          is defined as {carries a gate} minus
#                                          {excused} minus {make-invoked}. A
#                                          separate EXPECTED_MAKE_INVOKED_TARGETS
#                                          (which lazily-zig added) would pin the
#                                          complement of an already-pinned set,
#                                          and here it would be EMPTY — the shape
#                                          this guard refuses everywhere else. So
#                                          those two collapse into one; measured
#                                          in both directions, including the zig
#                                          case where deleting a make-invoked
#                                          member's step drops it back into the
#                                          domain unpinned.
#
#   THREE CONCLUSIONS WORTH MORE THAN THE IMPLEMENTATION.
#
#   1. The GATE-STEP pin and the COLLISION rung are two PROPERTIES, not two
#      spellings of one. They overlap on the member-to-member repoint and each has
#      a case the other cannot see, both measured above. Keep both.
#
#   2. The GATE-JOB pin is the SECOND rung in this file whose only remaining
#      property is failing closed when its partner is deleted — the vacuity
#      floor's shape exactly, and the second time this design has produced it.
#      Measured, in one table, rather than argued from the descriptions: reverting
#      GATE-JOB alone leaves all nine activation attacks refused, so it is the
#      sole catcher of none of them; reverting JOB-GUARD alone leaves the two
#      job-MOVE attacks refused, which is GATE-JOB catching them; reverting both
#      turns those two green. It is kept for that, and because its row is the one
#      a reader can read as "the set of jobs that gate this repo", where the
#      JOB-GUARD rows are two per job and read as a value table. It is NOT a
#      second spelling of JOB-GUARD's property, and it is not load-bearing for any
#      fault on its own; both halves of that are said out loud here so the next
#      reader does not have to re-measure to find out which.
#
#   3. The VACUITY floor is now restated TWICE, and last cycle's note that the
#      classification pin subsumes its trigger understated it. Its only remaining
#      property is surviving both pins being deleted. It was also PRE-EMPTING
#      them — its early `exit 1` printed "nothing was verified" and suppressed the
#      two findings that name which eight gates went — so it runs LAST now. That
#      is an ordering change, not a new mechanism, and it is the one thing worth
#      doing about a rung whose value is being the last line rather than the
#      catching one. It is not deleted: nothing else fails closed with both pins
#      gone.
#
# WHAT A SET PIN STRUCTURALLY CANNOT SEE
#
#   ORDER. A set has none, so no pin here can see `check`'s prerequisites being
#   reordered. In this binding that is enforced elsewhere, and measured:
#   `LAZILY_CONFORMANCE_RUN_ID` is minted once per make invocation, `test`'s first
#   command stamps it into the fixture manifest, and check-conformance-coverage.sh
#   refuses a manifest carrying any other id. Running `make conformance-coverage`
#   in its own invocation fails with `found: make-...173027-3bbedcb61b25 / want:
#   make-...173322-a3a30b22ed05`, exit 2 — so `conformance-coverage` cannot be
#   satisfied unless `test` ran before it in the SAME invocation. That is the only
#   ordering constraint this closure has, and it is pinned by the evidence rung
#   rather than by anything in this file (#lzstalemanifest).
#
#   EDGES. Dropping a dependency BETWEEN two closure members leaves the node set
#   unchanged, and can pass the oracle too when some other member already pulls
#   the same commands into the root's run. Measured here: this closure is a pure
#   star — `check` has eight prerequisites and every one of them has none — so
#   there is currently no intra-closure edge to drop. The gap becomes live the
#   moment a member is given a prerequisite of its own, and nothing in this file
#   would notice.
#
# WHICH CI STEP RUNS IT
#
#   Reach is scoped to ONE named CI step per gate, pinned in EXPECTED_GATE_STEPS
#   (#reversereachdirection). The pin, the two ways the flat check failed open, and the
#   measurements behind both are recorded above that array; this is the part a
#   reader needs before deciding whether the design applies at all.
#
#   IT APPLIES HERE BECAUSE CI SPELLS EVERY GATE OUT. All eight gate-carrying
#   members are reached via ANCHORS; none is reached through `make <target>`. CI
#   invokes make ZERO times. That number was measured from `run:` bodies with
#   comments and quoted strings excluded, which matters more here than in any
#   other binding in this family: a naive grep for `make` in precommit.yml
#   reports SIXTEEN hits — eight in comments, seven in step NAMES (the steps are
#   deliberately called `Format (make format)`, `Lint (make lint)` and so on), and
#   one inside an `echo "::error::..."` string. Sixteen to zero is the widest
#   comment-and-string gap in the family, and it is the same trap this header
#   opens with.
#
#   Where CI's instruction is `make <target>` instead, this design is meaningless
#   and the guard refuses the pin rather than accepting a decoration: such a
#   target has no independent CI-side spelling, so a step name pinned for it
#   asserts nothing. lazily-gd is excluded from this work for exactly that
#   reason — its CI is `Install Godot` plus `make check`, and CI faithfully runs
#   whatever the root runs.
#
#   NO WILDCARDS ON EITHER SIDE, which is why the sweep below is 8 for 8 rather
#   than lucky. An unresolvable variable reference becomes an ANY token matching
#   exactly one token; when ANY lands at the END of a step's anchor, that step is
#   a superset of every anchor that is a subsequence of its prefix plus one free
#   token. lazily-zig measured 4 of its 7 gates deletable from CI that way, with a
#   byte-identical OK at exit 0, including the interop peer and the reachability
#   guard's own step. Measured here: 0 of 12 CI step anchors and 0 of 9 target
#   anchors contain a wildcard at all. The only interpolation in a run body is
#   `${{ matrix.python-version }}`, which the normalizer reduces to the literal
#   token `matrix.python-version`, and LAZILY_CONFORMANCE_RUN_ID is delivered
#   through `env:` / `export` and never spelled in a command — the same property
#   the oracle relies on further down. A tripwire test holds this, not a refusal:
#   a trailing wildcard is legitimate, and step-scoping already confines its
#   absorption to the ONE member pinned to that step, which makes it a special
#   case of the open hole below rather than a hole of its own.
#
#   THE MAPPING IS A BIJECTION HERE, and that was measured rather than assumed.
#   Each of the eight members' anchor sets is a subsequence of exactly one step's
#   commands, and it is a different step for each. Deleting each member's own CI
#   step in turn reds this guard 8 times out of 8. What that rules out is the
#   SUPERSET shape: `anchor_reached` searches a flat set, so a member whose anchor
#   is a subsequence of a NARROWER step's command survives the deletion of the
#   step that actually runs it (lazily-rs has 8 such members out of 46, and
#   deleting its entire default-feature test job passed byte-identically). The
#   margin here is thin — one token on six of the nine anchors, `type-check`'s
#   whole anchor being `poe ty` because its is the one recipe in this Makefile
#   that does not go through `uv run` — so this is a clean state with no slack,
#   not an immunity. The detector was validated against the shape it is for:
#   shorten `type-check`'s recipe to `poe`, which IS a subsequence of the `README
#   examples` step, delete its own step, and the pre-#reversereachdirection guard printed
#   `reached type-check` and exited 0.
#
# WHAT IT DOES NOT PROVE
#
#   That CI runs it against the same inputs, in the same environment, or that the
#   command means the same thing there. Reach is a floor, not equivalence. The
#   sibling guards (conformance-coverage, assertion-keys, scenario-coverage) are
#   what prove a run examined anything.
#
#   THAT THE GATE SET RUNS ON A PULL REQUEST. The job and trigger levels ARE
#   closed now (#verifyworkflowactually) — the four activation pins above hold the
#   `on:` keys, their filters, the gate job and its guards to exact values, and
#   each of lazily-js's three states exits 1 naming what changed. What that closes
#   is DRIFT, not correctness, and measuring it turned up the one thing here that
#   is not a drift problem:
#
#   precommit.yml has NO `pull_request:` trigger. It is the only counted workflow
#   in the family without one — the other eight bindings are `push` +
#   `pull_request`. Push with `branches: ["**"]` covers a SAME-REPO pull request,
#   because the push to the head branch is itself a run and GitHub files that
#   run's checks under the PR's head sha; measured on all four merged PRs in this
#   repo, each of which carries three green `precommit (3.1x)` checks. It does NOT
#   cover a FORK pull request, whose push happens in the fork, nor the
#   `refs/pull/N/merge` commit that `pull_request` would test — so a PR green on
#   its head and broken against an updated base is caught only after the merge
#   lands on main. Today that is latent (0 forks, no branch protection, every PR
#   so far same-repo and from the owner) and it is REPORTED rather than fixed:
#   changing `on:` is a behaviour change, not this guard's to make. The trigger
#   pin means it cannot change unnoticed in either direction — adding
#   `pull_request:` reds this guard until the pin moves with it.
#
#   THAT A GATE CANNOT BE REPOINTED WITHIN ITS OWN PINNED STEP. Scoping narrows
#   the haystack to one step, and a step carrying more than one anchor still
#   offers a choice inside itself. Width here, measured: ONE of the eight pinned
#   steps carries two anchors — `Test (make test)`, running `uv run poe
#   conformance_manifest` and `uv run poe test` — and the residual is live there:
#   drop the second command from `test:`'s recipe and the guard prints `reached
#   test` at exit 0 while `make check` runs the suite, and conformance rungs 2-4
#   with it, zero times. The number is pinned in a test so widening it is a
#   reviewable edit. Splitting that step would close this instance and read as
#   closing the class, which it would not.
#
#   That a target's recipe still runs the gate its NAME claims — PARTLY. Since
#   #reversereachdirection, a recipe repointed at a command run by some OTHER step is
#   refused; that half used to pass byte-identically and is now the gate-step
#   pin's case, below. What remains open is the recipe weakened INSIDE its own
#   pinned step: anchors match as subsequences and extra CI-side tokens are
#   allowed by design, so dropping `--no-fix` from `lint`'s recipe still matches
#   the `Lint (make lint)` step. Closing THAT needs a per-target recipe-content
#   anchor — a second spelling of every recipe, which is the mistake recorded in
#   HOW A TARGET IS MATCHED below: it cost lazily-cpp a hardcoded duplicate path
#   plus a hand-written equality assertion, a new drift surface invented to
#   satisfy a drift detector. Its churn would also be recipe-rate, which is how a
#   pin becomes the passes-when-stale check this family has already removed once.
#   So it stays out of scope, and it bounds the honest claim: for a gate REPLACED
#   the reviewer of the diff is no longer the only control, and for a gate
#   WEAKENED in place it still is.
#
# HOW A TARGET IS MATCHED
#
#   Recipes are read through `make -n`, so make variables are already expanded and
#   we compare real command lines rather than source text. `make -p` is
#   deliberately NOT used: it dumps the entire environment to stdout, which would
#   print every secret in the job's env into the CI log.
#
#   Each command is split on the shell's sequencing operators, redirections are
#   dropped, and the remainder is reduced to an ANCHOR: the program basename plus
#   its subcommands and flag NAMES (values dropped), with path arguments reduced to
#   basenames and bare path globs discarded. A target is reached when EVERY one of
#   its anchors is a subsequence of a command's token list IN THE CI STEP PINNED
#   FOR IT (#reversereachdirection), or when CI runs `make <target>` directly. Every, not
#   any: a target that runs two gates and is half-covered by CI is a gap, and
#   "any" would report it green.
#
#   The haystack is the pinned step, not the workflow. Step-scoping is strictly
#   stronger than the flat check, so it can red a state that is legitimate today —
#   a member whose anchors are spread across two CI steps would be one. Measured
#   here before committing: on a healthy tree the scoped and flat verdicts are
#   identical, byte for byte, and that equality is asserted as a standing test
#   rather than left as a note.
#
#   Keeping flag names in the anchor is what makes the guard falsifiable rather
#   than decorative: `go test -race` does not match a CI step that only runs
#   `go test -count=1`, so dropping the race job reddens this guard instead of
#   being absorbed by the plain test job.
#
#   An argument that is still a VARIABLE reference at this point — `$MANIFEST` in
#   a CI step, or a `$$VAR` a recipe leaves for the shell — names a value the
#   guard cannot resolve, so it becomes a WILDCARD matching exactly one token on
#   the other side (#lzcireachvaranchor). Make and CI routinely spell the same
#   path differently, one through an expanded `$(VAR)` and the other through the
#   environment, and they are the same command. Dropping the token instead, which
#   is what this used to do, lost the argument as well as its value and reported
#   a step that genuinely ran the gate as unreachable — a false RED that cost one
#   binding a hardcoded second spelling of the path plus a hand-written equality
#   assertion, which is a new drift surface invented to satisfy a guard whose job
#   is detecting drift. Arity still counts: `script.sh $A` does not match a CI
#   step that passes no argument at all.
#
#   Commands whose program is a shell builtin or a plain file/text utility carry no
#   gate, so they contribute no anchor. A target with no non-trivial command at all
#   (a mkdir-only reset step, say) is reported as carrying no gate and is not
#   required to appear in CI. It cannot fail a build, so it cannot hide one.
#
# THE EXCUSE LIST IS THE OTHER HALF OF THE DELIVERABLE
#
#   scripts/ci-reach.conf names the workflows that count and the targets that are
#   deliberately local-only, each with a reason. It is the one place a reader can
#   see what this binding does not enforce in CI, in the same spirit as
#   KNOWN_UNCOVERED. Excuses are checked in every direction the ledger can rot:
#   one with no reason is refused at parse time, one for a target CI turns out to
#   reach fails as stale, and one for a target outside the closure altogether
#   fails as unread (#pinreachclosure) — before that last check it was consulted
#   by nobody and counted by nothing, so the list could name targets that had
#   never been gated at all and still present itself as the record.
set -euo pipefail

MAKE_BIN="${MAKE:-make}"
ROOT_TARGET="${CI_REACH_ROOT_TARGET:-check}"
CONF="${CI_REACH_CONF:-scripts/ci-reach.conf}"

if [ ! -f Makefile ]; then
	echo "check-ci-reach: no Makefile in $(pwd)" >&2
	exit 1
fi

# ------------------------------------------------------------------- the closure pin

# WHICH targets CI must reach, pinned (#pinreachclosure).
#
# Everything below this line audits the closure of `$ROOT_TARGET`. Nothing used
# to pin WHAT that closure contains, and the hole was measured at exit 0 in five
# bindings including this one: drop `test-interop-peer` from `check:`'s
# prerequisite list and the guard printed
#
#   check-ci-reach: OK - 7 target(s) reached by CI, 0 excused, 1 carrying no gate
#
# and exited 0. The interop peer is the single cross-binding wire-compatibility
# gate and the original #lzinteroppeerci casualty; it left the build's
# obligations without being named, in a report whose only trace was a 7 where an
# 8 had been. Every remaining target was still genuinely reached, so no other
# rung here has an opinion: reachability is a property OF the members, and the
# membership was the thing that moved.
#
# This is a different failure from #lzgrepcpipefail, which the readability probes
# below catch. That one hides an UNREADABLE target — make refuses it, silence
# reads as an empty recipe. This one hides a REMOVED one, whose recipe is perfect
# and simply nobody's prerequisite. The pin is what makes that removal a
# reviewable edit instead of an invisible one: dropping a gate now costs two
# edits in the same diff, one of which says out loud that a gate was dropped.
#
# SET EQUALITY, not a count floor and not a ceiling. A floor passes a swap — drop
# `test-interop-peer`, add a cheap target, the count is 8 again. A ceiling
# self-disables: it starts with zero slack and gains some with every legitimate
# addition until the same attack fits underneath it. The property that has to
# hold is fails-when-stale, not passes-when-stale — the same reasoning that
# replaced `MAX_LEDGERED_BLOCKS` with `_EXPECTED_LEDGERED_BLOCKS` in
# tests/conformance_assert.py, and the reason the name here carries `EXPECTED_`.
#
# Sorted, de-duplicated, root included — checked below rather than asked for in a
# comment, so the array stays a canonical form a reader can diff at a glance.
EXPECTED_ROOT_TARGET="check"
EXPECTED_CLOSURE_TARGETS=(
	assertion-ordering-check
	check
	ci-reach
	conformance-coverage
	format-check
	lint
	test
	test-interop-peer
	type-check
)

# WHICH targets are allowed to carry no gate, pinned (#pinreachclosure).
#
# Keeping a target's NAME and neutering its recipe leaves membership perfectly
# intact, so EXPECTED_CLOSURE_TARGETS is satisfied and the oracle is satisfied
# too — the commands are gone from `make -n <target>` and from `make -n check`
# alike, so the subset holds trivially. What moves is the target's
# CLASSIFICATION: it stops being a target that must be reached and becomes one
# that "carries no gate", which is not required to appear in CI at all.
#
# Measured on this Makefile with `type-check:` reduced to `true`:
#
#   no gate  type-check                       recipe runs no checkable command
#   check-ci-reach: OK - 7 target(s) reached by CI, 0 excused, 2 carrying no gate
#
# — the SAME verdict line this script's header records as the #lzgrepcpipefail
# false green, reached by a completely different route. "Carrying no gate" is a
# real and necessary verdict (a mkdir-only reset step cannot fail a build, so it
# cannot hide one), which is exactly why it makes such a good hiding place: it is
# the one classification that excuses a target from every requirement here.
#
# So pin it. Today the only legitimate member is the root, which has
# prerequisites and no recipe of its own.
EXPECTED_NO_GATE_TARGETS=(
	check
)

# WHICH CI STEP runs each gate, pinned (#reversereachdirection).
#
# `anchor_reached` asks whether SOME command in a flat set of every `run:` body
# contains a target's anchors. "Some command anywhere in CI" is a weaker claim
# than it reads as, and it fails open in two directions:
#
#   THE REPOINT. Swap a target's recipe for a command CI already runs somewhere
#   and every other rung holds — membership unchanged, the oracle sees the new
#   command on both sides, the classification is still "carries a gate", the
#   anchor matches a real step. Measured on this Makefile: `type-check:` running
#   `uv run poe run_readme` (the `README examples` step, which no `check` member
#   runs and whose own comment says so) produced output BYTE-IDENTICAL to the
#   healthy run — `reached type-check` included — at exit 0, while `make -n check`
#   ran `poe ty` zero times. The type gate was gone and the guard said 8 reached.
#
#   THE SUPERSET. A NARROW step's command can be a superset of a BROAD target's
#   anchor, in which case deleting the broad target's OWN step leaves the guard
#   green — the anchor is still found, in a step that runs something else.
#   lazily-rs has this live: 8 of its 46 members are contained in another step's
#   command, and deleting its entire default-feature test job passed
#   byte-identically.
#
# Pinning the STEP NAME closes the repoint directly: the anchors have to be in
# THAT step, so a member repointed at another step's command, or at a step no
# member runs, exits 1. Churn is step-name-rate, not recipe-rate — a recipe
# gaining a flag moves the recipe and its CI step together, so this mapping does
# not move. That is the property a per-recipe-content pin lacked, and it is why
# that one was declined: its churn rate would have made it the passes-when-stale
# check this family has already removed once.
#
# THE SUPERSET SHAPE IS NOT LIVE HERE, and it was measured rather than argued.
# For each of the eight members, its anchor set is a subsequence of exactly ONE
# step's commands, and that step is a different one for each member — an eight-way
# bijection with no overlap. Demonstrated by consequence, not by the relation:
# deleting each member's own CI step in turn, one tree per member, reds this guard
# 8 times out of 8 and names the member. The tightest margin is ONE token, on six
# of the nine anchors: `type-check`'s whole anchor is `poe ty` (its recipe is the
# one in this Makefile that does not go through `uv run`), and the non-owning
# `README examples` step already matches one of those two tokens. So this is a
# clean state with no slack, not a structural immunity.
#
# ONE FIELD, not two, and the step name therefore has to be unambiguous. Step
# names are NOT unique family-wide (rs has 69 `run:` steps and 65 distinct names),
# so a bare name could resolve to two steps and silently match the wrong one. The
# resolution below refuses a pinned name that occurs in more than one (workflow,
# job) — with the remedy being to qualify it, not to pick one. Today every `run:`
# step in the one listed workflow has a distinct name, and all of them are named:
# the repo's only unnamed `run:` steps are the two in `build-sdist` in wheels.yml,
# which is not a listed workflow and reaches nothing.
#
# A MAKE-INVOCATION-REACHED TARGET IS REFUSED A PIN. Where CI's instruction is
# `make <target>`, there is no independent CI-side spelling to cross-check, so a
# step name pinned for it asserts nothing. This binding has none today — CI
# invokes make ZERO times, measured from `run:` bodies below — and the refusal is
# here so that switching CI to `make check` cannot turn eight live pins into eight
# decorations.
#
# Sorted by target, one step per target, verified below rather than asked for.
# `<target>` then whitespace then the step name, the same shape `excuse:` uses in
# the conf file.
EXPECTED_GATE_STEPS=(
	"assertion-ordering-check  Assertion observation ordering (#lzassertordering)"
	"ci-reach                  CI-reachability guard (#lzcheckcireachguard)"
	"conformance-coverage      Conformance coverage guard (make conformance-coverage)"
	"format-check              Format (make format)"
	"lint                      Lint (make lint)"
	"test                      Test (make test)"
	"test-interop-peer         Interop peer self-check (make test-interop-peer)"
	"type-check                Type check (make type-check)"
)

if [ "${#EXPECTED_GATE_STEPS[@]}" -eq 0 ]; then
	echo "check-ci-reach: EXPECTED_GATE_STEPS is empty, so reach is back to 'some CI" >&2
	echo "                command anywhere' for every target and a recipe repointed at" >&2
	echo "                another step's command passes (#reversereachdirection)." >&2
	exit 1
fi

gate_step_targets=()
gate_step_names=()
gate_step_wfs=()
gate_step_jobs=()
gate_step_idxs=()
for gs_entry in "${EXPECTED_GATE_STEPS[@]}"; do
	gs_t="${gs_entry%%[[:space:]]*}"
	gs_s="${gs_entry#"$gs_t"}"
	gs_s="$(printf '%s' "$gs_s" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
	if [ -z "$gs_t" ] || [ -z "$gs_s" ]; then
		echo "check-ci-reach: EXPECTED_GATE_STEPS entry '$gs_entry' is not '<target> <step name>'." >&2
		echo "                An entry with no step name pins no step, and the target it" >&2
		echo "                names would fall back to matching any CI command at all" >&2
		echo "                (#reversereachdirection)." >&2
		exit 1
	fi
	gate_step_targets+=("$gs_t")
	gate_step_names+=("$gs_s")
	gate_step_wfs+=("")
	gate_step_jobs+=("")
	gate_step_idxs+=("")
done

gate_step_pin_canonical="$(printf '%s\n' "${gate_step_targets[@]}" | LC_ALL=C sort -u)"
if [ "$gate_step_pin_canonical" != "$(printf '%s\n' "${gate_step_targets[@]}")" ]; then
	echo "check-ci-reach: EXPECTED_GATE_STEPS is not sorted by target, or names one target" >&2
	echo "                twice. A duplicate target means one of the two pins is dead and" >&2
	echo "                a reader cannot tell which; sorted order is what makes a" >&2
	echo "                one-line diff of this pin readable. Targets in canonical order:" >&2
	printf '%s\n' "$gate_step_pin_canonical" | sed 's/^/                  /' >&2
	exit 1
fi

gate_step_index_of() {
	local t="$1" i
	for i in "${!gate_step_targets[@]}"; do
		if [ "${gate_step_targets[$i]}" = "$t" ]; then
			printf '%s' "$i"
			return 0
		fi
	done
	return 1
}

# WHETHER THE WORKFLOW AND THE JOB RUN AT ALL, pinned (#verifyworkflowactually).
#
# This is the gap every pin above rested on. lazily-js measured it: a pinned gate
# step can exist, be unique, be unconditional and run the gate — inside a job or a
# workflow that never runs. All three of these left this guard at exit 0, with
# output byte-identical to a healthy tree:
#
#   job-level `if: false`                 the job is skipped; the steps are perfect
#   job-level `continue-on-error: true`   the job's failure stops failing the run
#   `on:` reduced to `workflow_dispatch`  nothing runs unless someone clicks it
#
# And scripts/ci-reach.conf's claim that precommit.yml "runs on every push to
# every branch" was a COMMENT — the one premise the whole audit rested on that
# nothing checked.
#
# VALUES, NOT ABSENCES. The tempting rule is "no job-level continue-on-error, no
# `if:`", and it is wrong: lazily-zig has a legitimate job-level
# `continue-on-error: ${{ matrix.zig == 'master' }}` for its advisory master leg,
# and an absence rule would false-red a correct config. So the four pins below are
# set-equalities over exact values, in both directions, which is the same
# fails-when-stale property as every other pin in this file. Absent is spelled
# `<absent>` rather than left as a missing row, so absent -> present is a CHANGED
# value with a name rather than a row a reader has to notice is gone.
#
#   EXPECTED_TRIGGERS          the exact top-level `on:` keys, per counted workflow
#   EXPECTED_TRIGGER_FILTERS   the exact sub-keys under each trigger and their
#                              values — `branches:`, `paths:`, `tags:`, `types:`,
#                              `branches-ignore:`, anything
#   EXPECTED_GATE_JOBS         the exact jobs holding the pinned gate steps
#   EXPECTED_GATE_JOB_GUARDS   per gate job, the exact `if:` and
#                              `continue-on-error:` values
#
# WHAT EACH ONE CATCHES, measured on this workflow before it existed — every row
# below was exit 0 with byte-identical output first:
#
#   `on:` -> workflow_dispatch only        TRIGGERS: the `push` row goes missing
#   `branches: ["**"]` -> `["main"]`       TRIGGER_FILTERS: value changed
#   a `paths:` filter introduced           TRIGGER_FILTERS: a surplus row. NO
#                                          BINDING HAS ONE TODAY, which is the
#                                          point — a `paths:` filter excluding
#                                          `Makefile` means a gate-retiring edit
#                                          does not even trigger the workflow that
#                                          would have caught it.
#   a gate step moved to another job       GATE_JOBS: the old job goes missing and
#                                          the new one is surplus (and the guard
#                                          rows move with it, so this edit is named
#                                          twice — both findings are true and
#                                          neither is suppressed)
#   job-level `if: false`                  GATE_JOB_GUARDS: `if=<absent>` ->
#                                          `if=false`
#   job-level `continue-on-error: true`    GATE_JOB_GUARDS: same, for that key
#
# THE MATRIX QUESTION, since it is the same defect one level down. This gate job
# runs under `strategy.matrix.python-version: ['3.12', '3.13', '3.14']` and NO leg
# is advisory — there is no job-level `continue-on-error` at all — so all three
# legs are blocking. A leg cannot be made advisory without a job-level
# `continue-on-error`, and GATE_JOB_GUARDS names that in the absent -> present
# direction, so "the only blocking leg was removed" is closed here as a
# consequence of the guard pin rather than by a matrix pin. What is NOT closed is
# NARROWING the matrix to one interpreter: that loses coverage without de-gating,
# and no pin here sees it. It is pinned as a standing test instead
# (tests/test_ci_reach_activation_pin.py), which is where this file's other
# measured-but-not-guarded facts live.
#
# WHAT THESE FOUR STILL DO NOT PROVE, and it is the one finding this work
# produced rather than closed: that the gate set runs on a PULL REQUEST. This is
# the family's only counted workflow with no `pull_request` trigger — see
# scripts/ci-reach.conf, which now states what is true instead of what was
# assumed. The trigger set is pinned, so it cannot change unnoticed; it is not
# CORRECTED, because changing `on:` is a behaviour change and not this guard's to
# make.
#
# Sorted, one row per fact, verified below rather than asked for in a comment.
# `<workflow>` then whitespace then the rest, the same shape EXPECTED_GATE_STEPS
# and `excuse:` use.
EXPECTED_TRIGGERS=(
	".github/workflows/precommit.yml  push"
	".github/workflows/precommit.yml  workflow_dispatch"
)

# `<none>` as the ONLY entry means "this workflow has no trigger filters at all",
# which is a legitimate state elsewhere in the family (`on: [push, pull_request]`)
# and is spelled explicitly so that an EMPTY array stays what it is everywhere
# else here: a pin somebody deleted.
EXPECTED_TRIGGER_FILTERS=(
	".github/workflows/precommit.yml  push  branches=**"
)

EXPECTED_GATE_JOBS=(
	".github/workflows/precommit.yml  precommit"
)

EXPECTED_GATE_JOB_GUARDS=(
	".github/workflows/precommit.yml  precommit  continue-on-error=<absent>"
	".github/workflows/precommit.yml  precommit  if=<absent>"
)

# Canonicalize a pin array or a discovered row set to `<f1> TAB <f2> [TAB <rest>]`
# so a hand-aligned array and a scraped row compare as the same fact. `want` is
# the field count: 2 for the two-field pins, 3 for the ones carrying a `key=value`.
# `rest` keeps its internal single spaces, and a row with too few fields is
# reported as BADPIN rather than silently truncated to a shorter, matchable one.
canon_rows() {
	awk -v want="$1" '
		{
			line = $0
			gsub(/^[[:space:]]+|[[:space:]]+$/, "", line)
			if (line == "") next
			n = split(line, t, /[[:space:]]+/)
			if (n < want) { print "BADPIN\t" line; next }
			if (want == 2) {
				if (n != 2) { print "BADPIN\t" line; next }
				print t[1] "\t" t[2]
				next
			}
			rest = t[3]
			for (i = 4; i <= n; i++) rest = rest " " t[i]
			print t[1] "\t" t[2] "\t" rest
		}
	'
}

# Every activation pin, refused the moment it is unreadable or empty. An empty one
# is refused by name for the KNOWN_UNCOVERED reason: a count of zero does not mean
# there is nothing to pin, it means nobody pinned anything — and here it would
# announce itself as OK in the only direction that matters, by reporting every
# discovered trigger as unpinned.
act_pin_problems=0
for act_pin_spec in \
	"EXPECTED_TRIGGERS 2" \
	"EXPECTED_TRIGGER_FILTERS 3" \
	"EXPECTED_GATE_JOBS 2" \
	"EXPECTED_GATE_JOB_GUARDS 3"; do
	act_pin_name="${act_pin_spec%% *}"
	act_pin_want="${act_pin_spec##* }"
	eval "act_pin_entries=(\"\${${act_pin_name}[@]+\"\${${act_pin_name}[@]}\"}\")"
	if [ "${#act_pin_entries[@]}" -eq 0 ]; then
		echo "check-ci-reach: $act_pin_name is empty, so this guard pins nothing about" >&2
		echo "                whether the audited workflow or its gate job runs at all —" >&2
		echo "                which is the state where 'on: workflow_dispatch' and a" >&2
		echo "                job-level 'if: false' are both green (#verifyworkflowactually)." >&2
		echo "                If the fact it pinned genuinely went away, say so in a row:" >&2
		echo "                a lone '<none>' for the filter pin, not an empty array." >&2
		act_pin_problems=$((act_pin_problems + 1))
		continue
	fi
	if [ "$act_pin_name" = "EXPECTED_TRIGGER_FILTERS" ] &&
		[ "${#act_pin_entries[@]}" -eq 1 ] &&
		[ "${act_pin_entries[0]}" = "<none>" ]; then
		continue
	fi
	act_pin_c="$(printf '%s\n' "${act_pin_entries[@]}" | canon_rows "$act_pin_want")"
	if printf '%s\n' "$act_pin_c" | grep -q '^BADPIN	'; then
		echo "check-ci-reach: $act_pin_name entr(ies) this guard cannot parse:" >&2
		printf '%s\n' "$act_pin_c" | sed -n 's/^BADPIN\t/  - /p' >&2
		echo "                A row with fewer than $act_pin_want fields pins a shorter fact" >&2
		echo "                than it reads as, and the set-equality below would compare it" >&2
		echo "                against nothing (#verifyworkflowactually)." >&2
		act_pin_problems=$((act_pin_problems + 1))
		continue
	fi
	act_pin_sorted="$(printf '%s\n' "$act_pin_c" | LC_ALL=C sort -u)"
	if [ "$act_pin_sorted" != "$(printf '%s\n' "$act_pin_c")" ]; then
		echo "check-ci-reach: $act_pin_name is not in canonical form — sort it and remove" >&2
		echo "                duplicates. A duplicate is how a hand-merge keeps a row it" >&2
		echo "                meant to replace, and unsorted rows turn a one-line diff of" >&2
		echo "                this pin into a puzzle. Canonical form is:" >&2
		printf '%s\n' "$act_pin_sorted" | sed 's/\t/  /g; s/^/                  /' >&2
		act_pin_problems=$((act_pin_problems + 1))
		continue
	fi
done

if [ "$act_pin_problems" -gt 0 ]; then
	exit 1
fi

# An empty pin would make the set-equality check below vacuous in the only
# direction that matters, and it would announce itself as OK: an empty pin can
# only mismatch by reporting every discovered target as unpinned, which reads
# like a configuration accident rather than a missing pin. Refuse it by name,
# for the KNOWN_UNCOVERED reason — a count of zero does not mean there is nothing
# to pin, it means nobody pinned anything.
if [ "${#EXPECTED_CLOSURE_TARGETS[@]}" -eq 0 ]; then
	echo "check-ci-reach: EXPECTED_CLOSURE_TARGETS is empty, so this guard pins no" >&2
	echo "                membership at all and any prerequisite could be deleted" >&2
	echo "                without a word (#pinreachclosure)." >&2
	exit 1
fi

pin_canonical="$(printf '%s\n' "${EXPECTED_CLOSURE_TARGETS[@]}" | LC_ALL=C sort -u)"
if [ "$pin_canonical" != "$(printf '%s\n' "${EXPECTED_CLOSURE_TARGETS[@]}")" ]; then
	echo "check-ci-reach: EXPECTED_CLOSURE_TARGETS is not in canonical form — sort it and" >&2
	echo "                remove duplicates. A duplicate is how a hand-merge of two" >&2
	echo "                closure edits keeps a name it meant to replace, and unsorted" >&2
	echo "                entries turn a one-line diff of this pin into a puzzle." >&2
	echo "                Canonical form is:" >&2
	printf '%s\n' "$pin_canonical" | sed 's/^/                  /' >&2
	exit 1
fi

# The ROOT is pinned too, because every set below is derived from it. Renaming
# `check` — or pointing this guard at a smaller target through the environment —
# shrinks the audited closure without touching a single reachability verdict.
# Measured on this binding at exit 0: `CI_REACH_ROOT_TARGET=lint` printed
# `OK - 1 target(s) reached by CI, 0 excused, 0 carrying no gate` over a Makefile
# whose `check` still ran eight gates.
#
# A rename in the Makefile ALONE is already caught, by the dry-run gate further
# down (`make -n check` then fails outright). This catches the other half: the
# guard being aimed somewhere else while the Makefile is untouched.
if [ "$ROOT_TARGET" != "$EXPECTED_ROOT_TARGET" ]; then
	echo "check-ci-reach: auditing '$ROOT_TARGET', but this guard is pinned to" >&2
	echo "                '$EXPECTED_ROOT_TARGET' (EXPECTED_ROOT_TARGET in $0)." >&2
	echo "                Every verdict below would describe a closure nobody pinned," >&2
	echo "                and a smaller root reports OK with fewer gates required." >&2
	echo "                  * to audit the real root, drop CI_REACH_ROOT_TARGET." >&2
	echo "                  * if '$ROOT_TARGET' really is the root now, move" >&2
	echo "                    EXPECTED_ROOT_TARGET and EXPECTED_CLOSURE_TARGETS with it," >&2
	echo "                    in the same commit as the Makefile rename." >&2
	exit 1
fi

# ---------------------------------------------------------------- configuration

workflows=()
workflow_count=0
excused_targets=()
excused_reasons=()
excuse_count=0

if [ -f "$CONF" ]; then
	while IFS= read -r line || [ -n "$line" ]; do
		line="${line%%$'\r'}"
		case "$line" in
		'#'* | '') continue ;;
		esac
		key="${line%%:*}"
		val="${line#*:}"
		val="$(printf '%s' "$val" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
		case "$key" in
		workflow)
			workflows+=("$val")
			workflow_count=$((workflow_count + 1))
			;;
		excuse)
			tgt="${val%%[[:space:]]*}"
			reason="${val#"$tgt"}"
			reason="$(printf '%s' "$reason" | sed -e 's/^[[:space:]]*//')"
			if [ -z "$reason" ]; then
				echo "check-ci-reach: excuse for '$tgt' has no reason — an excuse without a reason is not an excuse" >&2
				exit 1
			fi
			excused_targets+=("$tgt")
			excused_reasons+=("$reason")
			excuse_count=$((excuse_count + 1))
			;;
		*)
			echo "check-ci-reach: unknown key '$key' in $CONF" >&2
			exit 1
			;;
		esac
	done <"$CONF"
fi

if [ "$workflow_count" -eq 0 ]; then
	workflows=(".github/workflows/ci.yml")
	workflow_count=1
fi

for wf in "${workflows[@]}"; do
	if [ ! -f "$wf" ]; then
		echo "check-ci-reach: workflow '$wf' listed in $CONF does not exist" >&2
		exit 1
	fi
done

# ------------------------------------------------------- make target extraction

# A Makefile may set .RECIPEPREFIX to something other than tab (lazily-rs uses
# `>`), which puts recipe lines at column 0 where a rule line lives. Without this
# a recipe such as `>cargo test --features a:b` reads as a rule named `>cargo`.
RECIPE_PREFIX="$(awk -F= '/^[[:space:]]*\.RECIPEPREFIX[[:space:]]*[:+]?=/ {
	v = $2; gsub(/^[[:space:]]+|[[:space:]]+$/, "", v); if (v != "") print substr(v, 1, 1); exit
}' Makefile)"

# Prerequisites of a target, straight from the Makefile source, with `\`
# continuations joined and trailing comments removed. Order-only prerequisites are
# dropped: they constrain ordering, not what runs.
prereqs_of() {
	awk -v target="$1" -v rp="$RECIPE_PREFIX" '
		BEGIN { pat = "^" target ":([^=]|$)"; if (rp == "") rp = "\t" }
		{
			line = $0
			# Only the ACTUAL recipe prefix marks a recipe line. Treating any
			# leading whitespace as one loses a rule that is merely indented,
			# which under a non-tab .RECIPEPREFIX is perfectly legal make and
			# collapses the whole closure to a single target. A continuation is
			# exempt: under the default tab prefix a wrapped prerequisite list is
			# normally tab-indented.
			if (!cont && substr(line, 1, 1) == rp) next
			sub(/^[[:space:]]+/, "", line)
			if (cont) {
				buf = buf " " line
				if (line ~ /\\[[:space:]]*$/) next
				cont = 0
				emit(buf)
				exit
			}
			if (line !~ pat) next
			buf = line
			if (line ~ /\\[[:space:]]*$/) { cont = 1; next }
			emit(buf)
			exit
		}
		function emit(s,   rest, n, i, parts) {
			gsub(/\\/, " ", s)
			sub(/#.*$/, "", s)
			rest = substr(s, index(s, ":") + 1)
			sub(/\|.*$/, "", rest)
			n = split(rest, parts, /[[:space:]]+/)
			for (i = 1; i <= n; i++) if (parts[i] != "") print parts[i]
		}
	' Makefile
}

# Is this name an explicit rule in the Makefile?
is_makefile_target() {
	awk -v target="$1" -v rp="$RECIPE_PREFIX" '
		BEGIN { pat = "^" target ":([^=]|$)"; if (rp == "") rp = "\t"; found = 0 }
		substr($0, 1, 1) == rp { next }
		{ line = $0; sub(/^[[:space:]]+/, "", line) }
		line ~ pat { found = 1; exit }
		END { exit found ? 0 : 1 }
	' Makefile
}

# ------------------------------------------------------------- make sanity gate

# Every verdict below is derived from `make -n`, and `dry_run` reads its output
# through `... | grep -v ... || true` (#lzgrepcpipefail). That `|| true` is
# CORRECT for the grep: a recipe whose every line is make's own chatter leaves
# the filter with nothing to print, and zero commands is a legitimate
# measurement here — it is the "carrying no gate" verdict. It is also
# indiscriminate. make's OWN failure exits nonzero through the same path,
# `2>/dev/null` hides what it said, and the empty stdout is then read as "this
# recipe runs no checkable command": the target drops out of the reach
# requirement and the guard reports OK.
#
# Measured, not supposed. Give `test:` a prerequisite with no rule
# (`test: build/generated-fixtures.json`). `make -n test` exits 2 with
# "No rule to make target ... needed by 'test'", and this guard printed
#
#   no gate  test      recipe runs no checkable command
#   check-ci-reach: OK - 7 target(s) reached by CI, 0 excused, 2 carrying no gate
#
# and exited 0. The pytest target — which carries conformance rungs 2-4 — had
# quietly stopped being required in CI, and the run was green, not fail-closed.
#
# The check belongs HERE rather than inside `dry_run`, because `dry_run` is
# called from command substitutions and pipelines (`$(dry_run ... | wc -l)`)
# where it runs in a SUBSHELL: an `exit 1` there kills the subshell and the
# script carries on with the empty output it was trying to refuse. This runs in
# the main shell, on the ROOT target, whose prerequisite closure is exactly the
# set every `dry_run` below asks about — so if make can dry-run the root it can
# dry-run any part of it, and the `|| true` downstream is then measuring the
# grep and nothing else.
#
# No temp file: a second `trap ... EXIT` would REPLACE the one the anchor files
# install further down, so make's diagnostic is captured into a variable.
# `2>&1 >/dev/null` in that order keeps stderr and discards the recipe dump.
# `command -v` FIRST, above every use. A tool-availability guard below its own
# first use is dead code, and both spellings of this one were measured with
# `make` off PATH (`MAKE=definitely-not-make`):
#
#   * before the gate below existed, `dry_run` swallowed the 127 for EVERY
#     target and the run reached the vacuity floor — "'check' has no
#     prerequisite target carrying a gate — nothing was verified", exit 1. Fail
#     closed, but it names the Makefile when the cause is the PATH.
#   * with the gate below but no check here, it printed "make said:" followed by
#     bash's own `definitely-not-make: command not found` — a quote attributed
#     to a process that never started.
#
# A missing interpreter and an interpreter that ran and found nothing are
# different findings; they must not share a diagnostic. This one carries its own
# name and its own remedy.
if ! command -v "$MAKE_BIN" >/dev/null 2>&1; then
	echo "check-ci-reach: no '$MAKE_BIN' on PATH, so every verdict below would be derived" >&2
	echo "                from a program that never ran. This guard IS a make consumer —" >&2
	echo "                it dry-runs '$ROOT_TARGET' to read each recipe back out of make." >&2
	echo "                Install make, or name yours:" >&2
	echo "                  MAKE=/path/to/make $0" >&2
	exit 1
fi

make_dry_err=""
if ! make_dry_err="$("$MAKE_BIN" -n "$ROOT_TARGET" 2>&1 >/dev/null)"; then
	echo "check-ci-reach: '$MAKE_BIN -n $ROOT_TARGET' FAILED, so every recipe below would" >&2
	echo "                read as empty and every target would be reported as carrying no" >&2
	echo "                gate — a broken Makefile announced as OK (#lzgrepcpipefail)." >&2
	echo "                $MAKE_BIN said:" >&2
	printf '%s\n' "$make_dry_err" | sed 's/^/                  /' >&2
	exit 1
fi

# Breadth-first closure of ROOT_TARGET's prerequisites, parents before children.
closure=""
queue="$ROOT_TARGET"
seen=" "
while [ -n "$queue" ]; do
	current="${queue%%$'\n'*}"
	if [ "$current" = "$queue" ]; then queue=""; else queue="${queue#*$'\n'}"; fi
	[ -n "$current" ] || continue
	case "$seen" in
	*" $current "*) continue ;;
	esac
	seen="$seen$current "
	closure="$closure$current"$'\n'
	while IFS= read -r dep; do
		[ -n "$dep" ] || continue
		if is_makefile_target "$dep"; then
			queue="$queue$dep"$'\n'
		fi
	done < <(prereqs_of "$current")
done

# ------------------------------------------------ per-target readability probe

# The root probe above is NOT sufficient, and the gap is not hypothetical: five
# bindings have now reproduced it. `make -n $ROOT_TARGET` can exit 0 while
# `make -n <member>` exits 2, because `MAKECMDGOALS` differs between the two
# invocations and a prerequisite can be added conditionally on it:
#
#     ifeq ($(MAKECMDGOALS),type-check)
#     type-check: only-when-type-check-is-the-goal
#     endif
#
# Measured on this binding's own Makefile: `make -n check` exits 0 (the
# conditional is false when `check` is the goal), `make -n type-check` exits 2
# with "No rule to make target 'only-when-type-check-is-the-goal'". Against the
# root probe alone the false green SURVIVED — `no gate type-check`,
# "OK - 7 target(s) reached by CI, 0 excused, 2 carrying no gate", exit 0 —
# because `dry_run`'s `|| true` swallows the member's 2 and silence reads as an
# empty recipe. `own_commands` asks make one target at a time, so the probe has
# to as well.
#
# Both probes stay, and they answer different questions: the root probe names
# the cause when the root itself is unbuildable (an include, a `$(error)`, a
# missing tool), and prints make's stderr verbatim. This one finds the member
# that drops out while the root stays green.
#
# Main shell, for the reason the root probe is here: `dry_run` is reached
# through `$(dry_run ... | wc -l)` and `$(own_commands ... | anchors ...)`, so an
# `exit 1` inside it dies with the subshell. `dry_run "${deps[@]}"` — the
# multi-goal call — is deliberately NOT probed: when IT fails, `prefix` becomes
# 0 and the target is credited with its prerequisites' anchors too, which
# over-reports and fails closed. It is the single-target call whose failure
# turns into silence.
unreadable=" "
unreadable_targets=()
unreadable_errors=()
while IFS= read -r target; do
	[ -n "$target" ] || continue
	probe_err=""
	if ! probe_err="$("$MAKE_BIN" -n "$target" 2>&1 >/dev/null)"; then
		unreadable="$unreadable$target "
		unreadable_targets+=("$target")
		unreadable_errors+=("$(printf '%s' "$probe_err" | tr '\n' ' ')")
	fi
done <<<"$closure"

# Mirrors `excuse_reason`: a lookup whose only call site is a `printf` argument
# after membership is already established, so its no-match return status is
# never the thing anyone reads.
unreadable_reason() {
	local t="$1" i
	for i in "${!unreadable_targets[@]}"; do
		if [ "${unreadable_targets[$i]}" = "$t" ]; then
			printf '%s' "${unreadable_errors[$i]}"
			return
		fi
	done
}

# `make -n` for a target emits its prerequisites' commands first, then its own.
# Asking make for the prerequisite list alone yields exactly that prefix — make
# applies the same de-duplication to both invocations — so removing it leaves the
# target's own recipe. Diagnostics make writes about targets it has nothing to do
# for are not commands and are dropped.
# A recipe line broken across physical lines with `\` reaches the shell as ONE
# command, and make -n prints it the way the Makefile spells it. Joining here is
# what keeps `VAR=x \` + `go test ./...` from being read as two commands, the
# second of which is where the whole gate lives.
join_continuations() {
	awk '
		{
			line = $0
			if (line ~ /\\[[:space:]]*$/) {
				sub(/\\[[:space:]]*$/, "", line)
				buf = buf line " "
				next
			}
			print buf line
			buf = ""
		}
		END { if (buf != "") print buf }
	'
}

dry_run() {
	"$MAKE_BIN" -n "$@" 2>/dev/null | grep -v -e '^make\[' -e '^make:' | join_continuations || true
}

own_commands() {
	local target="$1"
	local deps=()
	local dep_count=0
	while IFS= read -r dep; do
		[ -n "$dep" ] || continue
		if is_makefile_target "$dep"; then
			deps+=("$dep")
			dep_count=$((dep_count + 1))
		fi
	done < <(prereqs_of "$target")

	if [ "$dep_count" -eq 0 ]; then
		dry_run "$target"
		return
	fi
	local prefix
	prefix="$(dry_run "${deps[@]}" | wc -l)"
	dry_run "$target" | tail -n +"$((prefix + 1))"
}

# ------------------------------------------------------------- workflow scraping

# Command lines from every `run:` step, each labelled with the workflow, the job
# and the STEP NAME that runs it. Comment lines inside a run body are stripped
# here — the whole reason this guard is a script.
#
# The label is what makes `EXPECTED_GATE_STEPS` possible (#reversereachdirection). There
# used to be a second, unlabelled scraper here; the flat command stream every
# other rung reads is now `cut -f4-` of this one, so there is a single definition
# of "what CI runs" rather than two that can disagree about it.
#
# Three shapes of indentation are being tracked at once, and conflating them is
# how a scraper starts inventing or losing commands:
#
#   * the JOB key indent, learned from the first key under `jobs:` rather than
#     assumed to be two spaces.
#   * the STEP key indent, which is the sequence marker's indent plus two. `name:`
#     and `run:` are only read at that exact indent, so the `name:` inside an
#     `actions/upload-artifact` `with:` block is not mistaken for a step name and
#     a `run:` nested inside `with:` is not mistaken for a step's command.
#   * the BLOCK indent of a `run: |` body, which is the indent of the `run:` key
#     AS WRITTEN — `- run: |` and a `run: |` under a `name:` sit at different
#     columns, and using the step key indent for both would swallow the sibling
#     keys of the former.
ci_step_commands() {
	awk '
		# Commands are BUFFERED to the end of their step, then flushed with the
		# step name and the step guards attached. Printing them as they are read
		# cannot work: `name:`, `if:` and `continue-on-error:` may appear AFTER
		# `run:` in the same step, and a record printed before them would carry a
		# name and a guard state that are not the step is final ones.
		function emit(text) { ncmd++; cmd[ncmd] = text }
		function flushblock() { if (buf != "") { emit(buf); buf = "" } }
		function flushstep(   k, g) {
			flushblock()
			g = ""
			if (has_if) g = "if"
			if (has_coe) g = (g == "" ? "continue-on-error" : g ",continue-on-error")
			for (k = 1; k <= ncmd; k++)
				print wf "\t" job "\t" sidx "\t" step "\t" g "\t" cmd[k]
			ncmd = 0; has_if = 0; has_coe = 0
		}
		FNR == 1 {
			flushstep()
			wf = FILENAME
			injobs = 0; job = ""; job_indent = -1
			step = ""; step_key_indent = -1; sidx = 0
			inblock = 0
		}
		{
			line = $0
			indent = match(line, /[^ ]/) - 1
			if (indent < 0) indent = 9999

			if (inblock) {
				if (line ~ /^[[:space:]]*$/) next
				if (indent <= block_indent) { flushblock(); inblock = 0 }
				else {
					sub(/^[[:space:]]+/, "", line)
					if (substr(line, 1, 1) == "#") next
					if (line ~ /\\[[:space:]]*$/) {
						sub(/\\[[:space:]]*$/, "", line)
						buf = buf " " line
						next
					}
					if (buf != "") { emit(buf " " line); buf = "" }
					else emit(line)
					next
				}
			}

			# A top-level key ends whatever job context was open, and only `jobs:`
			# opens a new one. Without this, `permissions:` or a trailing top-level
			# key would leave the scraper reading later mappings as jobs.
			if (line ~ /^[^[:space:]#].*:[[:space:]]*$/) {
				flushstep()
				injobs = (line ~ /^jobs:[[:space:]]*$/)
				job = ""; job_indent = -1; step = ""; step_key_indent = -1
				next
			}

			if (injobs && line ~ /^[[:space:]]*[A-Za-z0-9_.-]+:[[:space:]]*$/) {
				if (job_indent < 0) job_indent = indent
				if (indent == job_indent) {
					flushstep()
					job = line
					sub(/^[[:space:]]*/, "", job)
					sub(/:[[:space:]]*$/, "", job)
					step = ""; step_key_indent = -1; sidx = 0
					next
				}
			}

			# A sequence item inside a job starts a new step. Guarded by `job`
			# because the `on:` block has sequences of its own (`branches:`,
			# `paths:`) and they are not steps.
			raw_indent = indent
			if (job != "" && line ~ /^[[:space:]]*-([[:space:]]|$)/) {
				flushstep()
				step = ""
				sidx++
				step_key_indent = indent + 2
				sub(/^[[:space:]]*-[[:space:]]*/, "", line)
				indent = step_key_indent
			}

			if (indent != step_key_indent) next

			if (line ~ /^[[:space:]]*name:[[:space:]]/) {
				s = line
				sub(/^[[:space:]]*name:[[:space:]]*/, "", s)
				sub(/[[:space:]]+$/, "", s)
				if (s ~ /^".*"$/ || s ~ /^'"'"'.*'"'"'$/) s = substr(s, 2, length(s) - 2)
				step = s
				next
			}
			# A step whose execution is CONDITIONAL, or whose failure is
			# discarded, is recorded so a pin for it can be refused
			# (#reversereachdirection). Value ignored for `if:` — this guard
			# cannot evaluate a GitHub expression, and a step it cannot prove
			# runs is not reach. `continue-on-error: false` is the default and
			# is not a guard.
			if (line ~ /^[[:space:]]*if:/) { has_if = 1; next }
			if (line ~ /^[[:space:]]*continue-on-error:[[:space:]]*("|'"'"')?true/) {
				has_coe = 1
				next
			}
			if (line ~ /^[[:space:]]*run:[[:space:]]*[|>][-+]?[[:space:]]*$/) {
				inblock = 1
				block_indent = raw_indent
				buf = ""
				next
			}
			if (line ~ /^[[:space:]]*run:[[:space:]]*[^|>[:space:]]/) {
				sub(/^[[:space:]]*run:[[:space:]]*/, "", line)
				emit(line)
			}
		}
		END { flushstep() }
	' "$@"
}

# The top-level `on:` block of each counted workflow, as rows
# (#verifyworkflowactually):
#
#   trigger  <workflow>  <trigger>                    one per `on:` key
#   filter   <workflow>  <trigger>  <subkey>=<v1,v2>  one per sub-key under one
#
# Three `on:` spellings are read — `on: push`, `on: [push, pull_request]`, and the
# block mapping this repo uses — and anything else emits an UNPARSED row, which
# the caller treats as a hard failure. That direction matters more than the
# coverage: a scraper that silently produced no triggers for a shape it did not
# understand would report the pinned set as entirely missing, which reads like a
# deleted `on:` block, or — worse, if the pin were ever emptied — like agreement.
#
# Sub-key values are collected from an inline flow list, a bare scalar, or block
# sequence items, and a nested MAPPING under a sub-key (`workflow_dispatch:
# inputs:`) becomes the literal `<mapping>` so it has to be pinned by hand rather
# than flattened into something that looks like a filter value.
#
# Trailing `#` comments are stripped only from lines carrying no quote character,
# so `branches: ["**"]  # every branch` is left intact rather than truncated at a
# `#` that might have been inside the string.
wf_activation() {
	awk '
		function flushvals() {
			if (trig != "" && sub_k != "") {
				key = wf SUBSEP trig SUBSEP sub_k
				V[key] = vals
			}
			sub_k = ""; vals = ""
		}
		function addval(v) {
			gsub(/^[[:space:]]+|[[:space:]]+$/, "", v)
			if (v ~ /^".*"$/ || v ~ /^'"'"'.*'"'"'$/) v = substr(v, 2, length(v) - 2)
			if (v == "") return
			vals = (vals == "" ? v : vals "," v)
		}
		function addflow(s,   n, i, parts) {
			sub(/^\[/, "", s); sub(/\][[:space:]]*$/, "", s)
			n = split(s, parts, /,/)
			for (i = 1; i <= n; i++) addval(parts[i])
		}
		function newtrig(t) {
			flushvals()
			trig = t
			T[wf SUBSEP trig] = 1
			trig_sub_indent = -1
		}
		FNR == 1 {
			flushvals()
			wf = FILENAME
			seen_on[wf] = 0
			inon = 0; trig = ""; trig_indent = -1; trig_sub_indent = -1; vals = ""; sub_k = ""
		}
		/^[[:space:]]*#/ { next }
		/^[[:space:]]*$/ { next }
		{
			line = $0
			indent = match(line, /[^ ]/) - 1
			body = line
			sub(/^[[:space:]]+/, "", body)
			sub(/[[:space:]]+$/, "", body)
			# Trailing comments are stripped ONLY on lines carrying no quote, so
			# a `branches: ["**"]  # note` is left alone rather than truncated at
			# a `#` that might be inside a string.
			if (body !~ /["'"'"']/) sub(/[[:space:]]+#.*$/, "", body)
		}
		indent == 0 {
			flushvals()
			trig = ""; trig_indent = -1; trig_sub_indent = -1
			key = body
			sub(/:.*$/, "", key)
			gsub(/^["'"'"']|["'"'"']$/, "", key)
			if (key != "on") { inon = 0; next }
			inon = 1
			seen_on[wf] = 1
			rest = body
			sub(/^[^:]*:[[:space:]]*/, "", rest)
			if (rest == "") next
			# `on: push` and `on: [push, pull_request]`, the two one-line forms.
			if (rest ~ /^\[/) {
				n = split(rest, p, /,/)
				sub(/^\[/, "", p[1]); sub(/\][[:space:]]*$/, "", p[n])
				for (i = 1; i <= n; i++) {
					v = p[i]
					gsub(/^[[:space:]]+|[[:space:]]+$/, "", v)
					gsub(/^["'"'"']|["'"'"']$/, "", v)
					if (v != "") T[wf SUBSEP v] = 1
				}
			} else {
				gsub(/^["'"'"']|["'"'"']$/, "", rest)
				T[wf SUBSEP rest] = 1
			}
			inon = 0
			next
		}
		!inon { next }
		{
			if (trig_indent < 0) trig_indent = indent
			if (indent < trig_indent) { inon = 0; next }
			if (indent == trig_indent) {
				if (body !~ /^[A-Za-z_][A-Za-z0-9_-]*:/) {
					printf("UNPARSED\t%s\t%s\n", wf, body)
					next
				}
				t = body
				sub(/:.*$/, "", t)
				newtrig(t)
				rest = body
				sub(/^[^:]*:[[:space:]]*/, "", rest)
				if (rest != "") printf("UNPARSED\t%s\t%s\n", wf, body)
				next
			}
			# Deeper than the trigger key: a filter sub-key, its inline value, or
			# a block-sequence item belonging to the sub-key above it.
			if (trig_sub_indent < 0) trig_sub_indent = indent
			if (body ~ /^-([[:space:]]|$)/) {
				if (sub_k == "") {
					printf("UNPARSED\t%s\t%s\n", wf, body)
					next
				}
				v = body
				sub(/^-[[:space:]]*/, "", v)
				addval(v)
				next
			}
			if (body !~ /^[A-Za-z_][A-Za-z0-9_-]*:/) {
				printf("UNPARSED\t%s\t%s\n", wf, body)
				next
			}
			if (indent > trig_sub_indent) {
				# A nested MAPPING under a filter sub-key. Not modelled as a value
				# list; recorded as <mapping> so it has to be pinned by hand.
				vals = "<mapping>"
				next
			}
			flushvals()
			sub_k = body
			sub(/:.*$/, "", sub_k)
			rest = body
			sub(/^[^:]*:[[:space:]]*/, "", rest)
			if (rest ~ /^\[/) { addflow(rest); flushvals(); next }
			if (rest != "") { addval(rest); flushvals(); next }
			next
		}
		END {
			flushvals()
			for (k in T) {
				split(k, a, SUBSEP)
				printf("trigger\t%s\t%s\n", a[1], a[2])
			}
			for (k in V) {
				split(k, a, SUBSEP)
				printf("filter\t%s\t%s\t%s=%s\n", a[1], a[2], a[3], V[k])
			}
			for (f in seen_on) if (seen_on[f] == 0) printf("NOON\t%s\n", f)
		}
	' "$@"
}

# Job-level `if:` and `continue-on-error:`, as `<workflow> <job> <key> <value>`
# (#verifyworkflowactually).
#
# JOB-level, not step-level, and the difference is the whole point: the step map
# above already refuses a pinned STEP carrying either, and a job-level one is
# invisible to it while having exactly the same effect on every step inside. The
# separation is by indentation — the first key indent inside each job is LEARNED
# rather than assumed to be four spaces, and step keys sit deeper — so a step's
# own `if: always()` is not mistaken for the job's.
#
# The VALUE is carried verbatim, because the pin is a value and not an absence:
# lazily-zig's advisory master leg is a legitimate job-level
# `continue-on-error: ${{ matrix.zig == 'master' }}`, and a rule that refused any
# job-level guard would false-red it.
wf_job_guards() {
	awk '
		FNR == 1 { wf = FILENAME; injobs = 0; job = ""; job_indent = -1; jobkey_indent = -1 }
		/^[[:space:]]*#/ { next }
		/^[[:space:]]*$/ { next }
		{
			indent = match($0, /[^ ]/) - 1
			body = $0
			sub(/^[[:space:]]+/, "", body)
			sub(/[[:space:]]+$/, "", body)
		}
		indent == 0 {
			injobs = (body ~ /^jobs:[[:space:]]*$/)
			job = ""; job_indent = -1; jobkey_indent = -1
			next
		}
		!injobs { next }
		{
			if (job_indent < 0 && body ~ /^[A-Za-z0-9_.-]+:[[:space:]]*$/) job_indent = indent
			if (indent == job_indent && body ~ /^[A-Za-z0-9_.-]+:[[:space:]]*$/) {
				job = body
				sub(/:[[:space:]]*$/, "", job)
				jobkey_indent = -1
				next
			}
			if (job == "") next
			if (indent <= job_indent) next
			if (jobkey_indent < 0) jobkey_indent = indent
			if (indent != jobkey_indent) next
			if (body ~ /^if:/) {
				v = body; sub(/^if:[[:space:]]*/, "", v)
				printf("%s\t%s\tif\t%s\n", wf, job, v)
			}
			if (body ~ /^continue-on-error:/) {
				v = body; sub(/^continue-on-error:[[:space:]]*/, "", v)
				printf("%s\t%s\tcontinue-on-error\t%s\n", wf, job, v)
			}
		}
	' "$@"
}

# ------------------------------------------------------------------- normalizing

# Reduce command text to anchors, one per line, each a space-separated token list.
anchors() {
	awk '
		BEGIN {
			# Sentinel for an unresolvable variable reference. Deliberately not a
			# string any real argument can be.
			ANY = "\001any"
			split(": true false echo printf cd pushd popd mkdir rmdir rm cp mv ln touch " \
			      "export unset set local read eval exec trap wait sleep exit return " \
			      "if then else elif fi for while until do done case esac function " \
			      "test [ [[ pwd ls cat head tail sed awk grep egrep fgrep sort uniq " \
			      "wc tr cut paste tee xargs env dirname basename date git", t, / /)
			for (i in t) if (t[i] != "") trivial[t[i]] = 1
		}
		{
			n = split(split_unquoted($0), cmds, /\n/)
			for (i = 1; i <= n; i++) emit(cmds[i])
		}
		# Split on the shell'"'"'s sequencing operators, but ONLY outside quotes. Doing
		# this before quotes are stripped is what stops a `;` inside a message —
		# `echo "missing $(DIR); clone the sibling"` — from being read as a second
		# command and inventing an anchor for a gate that does not exist. That is a
		# false RED, so it costs a real target its verdict.
		function split_unquoted(s,   i, c, nxt, len, inq, q, out) {
			out = ""; inq = 0; q = ""; len = length(s)
			for (i = 1; i <= len; i++) {
				c = substr(s, i, 1)
				if (inq) {
					if (c == q) { inq = 0; q = "" }
					out = out c
					continue
				}
				if (c == "\"" || c == "'"'"'" || c == "`") { inq = 1; q = c; out = out c; continue }
				nxt = substr(s, i + 1, 1)
				if (c == ";") { out = out "\n"; continue }
				if ((c == "&" && nxt == "&") || (c == "|" && nxt == "|")) { out = out "\n"; i++; continue }
				if (c == "|") { out = out "\n"; continue }
				out = out c
			}
			return out
		}
		function emit(cmd,   m, j, tok, out, prog, started, parts) {
			gsub(/[`"'"'"']/, " ", cmd)
			gsub(/\$\(/, " ", cmd)
			gsub(/\$\{/, " ", cmd)
			gsub(/[(){}]/, " ", cmd)
			m = split(cmd, parts, /[[:space:]]+/)
			prog = ""
			out = ""
			started = 0
			for (j = 1; j <= m; j++) {
				tok = parts[j]
				if (tok == "" || tok == "\\") continue
				if (tok ~ /^[0-9]*>>?$/ || tok == "<" || tok ~ /^[0-9]+>&[0-9]+$/) break
				if (!started) {
					if (tok ~ /^[A-Za-z_][A-Za-z0-9_]*=/) continue
					started = 1
					prog = tok
					sub(/.*\//, "", prog)
					if (prog == "" || (prog in trivial)) return
					out = prog
					continue
				}
				if (tok ~ /^-/) {
					sub(/=.*$/, "", tok)
					out = out " " tok
					continue
				}
				if (tok ~ /^\.{1,3}$/ || tok ~ /^\.{1,2}\/\.{0,3}$/) continue
				if (tok ~ /\//) {
					sub(/\/+$/, "", tok)
					sub(/.*\//, "", tok)
					if (tok == "" || tok ~ /^\.{1,3}$/) continue
				}
					# A token that is still a shell/make VARIABLE reference names a
					# value this guard cannot resolve — a CI step spelling a path as
					# "$LAZILY_CONFORMANCE_MANIFEST" and a Makefile recipe spelling the
					# same path through an expanded $(VAR) are the same command. Dropping
					# it (what this used to do) loses the ARGUMENT as well as its value,
					# so `script.sh <path>` no longer matched a CI step that really ran
					# `script.sh "$PATH"` and the target was reported unreachable. That is
					# a false RED, and it cost lazily-cpp a hardcoded second spelling of
					# the path plus a hand-written equality assertion to keep the two in
					# sync — a new drift surface invented to satisfy a guard that exists
					# to detect drift.
					#
					# Emit a WILDCARD instead: one token that matches one token, so arity
					# is preserved. `script.sh $A` still fails against a CI step that
					# passes no argument at all. This is the same looseness the normalizer
					# already applies to paths, which it reduces to basenames — reach is a
					# floor, not equivalence, exactly as the header says.
					if (substr(tok, 1, 1) == "$") { out = out " " ANY; continue }
				out = out " " tok
			}
			if (started && out != "") print out
		}
	'
}

# ------------------------------------------------------- the make-derived oracle

# Is the awk-derived closure a description of anything make does? (#pinreachclosure)
#
# `prereqs_of` reads Makefile SOURCE TEXT: it scans for the first line matching
# `^check:` and stops. It never asks make, so it cannot see a make conditional —
# and that decouples the set every verdict below is computed over from the set
# make actually builds:
#
#     ifeq (0,1)
#     check: format-check lint type-check test ... ci-reach   # the only ^check: line awk reads
#     else
#     check: format-check lint test ... ci-reach               # what make really parses
#     endif
#
# Measured on THIS Makefile: with that dead branch in place, `make -n check` runs
# no `poe ty` at all, and this guard's output was byte-identical (`cmp -s`, both
# streams) to the healthy run — `reached type-check` included, exit 0. A dead
# branch needs no environment variable; `ifeq ($(SKIP_SLOW),)` plus `SKIP_SLOW=1`
# is the same attack driven from outside the file.
#
# This is why the membership pin below is NOT the load-bearing part. A set pinned
# equal to the awk closure is pinned equal to a constant that the compromised
# state leaves untouched, so it passes by construction — not by being evaded.
# lazily-js found this after implementing the pin alone; every binding in this
# family ports the same script, and it reproduced here on the first attempt.
#
# The oracle: for each target in the awk closure, every ANCHOR of its own recipe
# must also appear among the anchors of what `make -n $ROOT_TARGET` prints. Asked
# of make both times, so a conditional is evaluated by the same program that
# would evaluate it during a real build. Set membership, order and multiplicity
# ignored — make de-duplicates commands across a multi-target graph, so requiring
# position or count here would red on an honest Makefile.
#
# ANCHORS, NOT RAW COMMAND LINES, and this is not a convenience.
#
# The obvious spelling compares the two `make -n` outputs line for line. lazily-gd
# measured that shape permanently red on the one target carrying its suite,
# because its recipe prints a per-invocation run id: `make -n <target>` and
# `make -n <root>` are two separate make invocations, a `:=` id is minted once per
# invocation, so the line cannot match itself. This Makefile mints exactly such an
# id — `LAZILY_CONFORMANCE_RUN_ID`, from `date -u` plus six bytes of
# /dev/urandom — and two invocations one second apart produced
# `make-20260912T173136-29ce2b1d8d08` and `make-20260912T173136-a7b381282ba0`.
#
# py escapes the raw-line red only because it delivers that id through `export`
# rather than through recipe text: every line of `make -n check` here is
# byte-identical across invocations, measured twice. That is one Makefile edit
# from being false. And the danger is not the red — it is what a red on an honest
# Makefile provokes, which is someone weakening this check until it goes away.
#
# So compare through `anchors`, the SAME normalizer the reachability verdict uses:
# program basename, subcommands, flag NAMES with values dropped, paths reduced to
# basenames. One definition of "which gate is this command", used by both rungs,
# rather than the oracle inventing a second one out of raw text. A run id passed
# as a flag value (`--run-id=$(...)`) normalizes away on both sides; a run id
# passed as a BARE POSITIONAL still differs, and the fix then is to pass it as a
# flag value or through the environment as this Makefile already does. Do not
# loosen the comparison to absorb it — that trades a real gate for a quiet one.
#
# A target whose recipe reduces to no anchors at all (every command trivial)
# passes here trivially, on both sides. That is deliberate: it is the
# classification pin's case, not the oracle's.
#
# `make -n` only. `make -p` would answer this question more directly and is
# deliberately not used anywhere in this guard: it builds the default goal and
# dumps the entire environment to stdout, which in CI means printing every secret
# in the job's env into the log.
#
# Targets make refuses to dry-run are skipped — the probe above already reported
# them, and an empty command list from a refused target would otherwise read here
# as "the root runs all of its commands", which is the #lzgrepcpipefail silence
# again in a new place.
#
# What the oracle does NOT see: a target whose recipe is neutered (its commands
# are gone from BOTH sides, so the subset holds trivially) — that is the
# classification pin's job, at the bottom of this file — and a target whose
# recipe is swapped for a gate CI already runs, which is out of scope; see the
# note above the classification pin.
root_anchors="$(dry_run "$ROOT_TARGET" | anchors | LC_ALL=C sort -u)"

oracle_missing=""
oracle_missing_count=0
while IFS= read -r oracle_target; do
	[ -n "$oracle_target" ] || continue
	case "$unreadable" in
	*" $oracle_target "*) continue ;;
	esac
	# `own_commands`, not `dry_run` — the target's OWN recipe, which is the same
	# unit the reachability verdict below asks about. A prerequisite's commands
	# are in the root's run by virtue of the prerequisite, and crediting them
	# here would let a target inherit its neighbours' presence.
	oracle_own="$(own_commands "$oracle_target" | anchors | LC_ALL=C sort -u || true)"
	while IFS= read -r oracle_anchor; do
		[ -n "$oracle_anchor" ] || continue
		# `-e`: an anchor can begin with `-` and would otherwise be read as an
		# option to grep rather than as the pattern.
		if ! grep -qxF -e "$oracle_anchor" <<<"$root_anchors"; then
			oracle_missing="$oracle_missing$oracle_target"$'\n'
			oracle_missing_count=$((oracle_missing_count + 1))
			break
		fi
	done <<<"$oracle_own"
done <<<"$closure"

if [ "$oracle_missing_count" -gt 0 ]; then
	echo "check-ci-reach: target(s) in the closure that 'make $ROOT_TARGET' does NOT run:" >&2
	while IFS= read -r oracle_target; do
		[ -n "$oracle_target" ] || continue
		echo "  - $oracle_target" >&2
	done <<<"$oracle_missing"
	echo >&2
	echo "                The closure here is read out of the Makefile's SOURCE TEXT, one" >&2
	echo "                '^$ROOT_TARGET:' line, with no knowledge of make conditionals." >&2
	echo "                make disagrees, and make is the one that builds. Every verdict" >&2
	echo "                below — including EXPECTED_CLOSURE_TARGETS being satisfied — is" >&2
	echo "                then about a set nobody runs (#pinreachclosure)." >&2
	echo "                Look for an ifeq/ifdef around '$ROOT_TARGET''s prerequisite" >&2
	echo "                list: the branch this guard reads and the branch make takes do" >&2
	echo "                not have to be the same one." >&2
	exit 1
fi

# ------------------------------------------------------- closure membership pin

# Set equality against EXPECTED_CLOSURE_TARGETS, both directions reported
# separately and BY NAME (#pinreachclosure).
#
# The two directions are different findings with different remedies, and
# collapsing them into one "the closure changed" message is what teaches people
# to re-run with the pin updated. In the pin and absent from the closure, a gate
# left the build. In the closure and absent from the pin, a gate arrived and
# nobody wrote it down — harmless today, and the reason the pin still means
# something tomorrow.
#
# `LC_ALL=C` on both sorts: `comm` compares by the collation its inputs were
# sorted in, and a locale that orders `test-interop-peer` and `test` differently
# from C would desynchronise the merge and invent members on both sides.
closure_canonical="$(printf '%s\n' "$closure" | sed '/^$/d' | LC_ALL=C sort -u)"

pin_dropped="$(LC_ALL=C comm -23 <(printf '%s\n' "$pin_canonical") <(printf '%s\n' "$closure_canonical"))"
pin_unpinned="$(LC_ALL=C comm -13 <(printf '%s\n' "$pin_canonical") <(printf '%s\n' "$closure_canonical"))"

# An excuse for a target that is not in the closure at all (#pinreachclosure).
#
# The mirror image of the drop, and it was silent in the same way: an excuse is
# only ever consulted via `is_excused` while walking the closure, so one naming
# something outside it is read by nobody and counted by nothing — `0 excused`,
# no complaint, exit 0. Measured here with TWO planted excuses, one for a real
# Makefile target outside `check` (`bench-scale`) and one for a target that does
# not exist at all; both vanished.
#
# The conformance guard already checks this direction for its own ledger
# (`KNOWN_UNCOVERED lists 'X', which is not in the canonical corpus`), and the
# same argument applies: an excuse nobody reads is a claim about coverage that
# still reads as a claim, so the excuse list rots into things that used to be
# true while presenting itself as the one place to see what is not enforced.
# `grep -qxF` is the comparison KNOWN_UNCOVERED uses, deliberately — whole-line,
# no pattern metacharacters.
pin_stray_excuses=""
for pin_i in ${excused_targets[@]+"${!excused_targets[@]}"}; do
	pin_t="${excused_targets[$pin_i]}"
	if ! grep -qxF "$pin_t" <<<"$closure_canonical"; then
		pin_stray_excuses="$pin_stray_excuses$pin_t"$'\n'
	fi
done

pin_problems=0
pin_membership_problems=0

if [ -n "$pin_dropped" ]; then
	echo "check-ci-reach: pinned in EXPECTED_CLOSURE_TARGETS, but NOT in the closure of" >&2
	echo "                'make $ROOT_TARGET' any more:" >&2
	while IFS= read -r pin_t; do
		[ -n "$pin_t" ] || continue
		echo "  - $pin_t" >&2
	done <<<"$pin_dropped"
	echo "                A gate stopped being required. Nothing else in this guard" >&2
	echo "                would have said so: the remaining targets are all still" >&2
	echo "                reached, so the report just counts one fewer and prints OK." >&2
	echo "                Two remedies, and they are NOT interchangeable:" >&2
	echo "                  * restore it as a prerequisite of '$ROOT_TARGET' — if the" >&2
	echo "                    gate left by accident, this is the fix, and updating the" >&2
	echo "                    pin instead would ratify the accident." >&2
	echo "                  * delete it from EXPECTED_CLOSURE_TARGETS — only if you MEANT" >&2
	echo "                    to stop gating on it, and say why in the same commit." >&2
	pin_problems=$((pin_problems + 1))
	pin_membership_problems=$((pin_membership_problems + 1))
fi

if [ -n "$pin_unpinned" ]; then
	echo "check-ci-reach: in the closure of 'make $ROOT_TARGET', but not pinned in" >&2
	echo "                EXPECTED_CLOSURE_TARGETS:" >&2
	while IFS= read -r pin_t; do
		[ -n "$pin_t" ] || continue
		echo "  - $pin_t" >&2
	done <<<"$pin_unpinned"
	echo "                A gate was added without being written down, so from now on it" >&2
	echo "                could be removed again in silence. Add it to" >&2
	echo "                EXPECTED_CLOSURE_TARGETS (sorted) — that IS the remedy here," >&2
	echo "                not a workaround for one." >&2
	pin_problems=$((pin_problems + 1))
	pin_membership_problems=$((pin_membership_problems + 1))
fi

if [ -n "$pin_stray_excuses" ]; then
	while IFS= read -r pin_t; do
		[ -n "$pin_t" ] || continue
		echo "check-ci-reach: $CONF excuses '$pin_t', which is not in the closure of" >&2
		echo "                'make $ROOT_TARGET' — so the excuse is read by nobody and" >&2
		echo "                excuses nothing, while still reading as a claim about what" >&2
		echo "                this binding does not enforce." >&2
		echo "                  * if '$pin_t' should be gated, add it to '$ROOT_TARGET'." >&2
		echo "                  * if it never was, delete the excuse." >&2
	done <<<"$pin_stray_excuses"
	pin_problems=$((pin_problems + 1))
fi

if [ "$pin_membership_problems" -gt 0 ]; then
	echo >&2
	echo "check-ci-reach: the closure is not the pinned set, so every verdict below" >&2
	echo "                would be about a different audit than the one pinned" >&2
	echo "                (#pinreachclosure)." >&2
fi

if [ "$pin_problems" -gt 0 ]; then
	exit 1
fi

# --------------------------------------------------------------------- matching

ci_steps="$(mktemp)"
ci_raw="$(mktemp)"
ci_anchor="$(mktemp)"
ci_step_anchor="$(mktemp)"
gate_anchors_file="$(mktemp)"
jg_found_file="$(mktemp)"
trap 'rm -f "$ci_steps" "$ci_raw" "$ci_anchor" "$ci_step_anchor" "$gate_anchors_file" "$jg_found_file"' EXIT
ci_step_commands "${workflows[@]}" >"$ci_steps"

# The flat command stream is DERIVED from the labelled one, not scraped again.
# Two scrapers would be two definitions of "what CI runs", and the one nobody
# reads while debugging is the one that drifts.
cut -f6- <"$ci_steps" >"$ci_raw"
anchors <"$ci_raw" | sort -u >"$ci_anchor"

if [ ! -s "$ci_anchor" ]; then
	echo "check-ci-reach: no run: steps found in ${workflows[*]} — a guard with an empty haystack passes everything" >&2
	exit 1
fi

# Anchors per STEP: `workflow<TAB>job<TAB>step<TAB>anchor` (#reversereachdirection).
#
# `anchors` is line-oriented — each input command yields its anchors independently
# of every other line — so grouping the input by step cannot change what it
# produces. That is the premise this file rests on, and it is asserted as such in
# tests/test_ci_reach_step_pin.py rather than trusted: the union of these
# per-step anchors must equal $ci_anchor exactly.
#
# `anchors` itself is untouched. One definition of "which gate is this command"
# is used by the oracle, by the flat check and by the step-scoped check; a second
# normalizer written for the step-scoped path could disagree with the other two
# about a wildcard or a basename and the disagreement would read as a gap.
while IFS="$(printf '\t')" read -r sk_wf sk_job sk_idx; do
	# A tab is IFS WHITESPACE, so `read` collapses a run of them and an empty
	# field would shift every field after it. None of these three can be empty —
	# the scraper only emits a command inside a job, and the ordinal starts at 1 —
	# so a blank here means the split went wrong, not that a field was blank.
	if [ -z "$sk_wf" ] || [ -z "$sk_job" ] || [ -z "$sk_idx" ]; then
		echo "check-ci-reach: could not split a scraped step key: '$sk_wf' / '$sk_job' /" >&2
		echo "                '$sk_idx'. Every anchor below would be filed under the" >&2
		echo "                wrong step (#reversereachdirection)." >&2
		exit 1
	fi
	while IFS= read -r sk_anchor; do
		[ -n "$sk_anchor" ] || continue
		printf '%s\t%s\t%s\t%s\n' "$sk_wf" "$sk_job" "$sk_idx" "$sk_anchor"
	done < <(
		awk -F'\t' -v w="$sk_wf" -v j="$sk_job" -v i="$sk_idx" '
			$1 == w && $2 == j && $3 == i {
				rest = $0
				for (k = 0; k < 5; k++) rest = substr(rest, index(rest, "\t") + 1)
				print rest
			}
		' "$ci_steps" | anchors | LC_ALL=C sort -u
	)
done < <(cut -f1-3 <"$ci_steps" | LC_ALL=C sort -u) >"$ci_step_anchor"

# An UNNAMED `run:` step in a listed workflow (#reversereachdirection).
#
# Reach is pinned by step NAME now, so a step with no name is one no pin can
# name. Refused rather than tolerated, for a reason lazily-zig measured: its
# scraper let an unnamed step INHERIT the previous step's name, which credited a
# command to a step that did not run it — worse than no name at all. The scraper
# here resets the name at every sequence marker, so an unnamed step carries an
# empty one and cannot be pinned; verified by measurement, and asserted in
# tests/test_ci_reach_step_pin.py. This refusal is what keeps that verified
# property from having to be re-verified: with no unnamed step in a listed
# workflow, neither inheritance nor a synthesised `<unnamed@file:line>`
# placeholder (which lazily-dart pinned, and it resolved, and it passed) has
# anything to attach to.
#
# Scope: LISTED workflows only. wheels.yml is not listed — it is tag-triggered —
# so it reaches nothing and its step names are its own business. Its two unnamed
# steps were named anyway, because listing it later should not be blocked on an
# unrelated edit.
gs_unnamed="$(awk -F'\t' '$4 == "" { print $1 "  job " $2 "  step #" $3 "  run: " $6 }' "$ci_steps" | LC_ALL=C sort -u)"
if [ -n "$gs_unnamed" ]; then
	echo "check-ci-reach: run: step(s) with no name: in ${workflows[*]}:" >&2
	printf '%s\n' "$gs_unnamed" | sed 's/^/  - /' >&2
	echo "                EXPECTED_GATE_STEPS pins gates BY STEP NAME, so a step with" >&2
	echo "                no name is one no pin can name — and a guard that fell back" >&2
	echo "                to the previous step's name would credit this command to a" >&2
	echo "                step that did not run it (#reversereachdirection). Add a name:." >&2
	exit 1
fi

# ----------------------------------------------------- the gate-step resolution

# Resolve each pinned step NAME to exactly one (workflow, job) pair, and refuse
# anything else (#reversereachdirection).
#
# A name that resolves to NOTHING is the dangerous one to leave alone: with
# step-scoped reach, its member's anchors are searched in an empty haystack, so
# it fails — correctly, but naming the anchor rather than the deleted step, which
# sends the reader looking for a missing command instead of a renamed step.
#
# A name that resolves to TWO is worse, because it passes. Step names are not
# unique family-wide, and "found in some step called X" is the flat check again
# with extra steps. The remedy is to qualify the pin, not to let the guard pick.
gs_resolve_problems=0
for gs_i in "${!gate_step_targets[@]}"; do
	gs_name="${gate_step_names[$gs_i]}"
	gs_hits="$(awk -F'\t' -v s="$gs_name" '$4 == s { print $1 "\t" $2 "\t" $3 }' "$ci_steps" | LC_ALL=C sort -u)"
	gs_hit_count="$(printf '%s' "$gs_hits" | grep -c . || true)"
	if [ "$gs_hit_count" -eq 0 ]; then
		echo "check-ci-reach: EXPECTED_GATE_STEPS pins '${gate_step_targets[$gs_i]}' to a CI step" >&2
		echo "                named '$gs_name', and no run: step in ${workflows[*]}" >&2
		echo "                has that name (#reversereachdirection)." >&2
		echo "                  * if the step was renamed, move the pin with it." >&2
		echo "                  * if the step was deleted, the gate is not in CI at all —" >&2
		echo "                    restore the step or excuse the target in $CONF." >&2
		gs_resolve_problems=$((gs_resolve_problems + 1))
		continue
	fi
	if [ "$gs_hit_count" -gt 1 ]; then
		echo "check-ci-reach: EXPECTED_GATE_STEPS pins '${gate_step_targets[$gs_i]}' to a CI step" >&2
		echo "                named '$gs_name', which occurs in $gs_hit_count places:" >&2
		printf '%s\n' "$gs_hits" | sed 's/\t/  job /; s/\t/  step #/' | sed 's/^/                  /' >&2
		echo "                A name that matches two steps is the flat 'some command" >&2
		echo "                anywhere' check again, so this guard will not pick one." >&2
		echo "                Rename one of the steps so the pin is unambiguous" >&2
		echo "                (#reversereachdirection)." >&2
		gs_resolve_problems=$((gs_resolve_problems + 1))
		continue
	fi
	gate_step_wfs[$gs_i]="${gs_hits%%$'\t'*}"
	gs_rest="${gs_hits#*$'\t'}"
	gate_step_jobs[$gs_i]="${gs_rest%%$'\t'*}"
	gate_step_idxs[$gs_i]="${gs_rest#*$'\t'}"
done

if [ "$gs_resolve_problems" -gt 0 ]; then
	exit 1
fi

# A pinned step that is CONDITIONAL, or whose failure is discarded
# (#reversereachdirection).
#
# lazily-js measured the level above this one: a pinned step can exist, be
# unique, be unconditional and run the gate, in a JOB or a WORKFLOW that never
# runs — job-level `if: false`, job-level `continue-on-error: true`, and `on:`
# reduced to `workflow_dispatch` all stayed green, because nothing here had a
# handle on the job or the trigger. That level is closed by the four activation
# pins (#verifyworkflowactually); this rung is still the STEP-level half, and the
# two do not substitute for each other — a step-level `if:` is invisible to a job
# pin and a job-level one is invisible to this rung.
#
# The STEP-level halves are closed, and only because the step map gave this guard
# a handle on the step at all. `continue-on-error: true` turns the gate's failure
# into a pass, which is reach without enforcement — the same defect as a
# repairing formatter in CI, one level up. An `if:` makes the step conditional on
# a GitHub expression this guard cannot evaluate, and a step it cannot prove runs
# is not reach; the value is deliberately not inspected, because `if: false` and
# `if: github.event_name == 'schedule'` differ only in how obvious they are.
#
# Measured on this workflow: ZERO steps carry either, so nothing legitimate is
# reddened. A gate step that genuinely needs a condition has to say so by losing
# its pin and taking an excuse in the conf, which is the reviewable form.
gs_guarded="$(awk -F'\t' -v OFS='\t' '$5 != "" { print $4, $5 }' "$ci_steps" | LC_ALL=C sort -u)"
gs_guard_problems=0
for gs_i in "${!gate_step_targets[@]}"; do
	gs_name="${gate_step_names[$gs_i]}"
	gs_g="$(awk -F'\t' -v s="$gs_name" '$1 == s { print $2; exit }' <<<"$gs_guarded")"
	[ -n "$gs_g" ] || continue
	echo "check-ci-reach: the CI step pinned for '${gate_step_targets[$gs_i]}' carries" >&2
	echo "                '$gs_g', so this guard cannot show it runs and fails:" >&2
	echo "                  step '$gs_name'" >&2
	echo "                An 'if:' makes the step conditional on an expression this" >&2
	echo "                guard cannot evaluate; 'continue-on-error: true' turns the" >&2
	echo "                gate's failure into a pass, which is reach without" >&2
	echo "                enforcement (#reversereachdirection)." >&2
	echo "                  * drop the condition, or" >&2
	echo "                  * drop the pin and excuse the target in $CONF, which is" >&2
	echo "                    the reviewable way to say a gate is not enforced here." >&2
	gs_guard_problems=$((gs_guard_problems + 1))
done

if [ "$gs_guard_problems" -gt 0 ]; then
	exit 1
fi

# Does CI contain a command whose tokens contain this anchor as an in-order
# subsequence? Extra flags and arguments on the CI side are fine; missing ones are
# not.
anchor_reached() {
	awk -v want="$1" '
		BEGIN { ANY = "\001any"; wn = split(want, w, / /) }
		{
			hn = split($0, h, / /)
			wi = 1
			# A wildcard on EITHER side matches, because either side may be the
			# one that spelled the argument through a variable.
			for (hi = 1; hi <= hn && wi <= wn; hi++)
				if (h[hi] == w[wi] || h[hi] == ANY || w[wi] == ANY) wi++
			if (wi > wn) { found = 1; exit }
		}
		END { exit found ? 0 : 1 }
	' "$ci_anchor"
}

# The same subsequence test, restricted to ONE pinned step (#reversereachdirection).
#
# This is the whole difference between "CI runs this command somewhere" and "the
# step this gate is pinned to runs it". The predicate is deliberately the same
# one as above, with the haystack narrowed — a second, subtly different matcher
# for the narrowed case would make a disagreement between them read as a gap in
# CI rather than as a bug here.
anchor_reached_in_step() {
	awk -F'\t' -v want="$1" -v w="$2" -v j="$3" -v i="$4" '
		BEGIN { ANY = "\001any"; wn = split(want, wa, / /) }
		$1 != w || $2 != j || $3 != i { next }
		{
			hn = split($4, h, / /)
			wi = 1
			for (hi = 1; hi <= hn && wi <= wn; hi++)
				if (h[hi] == wa[wi] || h[hi] == ANY || wa[wi] == ANY) wi++
			if (wi > wn) { found = 1; exit }
		}
		END { exit found ? 0 : 1 }
	' "$ci_step_anchor"
}

# CI invoking the target through make counts as reach without any anchor work.
make_invokes() {
	awk -v target="$1" '
		{
			n = split($0, t, / /)
			if (t[1] != "make") next
			for (i = 2; i <= n; i++) if (t[i] == target) { found = 1; exit }
		}
		END { exit found ? 0 : 1 }
	' "$ci_anchor"
}

is_excused() {
	local t="$1" i
	for i in "${!excused_targets[@]}"; do
		[ "${excused_targets[$i]}" = "$t" ] && return 0
	done
	return 1
}

excuse_reason() {
	local t="$1" i
	for i in "${!excused_targets[@]}"; do
		if [ "${excused_targets[$i]}" = "$t" ]; then
			printf '%s' "${excused_reasons[$i]}"
			return
		fi
	done
}

unreached=""
unreached_count=0
unreadable_count=0
# Which targets the gate-step pin is REQUIRED to cover, and which ones it must
# refuse, collected here because both are conclusions of the loop below rather
# than properties of the Makefile graph (#reversereachdirection).
gate_domain=""
mi_reached=""
excused_members=""
stale=""
stale_count=0
nogate=""
nogate_count=0
reached=0
excused_ok=0

while IFS= read -r target; do
	[ -n "$target" ] || continue

	# BEFORE the excuse check, and before anything reads a recipe. A target make
	# refuses to dry-run has no readable recipe at all, so every verdict about it
	# — reached, excused, carrying no gate — would be a statement about an empty
	# string. An excuse must not launder it either: an excuse is a claim about
	# what CI runs, not a licence for a Makefile make cannot read.
	case "$unreadable" in
	*" $target "*)
		printf 'UNREADABLE %s\n' "$target"
		printf '           %s -n %s failed: %s\n' \
			"$MAKE_BIN" "$target" "$(unreadable_reason "$target")"
		unreadable_count=$((unreadable_count + 1))
		continue
		;;
	esac

	target_anchors="$(own_commands "$target" | anchors | sort -u || true)"

	if [ -z "$target_anchors" ]; then
		nogate="$nogate$target"$'\n'
		nogate_count=$((nogate_count + 1))
		continue
	fi

	hit=1
	missing_anchors=""
	# Which step the anchors are looked for IN (#reversereachdirection). Empty means this
	# target has no pin, and the flat "some CI command anywhere" check is used
	# instead — deliberately, so an unpinned target gets ONE finding (the missing
	# pin, reported after this loop) rather than a MISSING verdict whose named
	# cause is the wrong one. It is not a way out: an unpinned gate-carrying
	# target fails the domain check below regardless of this verdict.
	target_step=""
	target_step_wf=""
	target_step_job=""
	target_step_idx=""
	if gs_i="$(gate_step_index_of "$target")"; then
		target_step="${gate_step_names[$gs_i]}"
		target_step_wf="${gate_step_wfs[$gs_i]}"
		target_step_job="${gate_step_jobs[$gs_i]}"
		target_step_idx="${gate_step_idxs[$gs_i]}"
	fi

	if make_invokes "$target"; then
		mi_reached="$mi_reached$target"$'\n'
	else
		if ! is_excused "$target"; then
			gate_domain="$gate_domain$target"$'\n'
			while IFS= read -r a; do
				[ -n "$a" ] || continue
				printf '%s\t%s\n' "$target" "$a" >>"$gate_anchors_file"
			done <<<"$target_anchors"
		fi
		while IFS= read -r a; do
			[ -n "$a" ] || continue
			if [ -n "$target_step" ]; then
				anchor_reached_in_step "$a" "$target_step_wf" "$target_step_job" "$target_step_idx" && continue
			else
				anchor_reached "$a" && continue
			fi
			hit=0
			missing_anchors="$missing_anchors$a"$'\n'
		done <<<"$target_anchors"
	fi

	if is_excused "$target"; then
		excused_members="$excused_members$target"$'\n'
		if [ "$hit" -eq 1 ]; then
			stale="$stale$target"$'\n'
			stale_count=$((stale_count + 1))
		else
			excused_ok=$((excused_ok + 1))
			printf 'excused  %-32s %s\n' "$target" "$(excuse_reason "$target")"
		fi
		continue
	fi

	if [ "$hit" -eq 1 ]; then
		reached=$((reached + 1))
		printf 'reached  %s\n' "$target"
	else
		unreached="$unreached$target"$'\n'
		unreached_count=$((unreached_count + 1))
		printf 'MISSING  %s\n' "$target"
		while IFS= read -r a; do
			[ -n "$a" ] || continue
			if [ -n "$target_step" ]; then
				printf '           the pinned CI step `%s` does not run `%s`\n' \
					"$target_step" "$a"
			else
				printf '           no CI run: step matches `%s`\n' "$a"
			fi
		done <<<"$missing_anchors"
	fi
done <<<"$closure"

while IFS= read -r target; do
	[ -n "$target" ] || continue
	printf 'no gate  %-32s recipe runs no checkable command\n' "$target"
done <<<"$nogate"

status=0
if [ "$unreadable_count" -gt 0 ]; then
	echo >&2
	echo "check-ci-reach: $unreadable_count target(s) in 'make $ROOT_TARGET''s closure that" >&2
	echo "                $MAKE_BIN refuses to dry-run, so their recipes could not be read" >&2
	echo "                at all. Left unreported this is a FALSE GREEN, not a missing" >&2
	echo "                verdict: an unreadable recipe reads as an empty one, and an" >&2
	echo "                empty one is excused as 'carrying no gate' (#lzgrepcpipefail)." >&2
	for i in "${!unreadable_targets[@]}"; do
		echo "  - ${unreadable_targets[$i]}: ${unreadable_errors[$i]}" >&2
	done
	status=1
fi
if [ "$stale_count" -gt 0 ]; then
	echo >&2
	while IFS= read -r t; do
		[ -n "$t" ] || continue
		echo "check-ci-reach: '$t' is excused in $CONF but CI DOES reach it — remove the excuse" >&2
	done <<<"$stale"
	status=1
fi

# Classification set equality (#pinreachclosure). Reported here rather than with
# the other two pins because it is the only one that needs the verdict loop to
# have run: "carries no gate" is a conclusion about a recipe, not about the
# Makefile's prerequisite graph.
nogate_canonical="$(printf '%s\n' "$nogate" | sed '/^$/d' | LC_ALL=C sort -u)"
nogate_pin_canonical="$(printf '%s\n' "${EXPECTED_NO_GATE_TARGETS[@]}" | LC_ALL=C sort -u)"
nogate_unpinned="$(LC_ALL=C comm -13 <(printf '%s\n' "$nogate_pin_canonical") <(printf '%s\n' "$nogate_canonical"))"
nogate_dropped="$(LC_ALL=C comm -23 <(printf '%s\n' "$nogate_pin_canonical") <(printf '%s\n' "$nogate_canonical"))"

if [ -n "$nogate_unpinned" ]; then
	echo >&2
	echo "check-ci-reach: target(s) now carrying no gate that are not pinned as" >&2
	echo "                EXPECTED_NO_GATE_TARGETS:" >&2
	while IFS= read -r t; do
		[ -n "$t" ] || continue
		echo "  - $t" >&2
	done <<<"$nogate_unpinned"
	echo "                Its name is still in '$ROOT_TARGET''s prerequisites and make" >&2
	echo "                still runs it, so neither the membership pin nor the oracle" >&2
	echo "                has anything to object to — the recipe simply stopped running" >&2
	echo "                a checkable command, and a target carrying no gate is not" >&2
	echo "                required to appear in CI at all (#pinreachclosure)." >&2
	echo "                  * if the gate was emptied by accident, restore the recipe." >&2
	echo "                  * if it genuinely carries nothing now, add it to" >&2
	echo "                    EXPECTED_NO_GATE_TARGETS and say why — that entry is a" >&2
	echo "                    standing claim that this target cannot fail a build." >&2
	status=1
fi
if [ -n "$nogate_dropped" ]; then
	echo >&2
	echo "check-ci-reach: pinned as EXPECTED_NO_GATE_TARGETS, but no longer classified" >&2
	echo "                that way:" >&2
	while IFS= read -r t; do
		[ -n "$t" ] || continue
		echo "  - $t" >&2
	done <<<"$nogate_dropped"
	echo "                It carries a gate now (or make refuses to dry-run it, in which" >&2
	echo "                case the UNREADABLE finding above is the one to read). Either" >&2
	echo "                way the claim that it cannot fail a build is stale: remove it" >&2
	echo "                from EXPECTED_NO_GATE_TARGETS so the reach requirement applies." >&2
	status=1
fi

# Gate-step DOMAIN set equality (#reversereachdirection). Here for the same reason the
# classification pin is: whether a target is anchor-reached, make-invocation-
# reached, excused or gateless is a conclusion of the loop above, not a property
# of the Makefile's prerequisite graph.
#
# The domain is exactly {carries a gate} minus {excused} minus {reached through
# `make <target>`}. Every member of it must be pinned to a step, and nothing
# outside it may be.
gs_domain_canonical="$(printf '%s\n' "$gate_domain" | sed '/^$/d' | LC_ALL=C sort -u)"
gs_pinned_canonical="$(printf '%s\n' "${gate_step_targets[@]}" | LC_ALL=C sort -u)"
gs_unpinned="$(LC_ALL=C comm -13 <(printf '%s\n' "$gs_pinned_canonical") <(printf '%s\n' "$gs_domain_canonical"))"
gs_surplus="$(LC_ALL=C comm -23 <(printf '%s\n' "$gs_pinned_canonical") <(printf '%s\n' "$gs_domain_canonical"))"

if [ -n "$gs_unpinned" ]; then
	echo >&2
	echo "check-ci-reach: gate(s) CI spells out that EXPECTED_GATE_STEPS does not pin to" >&2
	echo "                a step:" >&2
	while IFS= read -r gs_t; do
		[ -n "$gs_t" ] || continue
		echo "  - $gs_t" >&2
	done <<<"$gs_unpinned"
	echo "                Their anchors are matched against every run: body in" >&2
	echo "                ${workflows[*]} instead of against one step, which" >&2
	echo "                is the state where a recipe repointed at another step's" >&2
	echo "                command reads as reached (#reversereachdirection). Add each one to" >&2
	echo "                EXPECTED_GATE_STEPS (sorted) naming the step that runs it —" >&2
	echo "                that IS the remedy here, not a workaround for one." >&2
	status=1
fi

if [ -n "$gs_surplus" ]; then
	echo >&2
	while IFS= read -r gs_t; do
		[ -n "$gs_t" ] || continue
		if grep -qxF "$gs_t" <<<"$(printf '%s\n' "$mi_reached" | sed '/^$/d')"; then
			echo "check-ci-reach: EXPECTED_GATE_STEPS pins a step for '$gs_t', but CI reaches" >&2
			echo "                it by running \`make $gs_t\` — so there is no independent" >&2
			echo "                CI-side spelling of the gate to cross-check, and the pinned" >&2
			echo "                step name asserts nothing (#reversereachdirection). Where CI's" >&2
			echo "                instruction is 'run the target', CI faithfully runs whatever" >&2
			echo "                the recipe became. Delete the entry rather than keeping a" >&2
			echo "                pin that cannot fail." >&2
		elif grep -qxF "$gs_t" <<<"$(printf '%s\n' "$excused_members" | sed '/^$/d')"; then
			echo "check-ci-reach: EXPECTED_GATE_STEPS pins a step for '$gs_t', which $CONF" >&2
			echo "                excuses as deliberately NOT reached by CI. The two claims are" >&2
			echo "                opposites; one of them is stale (#reversereachdirection)." >&2
		elif grep -qxF "$gs_t" <<<"$(printf '%s\n' "$nogate" | sed '/^$/d')"; then
			echo "check-ci-reach: EXPECTED_GATE_STEPS pins a step for '$gs_t', which now carries" >&2
			echo "                no gate at all — its recipe runs no checkable command, so the" >&2
			echo "                pinned step is a claim about a gate that no longer exists" >&2
			echo "                (#reversereachdirection). Restore the recipe, or drop the entry and" >&2
			echo "                say why in the same commit." >&2
		else
			echo "check-ci-reach: EXPECTED_GATE_STEPS pins a step for '$gs_t', which is not a" >&2
			echo "                gate-carrying target in the closure of 'make $ROOT_TARGET'." >&2
			echo "                A pin nobody consults still reads as a claim about which CI" >&2
			echo "                step runs that gate (#reversereachdirection)." >&2
		fi
	done <<<"$gs_surplus"
	status=1
fi

# ANCHOR COLLISION between pinned members (#reversereachdirection).
#
# Step-scoping and this rung overlap on the member-to-member repoint, and they are
# two PROPERTIES rather than two spellings of one — lazily-dart demonstrated a
# collision-only case, and it reproduces here at exit 0 without this rung:
#
#   Consolidate `Format (make format)` and `Lint (make lint)` into one
#   `Ruff (format + lint)` step running both commands (a plausible tidy-up), pin
#   BOTH members to it, and repoint `format-check:`'s recipe at lint's command.
#   Every anchor is inside its own pinned step, so step-scoping is satisfied;
#   `make -n check` runs `ruff format --check` ZERO times; the guard printed
#   "8 gate(s) reached INSIDE the CI step pinned for each" and exited 0.
#
# The property that fails there is not "is the anchor in the pinned step" but "is
# the pinned step running SOMEONE ELSE'S gate". So: for every pinned member, no
# OTHER pinned member's anchor set may be fully contained in its step. That
# subsumes the simpler injectivity check (two members pinned to one step is the
# special case where each contains the other) and also catches the asymmetric
# shape where one step happens to run a second member's command without being
# pinned to it.
#
# The cost is stated rather than hidden: a CI step that deliberately runs two
# gates is refused, and the remedy is to split it into two named steps. That is
# the same shape as every other refusal here — the guard will not pick which of
# two readings of an ambiguous CI file is the intended one.
gs_collisions=""
for gs_i in "${!gate_step_targets[@]}"; do
	gs_owner="${gate_step_targets[$gs_i]}"
	grep -qxF "$gs_owner" <<<"$gs_domain_canonical" || continue
	for gs_j in "${!gate_step_targets[@]}"; do
		[ "$gs_i" != "$gs_j" ] || continue
		gs_other="${gate_step_targets[$gs_j]}"
		gs_other_anchors="$(awk -F'\t' -v t="$gs_other" '$1 == t { print $2 }' "$gate_anchors_file")"
		[ -n "$gs_other_anchors" ] || continue
		gs_all_in=1
		while IFS= read -r gs_a; do
			[ -n "$gs_a" ] || continue
			anchor_reached_in_step "$gs_a" "${gate_step_wfs[$gs_i]}" \
				"${gate_step_jobs[$gs_i]}" "${gate_step_idxs[$gs_i]}" && continue
			gs_all_in=0
			break
		done <<<"$gs_other_anchors"
		if [ "$gs_all_in" -eq 1 ]; then
			gs_collisions="$gs_collisions$gs_owner	$gs_other	${gate_step_names[$gs_i]}"$'\n'
		fi
	done
done

if [ -n "$gs_collisions" ]; then
	echo >&2
	echo "check-ci-reach: CI step(s) pinned for one gate that also run ANOTHER gate's:" >&2
	while IFS="$(printf '\t')" read -r gs_owner gs_other gs_step; do
		[ -n "$gs_owner" ] || continue
		echo "  - '$gs_step' is pinned for '$gs_owner' and runs all of '$gs_other''s" >&2
		echo "    anchors too" >&2
	done <<<"$gs_collisions"
	echo "                Step-scoping cannot see a repoint BETWEEN these two: either" >&2
	echo "                recipe can become the other's command and its anchors are" >&2
	echo "                still inside its own pinned step, while the gate runs zero" >&2
	echo "                times (#reversereachdirection). Split the step so each gate has one" >&2
	echo "                named CI step of its own." >&2
	status=1
fi

# ACTIVATION: does the workflow run, and does the gate job run
# (#verifyworkflowactually).
#
# Four set-equalities, reported HERE rather than before the verdicts above for the
# same reason the vacuity floor moved last: a failure here does not invalidate a
# single reach verdict — the steps are exactly where they were — and pre-empting
# the reach report would hide which gates are in which step while telling the
# reader the workflow does not run. Both halves of the picture print.
act_rows="$(wf_activation "${workflows[@]}")"
act_bad="$(printf '%s\n' "$act_rows" | awk -F'\t' '$1 == "UNPARSED" || $1 == "NOON"')"
if [ -n "$act_bad" ]; then
	echo >&2
	echo "check-ci-reach: this guard could not read the 'on:' block of a counted" >&2
	echo "                workflow:" >&2
	printf '%s\n' "$act_bad" | sed 's/\t/  /g; s/^/  - /' >&2
	echo "                A NOON row means no top-level 'on:' key was found at all; an" >&2
	echo "                UNPARSED row is a shape the three supported spellings do not" >&2
	echo "                cover. Either way the trigger pin below would be comparing" >&2
	echo "                against nothing, so it is refused rather than reported as a" >&2
	echo "                missing trigger (#verifyworkflowactually)." >&2
	status=1
fi

act_report() {
	# `<pinned but not found>` / `<found but not pinned>` for one pin, with the
	# rows printed in the pin's own spelling so a reader can paste the fix.
	local label="$1" missing="$2" surplus="$3" why="$4"
	echo >&2
	if [ -n "$missing" ]; then
		echo "check-ci-reach: pinned in $label, but NOT how the workflow reads now:" >&2
		printf '%s\n' "$missing" | sed 's/\t/  /g; s/^/  - /' >&2
	fi
	if [ -n "$surplus" ]; then
		echo "check-ci-reach: in the workflow now, and NOT pinned in $label:" >&2
		printf '%s\n' "$surplus" | sed 's/\t/  /g; s/^/  - /' >&2
	fi
	# WHY once, after both directions. A changed value is a missing row AND a
	# surplus row, and printing the same paragraph twice reads like two findings.
	printf '%s\n' "$why" | sed 's/^/                /' >&2
}

act_diff() {
	# Set-equality between a canonicalized pin and a canonicalized discovery.
	# Both sides go through `canon_rows`, so a hand-aligned array row and a
	# scraped row are the same fact; comparing the raw strings would make the
	# array's alignment load-bearing.
	local pin="$1" found="$2"
	act_missing="$(LC_ALL=C comm -23 <(printf '%s\n' "$pin") <(printf '%s\n' "$found"))"
	act_surplus="$(LC_ALL=C comm -13 <(printf '%s\n' "$pin") <(printf '%s\n' "$found"))"
}

trig_found="$(printf '%s\n' "$act_rows" |
	awk -F'\t' '$1 == "trigger" { print $2 "\t" $3 }' | canon_rows 2 | LC_ALL=C sort -u)"
trig_pin="$(printf '%s\n' "${EXPECTED_TRIGGERS[@]}" | canon_rows 2 | LC_ALL=C sort -u)"
act_diff "$trig_pin" "$trig_found"
if [ -n "$act_missing" ] || [ -n "$act_surplus" ]; then
	act_report "EXPECTED_TRIGGERS" "$act_missing" "$act_surplus" \
		"A trigger that left is a trigger the gates no longer run on — 'on:' reduced
to 'workflow_dispatch' is the shape lazily-js measured at exit 0, with every
gate perfectly pinned to a step nothing ever executes. A trigger that arrived
changes when the whole gate set runs. Neither is this guard's to decide: move
the pin in the same commit as the workflow, or revert the workflow
(#verifyworkflowactually)."
	status=1
fi

filt_ws="$(printf '%s\n' "$act_rows" | awk -F'\t' '$1 == "filter" && $4 ~ /[[:space:]]/ { print $2 "\t" $3 "\t" $4 }')"
if [ -n "$filt_ws" ]; then
	echo >&2
	echo "check-ci-reach: trigger filter value(s) containing whitespace:" >&2
	printf '%s\n' "$filt_ws" | sed 's/\t/  /g; s/^/  - /' >&2
	echo "                EXPECTED_TRIGGER_FILTERS is whitespace-separated, so such a" >&2
	echo "                value cannot be pinned exactly and would compare equal to a" >&2
	echo "                different one (#verifyworkflowactually)." >&2
	status=1
fi

filt_found="$(printf '%s\n' "$act_rows" |
	awk -F'\t' '$1 == "filter" { print $2 "\t" $3 "\t" $4 }' | canon_rows 3 | LC_ALL=C sort -u)"
if [ "${#EXPECTED_TRIGGER_FILTERS[@]}" -eq 1 ] && [ "${EXPECTED_TRIGGER_FILTERS[0]}" = "<none>" ]; then
	filt_pin=""
else
	filt_pin="$(printf '%s\n' "${EXPECTED_TRIGGER_FILTERS[@]}" | canon_rows 3 | LC_ALL=C sort -u)"
fi
act_diff "$filt_pin" "$filt_found"
if [ -n "$act_missing" ] || [ -n "$act_surplus" ]; then
	act_report "EXPECTED_TRIGGER_FILTERS" "$act_missing" "$act_surplus" \
		"A filter decides which pushes and which PRs actually start the run, so this
is the trigger pin at finer grain: 'branches: [\"**\"]' narrowed to '[\"main\"]'
stops gating every branch, and an introduced 'paths:' filter is the one that
matters most — NO binding in this family has one today, and a 'paths:' filter
that excludes the Makefile means a gate-retiring edit does not even trigger
the workflow that would have caught it (#verifyworkflowactually)."
	status=1
fi

gj_found="$(for gj_i in "${!gate_step_targets[@]}"; do
	printf '%s\t%s\n' "${gate_step_wfs[$gj_i]}" "${gate_step_jobs[$gj_i]}"
done | canon_rows 2 | LC_ALL=C sort -u)"
gj_pin="$(printf '%s\n' "${EXPECTED_GATE_JOBS[@]}" | canon_rows 2 | LC_ALL=C sort -u)"
act_diff "$gj_pin" "$gj_found"
if [ -n "$act_missing" ] || [ -n "$act_surplus" ]; then
	act_report "EXPECTED_GATE_JOBS" "$act_missing" "$act_surplus" \
		"These are the jobs holding the pinned gate steps. A gate step that moved to
another job keeps its name, stays unique, stays unconditional and still runs
the gate — every rung above is satisfied — while the job it landed in may be
skipped, advisory, or gated on a condition of its own. The move is named here
and its job guards are named below; both findings describe the same edit
(#verifyworkflowactually)."
	status=1
fi

jg_rows="$(wf_job_guards "${workflows[@]}")"
jg_found=""
while IFS="$(printf '\t')" read -r jg_wf jg_job; do
	[ -n "$jg_wf" ] && [ -n "$jg_job" ] || continue
	for jg_key in continue-on-error if; do
		jg_v="$(awk -F'\t' -v w="$jg_wf" -v j="$jg_job" -v k="$jg_key" \
			'$1 == w && $2 == j && $3 == k { print $4; exit }' <<<"$jg_rows")"
		# `<absent>` rather than a missing row, so absent -> present is a CHANGED
		# value with a name rather than a row a reader has to notice is gone.
		[ -n "$jg_v" ] || jg_v="<absent>"
		printf '%s\t%s\t%s=%s\n' "$jg_wf" "$jg_job" "$jg_key" "$jg_v"
	done
done <<<"$gj_found" >"$jg_found_file"
jg_found="$(canon_rows 3 <"$jg_found_file" | LC_ALL=C sort -u)"
jg_pin="$(printf '%s\n' "${EXPECTED_GATE_JOB_GUARDS[@]}" | canon_rows 3 | LC_ALL=C sort -u)"
act_diff "$jg_pin" "$jg_found"
if [ -n "$act_missing" ] || [ -n "$act_surplus" ]; then
	act_report "EXPECTED_GATE_JOB_GUARDS" "$act_missing" "$act_surplus" \
		"Job-level, not step-level: the step map already refuses a pinned STEP that
carries 'if:' or 'continue-on-error: true', and the job-level versions are
invisible to it while having the same effect on every step inside. 'if: false'
skips the job; 'continue-on-error: true' turns its failure into a pass. The
pin is a VALUE in both directions, not an absence — lazily-zig's advisory
master leg is a legitimate job-level 'continue-on-error' keyed on a matrix
value, so a rule refusing any guard at all would false-red a correct config
(#verifyworkflowactually)."
	status=1
fi

# THE ACTIVATION FLOOR: the rung a PIN EDIT CANNOT CLEAR
# (#verifyworkflowactually).
#
# lazily-go named the hole in the four pins above, and it is the family's oldest
# lesson wearing a new hat. Every one of them is fails-when-stale, which catches
# DRIFT — the workflow moving while the pin stays put. It does not catch a
# LAUNDERED edit: reduce `on:` to `workflow_dispatch` and rewrite
# EXPECTED_TRIGGERS to match, in the same commit, and all four pins agree with
# each other about a workflow that never runs. The pin makes that edit visible in
# a diff; it does not make it fail.
#
# AND THE EDIT IS SELF-CONCEALING, which is why this has to be a `make check`
# gate rather than a CI-only one. GitHub reads a workflow's trigger set from the
# commit being evaluated, so the commit that reduces the triggers is judged under
# the REDUCED set: no run starts, nothing objects, and the pull request shows no
# failing check because it shows no check at all. CI cannot catch the edit that
# switches CI off. `make check` — and a reviewer — are what is left.
#
# So: absolute requirements, not pinned values. Two of them, and both had to be
# derived from what THIS repo claims rather than copied from the binding that
# proposed them. go's floor is "push AND pull_request", which is true of its own
# `ci.yml` and of seven others and would RED precommit.yml, whose triggers are
# `push` + `workflow_dispatch`. A floor that encodes another binding's
# configuration is not a floor, it is a copied assumption — so this one asserts
# what scripts/ci-reach.conf actually claims:
#
#   1. EVERY COUNTED WORKFLOW GATES ORDINARY WORK. It must trigger on `push` or
#      `pull_request`; neither may carry a `paths:` or `paths-ignore:` filter; and
#      where `push` is the only one of the two — precommit.yml's case — its
#      `branches:` must cover every branch, because that is the ONLY thing making
#      the conf's claim about PR heads true. A `push`-only workflow narrowed to
#      `main`, or restricted to `tags:`, gates no branch and therefore no pull
#      request. This is a floor every binding in the family passes today, and it
#      does not require `pull_request` of anybody — see WHAT IT DOES NOT PROVE
#      for the fork-PR gap it leaves precisely because it does not.
#
#   2. NO BARE NEVER-TRUE LITERAL ON A GATE JOB. `if: false` and
#      `continue-on-error: true`, spelled exactly, are refused outright. This
#      false-reds nothing: lazily-zig's advisory leg is an EXPRESSION
#      (`${{ matrix.zig == 'master' }}`), not the literal `true`, so the one
#      legitimate job-level guard in the family is untouched. A never-true
#      EXPRESSION — `if: github.event_name == 'schedule'` — is NOT caught here and
#      remains the job-guard pin's case alone; the two rungs are complementary and
#      the table above says which catches which.
#
# The floor prints AFTER the pins on purpose. The pins say what changed, which is
# the actionable half; the floor says the state is inadmissible however the pins
# read. A laundered edit gets only the floor's finding, and that is the case it
# exists for.
floor_gating=""
floor_problems=0
for act_wf in "${workflows[@]}"; do
	act_wf_trigs="$(printf '%s\n' "$act_rows" |
		awk -F'\t' -v w="$act_wf" '$1 == "trigger" && $2 == w { print $3 }' | LC_ALL=C sort -u)"
	act_gating="$(printf '%s\n' "$act_wf_trigs" | grep -xE 'push|pull_request' || true)"
	if [ -z "$act_gating" ]; then
		echo >&2
		echo "check-ci-reach: '$act_wf' is counted as CI reach and triggers on neither" >&2
		echo "                'push' nor 'pull_request':" >&2
		printf '%s\n' "$act_wf_trigs" | sed '/^$/d; s/^/  - /' >&2
		echo "                Then it runs only when somebody starts it by hand or on a" >&2
		echo "                schedule, and every gate this guard reports as reached is" >&2
		echo "                reached by a workflow that ordinary work never triggers." >&2
		echo "                This is a FLOOR, not a pin: rewriting EXPECTED_TRIGGERS to" >&2
		echo "                agree does not clear it, because the reduction is" >&2
		echo "                self-concealing — GitHub reads the trigger set from the" >&2
		echo "                commit it is evaluating, so the commit that switches CI off" >&2
		echo "                is judged under the reduced set and no run objects" >&2
		echo "                (#verifyworkflowactually)." >&2
		floor_problems=$((floor_problems + 1))
		continue
	fi
	floor_gating="$floor_gating$act_wf	$(printf '%s' "$act_gating" | paste -sd, -)"$'\n'
	for act_t in $act_gating; do
		for act_bad_filter in paths paths-ignore; do
			act_fv="$(printf '%s\n' "$act_rows" | awk -F'\t' \
				-v w="$act_wf" -v t="$act_t" -v k="$act_bad_filter" \
				'$1 == "filter" && $2 == w && $3 == t && index($4, k "=") == 1 { print $4; exit }')"
			[ -n "$act_fv" ] || continue
			echo >&2
			echo "check-ci-reach: '$act_wf''s '$act_t' trigger is narrowed by a path filter:" >&2
			echo "                  $act_fv" >&2
			echo "                A path filter means MOST commits do not start this" >&2
			echo "                workflow, so 'reached by CI' becomes 'reached on the" >&2
			echo "                commits that happen to touch those paths'. The case that" >&2
			echo "                matters: a filter excluding the Makefile means a" >&2
			echo "                gate-retiring edit does not even trigger the workflow" >&2
			echo "                that would have caught it. Refused as a FLOOR — no pin" >&2
			echo "                edit clears it. Split the packaging work into its own" >&2
			echo "                uncounted workflow instead, the way wheels.yml already" >&2
			echo "                is (#verifyworkflowactually)." >&2
			floor_problems=$((floor_problems + 1))
		done
	done
	# `push` as the ONLY gating trigger has to cover every branch, because a
	# same-repo pull request is then gated by the push to its head branch and by
	# nothing else.
	if [ "$(printf '%s' "$act_gating" | tr '\n' ' ')" = "push " ]; then
		act_br="$(printf '%s\n' "$act_rows" | awk -F'\t' -v w="$act_wf" \
			'$1 == "filter" && $2 == w && $3 == "push" && index($4, "branches=") == 1 { print substr($4, 10); exit }')"
		act_bri="$(printf '%s\n' "$act_rows" | awk -F'\t' -v w="$act_wf" \
			'$1 == "filter" && $2 == w && $3 == "push" && index($4, "branches-ignore=") == 1 { print $4; exit }')"
		act_tags="$(printf '%s\n' "$act_rows" | awk -F'\t' -v w="$act_wf" \
			'$1 == "filter" && $2 == w && $3 == "push" && index($4, "tags=") == 1 { print $4; exit }')"
		act_all_branches=0
		case ",$act_br," in
		*",**,"*) act_all_branches=1 ;;
		esac
		if [ -z "$act_br" ] && [ -z "$act_tags" ]; then
			# No branch and no tag filter at all: every push, which is broader
			# still.
			act_all_branches=1
		fi
		if [ -n "$act_bri" ]; then
			act_all_branches=0
		fi
		if [ "$act_all_branches" -ne 1 ]; then
			echo >&2
			echo "check-ci-reach: '$act_wf' gates on 'push' alone, and that push does not" >&2
			echo "                cover every branch:" >&2
			echo "                  branches=${act_br:-<absent>}  ${act_bri:-}  ${act_tags:-}" >&2
			echo "                With no 'pull_request:' trigger, the push to a pull" >&2
			echo "                request's head branch is the ONLY thing that gates that" >&2
			echo "                PR — which is exactly the claim scripts/ci-reach.conf" >&2
			echo "                makes and this floor enforces. Narrowed to a branch list," >&2
			echo "                or restricted to tags, it gates no branch and therefore" >&2
			echo "                no PR, while every verdict above stays green. Refused as" >&2
			echo "                a FLOOR: no pin edit clears it. Either restore the" >&2
			echo "                all-branches push or add a 'pull_request:' trigger" >&2
			echo "                (#verifyworkflowactually)." >&2
			floor_problems=$((floor_problems + 1))
		fi
	fi
done

while IFS="$(printf '\t')" read -r jg_wf jg_job; do
	[ -n "$jg_wf" ] && [ -n "$jg_job" ] || continue
	for jg_pair in "if:false" "continue-on-error:true"; do
		jg_key="${jg_pair%%:*}"
		jg_never="${jg_pair##*:}"
		jg_v="$(awk -F'\t' -v w="$jg_wf" -v j="$jg_job" -v k="$jg_key" \
			'$1 == w && $2 == j && $3 == k { print $4; exit }' <<<"$jg_rows")"
		jg_v="$(printf '%s' "$jg_v" | tr -d "\"'" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
		[ "$jg_v" = "$jg_never" ] || continue
		echo >&2
		echo "check-ci-reach: the gate job '$jg_job' in '$jg_wf' carries the bare literal" >&2
		echo "                  $jg_key: $jg_never" >&2
		if [ "$jg_key" = "if" ]; then
			echo "                so the job is skipped and not one of its steps runs," >&2
		else
			echo "                so the job's failure stops failing the build," >&2
		fi
		echo "                while every gate step inside it stays pinned, unique and" >&2
		echo "                unconditional. Refused as a FLOOR rather than pinned as a" >&2
		echo "                value: a pin can be rewritten to agree in the same commit." >&2
		echo "                This refuses only the BARE LITERAL, so lazily-zig's advisory" >&2
		echo "                matrix leg — an expression, not 'true' — is untouched, and a" >&2
		echo "                never-true EXPRESSION here is still the job-guard pin's case" >&2
		echo "                (#verifyworkflowactually)." >&2
		floor_problems=$((floor_problems + 1))
	done
done <<<"$gj_found"

if [ "$floor_problems" -gt 0 ]; then
	status=1
fi

if [ "$unreached_count" -gt 0 ]; then
	echo >&2
	echo "check-ci-reach: $unreached_count target(s) run by 'make $ROOT_TARGET' that the CI" >&2
	echo "                step pinned for them does not reach:" >&2
	while IFS= read -r t; do
		[ -n "$t" ] || continue
		echo "  - $t" >&2
	done <<<"$unreached"
	echo >&2
	echo "Three remedies, and they are NOT interchangeable (#reversereachdirection):" >&2
	echo "  * the gate really is missing from CI — add a step that runs it, or add an" >&2
	echo "    excuse with a reason to $CONF." >&2
	echo "  * the gate is in CI but in a DIFFERENT step — move the EXPECTED_GATE_STEPS" >&2
	echo "    entry to the step that runs it." >&2
	echo "  * the RECIPE changed under the target's name and CI still runs the old" >&2
	echo "    gate. Then the pin is right and the Makefile is the edit to look at: a" >&2
	echo "    recipe repointed at a command some other step happens to run is exactly" >&2
	echo "    the state this pin exists to refuse, and moving the pin would ratify it." >&2
	status=1
fi

# A guard that examined nothing must not report OK — the same vacuity rule the
# conformance guards apply (#lzvacuousrun).
#
# LAST, and no longer an early exit, which is a change of ORDER with a reason
# (#reversereachdirection). This floor is now restated twice over: an all-recipes-gutted
# Makefile is caught by the classification pin AND by the gate-step domain check,
# and each of those names all eight targets. Measured on this Makefile with every
# recipe replaced by `true`:
#
#   floor first (as it used to be)   "no prerequisite target carrying a gate —
#                                    nothing was verified", and nothing else
#   floor last (as it is now)        eight targets named by EXPECTED_NO_GATE_TARGETS,
#                                    eight more by EXPECTED_GATE_STEPS, then this
#
# It was pre-empting the two diagnostics that say WHICH gates went. Running it
# last keeps the only property it still has that neither pin has — it fails
# closed with both of them deleted, measured: delete the floor and both pins and
# the gutted Makefile exits 0 — while letting the better diagnostics print.
if [ "$((reached + excused_ok + unreached_count + unreadable_count))" -eq 0 ]; then
	echo >&2
	echo "check-ci-reach: '$ROOT_TARGET' has no prerequisite target carrying a gate — nothing was verified" >&2
	status=1
fi

if [ "$status" -eq 0 ]; then
	echo "check-ci-reach: OK — ${#gate_step_targets[@]} gate(s) reached INSIDE the CI step pinned for each"
	echo "check-ci-reach: OK — $reached target(s) reached by CI, $excused_ok excused, $nogate_count carrying no gate"
	echo "check-ci-reach: OK — ${#EXPECTED_TRIGGERS[@]} trigger(s), ${#EXPECTED_TRIGGER_FILTERS[@]} trigger filter(s) and ${#EXPECTED_GATE_JOBS[@]} gate job(s) pinned to exact values"
	printf '%s' "$floor_gating" | sed '/^$/d' | while IFS="$(printf '\t')" read -r f_wf f_trigs; do
		echo "check-ci-reach: OK — $f_wf gates on $f_trigs — unfiltered by path, every branch"
	done
fi
exit "$status"
