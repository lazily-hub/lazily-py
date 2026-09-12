#!/usr/bin/env bash
# Conformance-coverage guard (#portconformancecoverage).
#
# Fails the build when the canonical corpus in ../lazily-spec/conformance/ grows a
# fixture that no test in this repo even mentions. That is the drift this guard
# exists for: a fixture lands upstream, every binding stays green, and nobody
# learns that one of them is not replaying it.
#
# This binding uses the RUNTIME manifest (#lazilyupgradeconformance), not the
# static grep it started with. The test run records every file it actually reads
# from the conformance corpus, so a fixture named in a comment but hand-transcribed
# — the drift found in lazily-cpp's queue tests — is caught here. A source grep
# cannot see that case at all.
#
# A missing manifest is missing EVIDENCE and fails. It does not mean "no fixtures
# were read"; it means the suite ran without the recorder attached, and passing in
# that state is the vacuous green this guard exists to prevent.
#
# A manifest from ANOTHER invocation fails for the same reason (#lzstalemanifest).
# This is the one rung in this binding whose evidence crosses a process boundary —
# pytest writes it, this script reads it — and the file outlives any single run, so
# the run id stamped on line 1 has to match the invocation running this guard. See
# the freshness block below for what was measured before that check existed.
#
# The same reasoning is why this script ends with a positive-evidence FLOOR
# (#lzvacuousrun). Every check between here and there is a statement about the
# fixtures the run opened, and every one of them is trivially satisfied when that
# set is empty. Reporting "coverage OK" after examining zero fixtures is the
# failure mode, not a degenerate case of success, so the magnitude is asserted
# before OK is printed.
set -euo pipefail

SPEC_DIR="${LAZILY_SPEC_CONFORMANCE_DIR:-../lazily-spec/conformance}"
if [ ! -d "$SPEC_DIR" ]; then
  # Skipping is right for a local checkout without the sibling clone, and wrong
  # everywhere the run is supposed to PROVE something (#lzvacuousrun). On CI (or
  # with LAZILY_CONFORMANCE_REQUIRE_CORPUS=1) an absent corpus is the vacuous
  # green this whole ladder exists to reject: every rung below reasons about
  # fixtures the run OPENED, so an absent corpus reports OK over nothing at all.
  # Under CI that is missing EVIDENCE, not evidence of absence — the checkout is
  # wrong, not the corpus.
  if [ -n "${CI:-}" ] || [ -n "${LAZILY_CONFORMANCE_REQUIRE_CORPUS:-}" ]; then
    echo "::error::canonical corpus not found at $SPEC_DIR — the coverage guard would" >&2
    echo "         compare against nothing and pass vacuously. Clone lazily-spec as a" >&2
    echo "         sibling, or point LAZILY_SPEC_CONFORMANCE_DIR at a copy." >&2
    exit 1
  fi
  echo "SKIP: canonical corpus not found at $SPEC_DIR (clone the lazily-spec sibling)" >&2
  echo "      Local checkout only — this would be a hard failure under CI." >&2
  exit 0
fi

# Fixtures deliberately not covered by this binding yet. Each entry is a claim that
# someone looked; shrinking this list is the work. Adding to it silently is how the
# guard rots, so keep a reason with any new entry.
#
# This list is half of what lazily-py does not prove. The other half is
# SCENARIO_EXCUSES in tests/conformance_assert.py (#lzscenariocoverage): this list
# names whole fixtures this binding never opens, that one names scenarios inside a
# fixture it DOES open, which this guard cannot see because opening a fixture for
# one scenario satisfies it. Read the two together, and keep them disjoint — a
# fixture named here is already excused a level up and must not also carry scenario
# excuses.
KNOWN_UNCOVERED=(
  # Register CRDTs (LWW / MV / PnCounter + the CellCrdt projection bit) are
  # implemented here, but this binding has no canonical replay for the new
  # registers corpus yet; the Registers coverage row is `~` until it does.
  "collections/registers_convergence.json"
  # Reactive egress is currently Rust-only; Python has no egress replay runner.
  "egress/egress_generation_fence.json"
  "egress/egress_inflight_window.json"
  "egress/egress_ordered_ack.json"
  "egress/egress_retry_budget.json"
  # Experimental protobuf-v1 generation is piloted in Rust/Kotlin/TypeScript;
  # Python must negotiate the capability before replaying this typed trace.
  "protobuf/graph_boundary_traces.json"
  "agent-doc/delta_agent_doc_state.json"
  "agent-doc/snapshot_agent_doc_state.json"
  "reliable-sync/coalesce_bounds_outbox.json"
  "reliable-sync/liveness_lease_eviction.json"
  # The canonical journal-decoder trace has no Python replay runner yet.
  "reliable-sync/outbox_journal_decode.json"
)

MANIFEST="${LAZILY_CONFORMANCE_MANIFEST:-build/conformance-fixtures-loaded.txt}"
TEST_DIRS=("tests")
EXTS=(".py")

collect_sources() {
  for d in "${TEST_DIRS[@]}"; do
    [ -d "$d" ] || continue
    for e in "${EXTS[@]}"; do
      find "$d" -type f -name "*$e" -print0
    done
  done
}

# Absent, or zero bytes. This is NOT the emptiness check any more
# (#lzstampsatisfiesnonempty): since the run id is stamped at TRUNCATE time,
# before the suite starts, the smallest file this protocol can leave behind is a
# stamp line and nothing else — non-empty, ~50 bytes, recording zero reads. `-s`
# passed on it. The records floor below the freshness block is `-s` restated
# against RECORDS, and it is the one that sees that state.
if [ ! -s "$MANIFEST" ]; then
  echo "FAIL: no conformance manifest at $MANIFEST." >&2
  echo "      Run the suite with LAZILY_CONFORMANCE_MANIFEST set so the recorder" >&2
  echo "      attaches. An absent manifest is missing evidence, not evidence of" >&2
  echo "      absence." >&2
  exit 1
fi

# ---- Freshness: is this manifest THIS invocation's evidence? (#lzstalemanifest)
#
# Everything below — and every line of the OK message — is a claim about what
# THIS run opened. The manifest is the only input to that claim, and it is
# written by a DIFFERENT process (pytest) than the one reading it, in a file that
# outlives any single invocation. Without the check below, running this script on
# its own read a manifest left behind by an earlier run and reported "these bytes
# were really read" about bytes this invocation never touched. Measured, not
# supposed: against a 145-line manifest from an earlier run, with no pytest
# invoked at all, it printed "conformance coverage OK: 145/156" and exited 0.
#
# The routes that reach it are not build caching — pytest caches no execution —
# but they are ordinary:
#
#   * `make conformance-coverage` / `poe conformance_coverage` alone, after any
#     earlier run left a manifest in build/.
#   * `poe test -k something`, which has the recorder attached and APPENDS: a
#     partial run's reads land on top of a full run's list, and the union reads
#     as complete coverage.
#   * a `make check` whose pytest step dies, followed by the guard run by hand.
#
# So the manifest must carry the id of the invocation that produced it. The
# TRUNCATE step stamps `# lazily-run-id <id>` as line 1 — `conformance_manifest`
# in pyproject.toml, which runs BEFORE pytest; `_write_manifest` in
# tests/conftest.py re-stamps only when it finds a foreign id. This compares the
# stamp to the id in the environment and refuses on any mismatch, naming both.
#
# Because the stamp lands before the suite does, "stamped" and "carries
# evidence" are independent: see the records floor below
# (#lzstampsatisfiesnonempty), which is the check that a stamp alone no longer
# satisfies.
#
# An UNSET id refuses rather than skips. A guard that accepts unstamped evidence
# whenever the variable is missing is the same hole with one extra step, since
# the variable is missing in precisely the hand-invoked case that exposed it.
#
# There is NO opt-out flag. There was: `LAZILY_CONFORMANCE_ALLOW_UNSTAMPED_EVIDENCE=1`
# skipped the whole freshness block and printed a warning, which is a boolean a
# person can set once in a shell profile or a config and never see again — and
# whose entire meaning is "do not check the thing this block exists to check".
# lazily-js's shape is better and is adopted here: the escape hatch for reading a
# manifest by hand is to ADOPT the id the manifest is stamped with, named
# explicitly on the command line (see the message below). That cannot be set
# blindly — a bare `=1` buys nothing, you have to read the id off the file — it
# still verifies that a stamp is present and that the verdict is about the run
# you named, and it leaves the operator's claim in their shell history.
RUN_ID="${LAZILY_CONFORMANCE_RUN_ID:-}"
if [ -z "$RUN_ID" ]; then
  echo "FAIL: LAZILY_CONFORMANCE_RUN_ID is unset, so this guard cannot tell whether" >&2
  echo "      $MANIFEST is THIS invocation's evidence or a leftover from an" >&2
  echo "      earlier run (#lzstalemanifest). Every check below, and the OK line" >&2
  echo "      itself, is a claim about the fixtures THIS run opened." >&2
  echo "      Run 'make conformance-coverage' or 'poe precommit', which generate" >&2
  echo "      an id and stamp it into the manifest. To inspect an existing" >&2
  echo "      manifest by hand, adopt the id it is stamped with — the id is the" >&2
  echo "      last field of its first line — and say so out loud:" >&2
  echo "        LAZILY_CONFORMANCE_RUN_ID=\"\$(head -n 1 $MANIFEST | awk '{print \$NF}')\" \\" >&2
  echo "          $0" >&2
  exit 1
fi
STAMP_PREFIX="# lazily-run-id "
FIRST_LINE="$(head -n 1 "$MANIFEST")"
case "$FIRST_LINE" in
"$STAMP_PREFIX"*) FOUND_ID="${FIRST_LINE#"$STAMP_PREFIX"}" ;;
*) FOUND_ID="" ;;
esac
if [ -z "$FOUND_ID" ]; then
  echo "FAIL: $MANIFEST carries no run-id stamp on its first line." >&2
  echo "      Wanted a first line reading '${STAMP_PREFIX}$RUN_ID'; found:" >&2
  echo "        $FIRST_LINE" >&2
  echo "      An unstamped manifest predates the freshness protocol, or was" >&2
  echo "      written by something that bypassed the truncate step — either way" >&2
  echo "      it is not evidence about this run (#lzstalemanifest). Run the suite:" >&2
  echo "        make test    (or: uv run poe conformance_manifest && uv run poe test)" >&2
  exit 1
fi
if [ "$FOUND_ID" != "$RUN_ID" ]; then
  echo "FAIL: $MANIFEST is a DIFFERENT run's evidence (#lzstalemanifest)." >&2
  echo "        file:  $MANIFEST" >&2
  echo "        found: $FOUND_ID" >&2
  echo "        want:  $RUN_ID" >&2
  echo "      The manifest outlives an invocation and the recorder appends, so a" >&2
  echo "      guard that trusts it credits this run with fixtures an earlier one" >&2
  echo "      opened. Re-run the suite in the same invocation as this guard:" >&2
  echo "        make check      (truncates, stamps, replays, then checks)" >&2
  exit 1
fi

# `sed`, not `grep -v`: the stamp line is dropped from the opened set, and a
# manifest holding ONLY the stamp (pytest recorded nothing) must reach the
# records floor immediately below with its own message rather than dying here on
# `grep`'s no-match exit status under `set -e`.
OPENED="$(sed '/^# lazily-run-id /d' "$MANIFEST" | sort -u)"

# ---- Records floor: stamped, and recording nothing (#lzstampsatisfiesnonempty)
#
# The stamp is written at TRUNCATE time by `conformance_manifest`, before pytest
# starts, so "stamped" and "carries evidence" came apart the moment the run-id
# protocol landed. A `make check` whose pytest step dies after the truncate step
# — or a `poe test` that runs with the recorder detached — leaves a file that is
# non-empty, carries THIS invocation's id, and names zero fixtures. Both checks
# that used to catch an evidence-free manifest are satisfied by it: `-s` above
# because a stamp is bytes, and the freshness block because the id is the right
# one.
#
# It is not that the guard then passed — it did not; the per-fixture loop below
# reported all 145 unexcused canonical fixtures as "NOT opened" and exited 1. But
# that is a coverage verdict standing in for an evidence verdict, and it reads as
# 145 regressions rather than as one detached recorder. The set-shaped diagnostic
# is also only correct by the shape of the corpus: it is the loop below that
# happens to have 145 unexcused fixtures to complain about, and every line of it
# is a statement about the opened set being empty — which is the condition, said
# 145 times and mislabelled.
#
# So name the condition once, here, before anything reasons about coverage. This
# is distinct from the `covered -eq 0` floor at the bottom, which fires when the
# manifest HAS records that are all attributed away (a KNOWN_UNCOVERED list that
# excuses the whole corpus, or attribution pointed at the wrong root).
if [ -z "$OPENED" ]; then
  echo "FAIL: $MANIFEST carries this run's id and NO records" >&2
  echo "        file:  $MANIFEST" >&2
  echo "        found: the run-id stamp and nothing else" >&2
  echo "        want:  one corpus-relative fixture path per line below the stamp" >&2
  echo "      The stamp is written when the manifest is TRUNCATED, before the suite" >&2
  echo "      runs, so this is what a pytest that never started — or died before" >&2
  echo "      pytest_sessionfinish, or ran with the recorder detached — leaves" >&2
  echo "      behind (#lzstampsatisfiesnonempty). A stamp is bytes, not evidence:" >&2
  echo "      that is why this is checked against RECORDS and not with 'test -s'." >&2
  echo "      Re-run the suite and this guard in one invocation:" >&2
  echo "        make check" >&2
  exit 1
fi

missing=0
total=0
covered=0
while IFS= read -r fixture; do
  total=$((total + 1))
  name="$(basename "$fixture")"
  # Here-string, NOT a pipe. With `set -o pipefail`, `printf ... | grep -q` reports
  # FAILURE when grep matches: grep -q exits immediately on the first hit, printf
  # takes SIGPIPE writing the rest, and pipefail surfaces printf's death as the
  # pipeline's status. The check then inverts — every covered fixture is reported
  # missing. That is exactly how it behaved before this line changed.
  if grep -qxF "$fixture" <<< "$OPENED"; then
    covered=$((covered + 1))
    continue
  fi
  excused=0
  # `[@]+...`, not `[@]:-`. Under `set -u` an EMPTY array spelled `"${a[@]:-}"`
  # expands to one EMPTY STRING rather than to nothing, so emptying the allowlist
  # — which is the goal state, "shrinking this list is the work" — injects a
  # phantom entry `''` into both loops. Here it is only a wasted comparison; in
  # the stale-allowlist loop below it hard-fails every run.
  for known in ${KNOWN_UNCOVERED[@]+"${KNOWN_UNCOVERED[@]}"}; do
    if [ "$known" = "$fixture" ]; then excused=1; break; fi
  done
  if [ "$excused" -eq 0 ]; then
    echo "ERROR: canonical fixture '$fixture' was NOT opened by the suite." >&2
    echo "       A runner may still name it in source while no longer reading it —" >&2
    echo "       that is the drift this manifest exists to catch. Replay it, or add" >&2
    echo "       it to KNOWN_UNCOVERED with a reason." >&2
    missing=$((missing + 1))
  fi
done < <(cd "$SPEC_DIR" && find . -name '*.json' | sed 's|^\./||' | sort)

# A stale allowlist is its own drift, in two directions:
#
#   1. An entry naming a fixture that no longer exists means the corpus moved and
#      nobody updated the excuse.
#   2. An entry naming a fixture the suite DOES open is a stale excuse. Nobody
#      files a bug about coverage they are told they lack, so a stale excuse hides
#      real work already done and pads the list until the genuine gaps are
#      unreadable. This is ledger rot in the understating direction.
#
# The covered-check above and the stale-check below use the SAME comparison
# (`grep -qxF` against "$OPENED") so the two can never disagree about whether a
# given fixture was opened.
for known in ${KNOWN_UNCOVERED[@]+"${KNOWN_UNCOVERED[@]}"}; do
  if [ ! -f "$SPEC_DIR/$known" ]; then
    echo "ERROR: KNOWN_UNCOVERED lists '$known', which is not in the canonical corpus." >&2
    missing=$((missing + 1))
    continue
  fi
  if grep -qxF "$known" <<< "$OPENED"; then
    echo "ERROR: KNOWN_UNCOVERED lists '$known', but the suite DID open it." >&2
    echo "       The excuse is stale: this fixture is covered. Delete the entry from" >&2
    echo "       KNOWN_UNCOVERED so the list keeps naming only the real gaps." >&2
    missing=$((missing + 1))
  fi
done

if [ "$missing" -gt 0 ]; then
  echo "conformance coverage FAILED: $missing problem(s)" >&2
  exit 1
fi

# ---- Positive-evidence floor (#lzvacuousrun) ----
# Everything above reasons about fixtures this run OPENED, so all of it is
# vacuously satisfied by an empty population: zero fixtures means zero uncovered
# fixtures and zero stale excuses. The loops cannot distinguish "nothing is
# wrong" from "nothing was examined", so assert the MAGNITUDE explicitly before
# reporting OK. Do not lower these to fix a red run — a drop here means the
# corpus or the recorder shrank, which is the finding.
#
# There is NO fixture COUNT pinned here any more (#lzledgerceiling). There was:
# a MIN_FIXTURES bash default of 145 with a same-named env override, compared
# `covered -lt MIN_FIXTURES`. Deliberately not restated in its literal form: the
# upstream audit (lazily-spec scripts/check-corpus-floors.mjs) greps this whole
# `scripts/` directory for that exact spelling, so quoting it here would have the
# audit keep deriving and comparing a pin that no longer runs — a retired floor
# reported as a live one.
#
# It mirrored a number the set checks above already fix exactly. With zero errors
# reported, every canonical fixture outside KNOWN_UNCOVERED was opened, every
# KNOWN_UNCOVERED entry exists in the corpus, and none of them was opened — so
# `covered` IS `total` minus the ledger, which is 156 - 11 = 145, the pin's own
# value. Equal sets have equal counts: the pin carried no information the loops
# had not already established, and it could not fire. Demonstrated rather than
# argued: dropping `arena_blob.json` from the runtime manifest takes `covered` to
# 144, one under the old pin, and the run dies at "canonical fixture
# 'arena_blob.json' was NOT opened by the suite" — the earlier `exit 1`, reached
# before the floor is evaluated at all.
#
# What it did cost was real. It was a hand-retyped literal read off a CI log, and
# its own comment block was a ledger of the rot: 132 -> 139 -> 141 -> 145, with
# the 132 pin sitting seven fixtures behind the live count and the 141 pin four
# behind (#lzscenariofloordrift). A number that must be re-pinned whenever the
# corpus moves is an edit site that drifts, and `-lt` cannot see the drift.
#
# The one thing the pin asserted that the set checks do NOT imply is kept below,
# at ZERO: if KNOWN_UNCOVERED ever excused the WHOLE corpus, every loop above
# would be silent and `covered` would be 0 with nothing wrong reported. That is a
# ceiling on how much may be excused, spelled as a floor on what must remain — it
# is policy, it does not move with the corpus, and it never needs re-pinning.
# The block-level ledger carries the same kind of policy in
# `_EXPECTED_LEDGERED_BLOCKS` in tests/conformance_assert.py — pinned there as an
# exact size rather than a bound, because a bound that only refuses growth gains
# slack with every migration and stops firing (#lzledgerratchet).
if [ "$total" -eq 0 ]; then
  echo "ERROR: the corpus at $SPEC_DIR listed ZERO fixtures." >&2
  echo "       Every check above is vacuously green over an empty population:" >&2
  echo "       no fixture can be uncovered when there are no fixtures. The" >&2
  echo "       directory exists but holds no *.json — wrong path or wrong" >&2
  echo "       checkout (#lzvacuousrun)." >&2
  exit 1
fi
if [ "$covered" -eq 0 ]; then
  echo "ERROR: the corpus at $SPEC_DIR listed $total fixture(s) and the suite OPENED" >&2
  echo "       NONE of them. Every check above is a statement about the fixtures" >&2
  echo "       this run opened and all of them are vacuously true over an empty" >&2
  echo "       set, so nothing above reported a problem. Either KNOWN_UNCOVERED" >&2
  echo "       now excuses the entire corpus, or the recorder never attached" >&2
  echo "       (#lzvacuousrun). This floor is ZERO on purpose: it is the part of" >&2
  echo "       the old MIN_FIXTURES pin the set checks above do not already imply," >&2
  echo "       and unlike that pin it does not move when the corpus does." >&2
  exit 1
fi

echo "conformance coverage OK: $covered/$total canonical fixtures OPENED by the suite" \
     "(${#KNOWN_UNCOVERED[@]} listed as known-uncovered; runtime manifest — these bytes were really read)"
