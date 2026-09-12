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
#   For every target in `check`'s prerequisite closure, at least one CI `run:`
#   step invokes the same program with the same distinguishing flags.
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
#   Each of the three was measured on this Makefile at exit 0 before the pin it
#   answers existed; the cases are recorded above each one. The three divide
#   cleanly, and the division is worth keeping straight when reading a failure:
#   the oracle says the EXECUTED set matches the modelled one, the membership pin
#   says the MODELLED set has not changed unnoticed, and the classification pin
#   says no modelled member has been hollowed out. None of them says the set is
#   CORRECT. `check`'s vacuity floor further down restates, independently of all
#   three, that the audit did not evaluate nothing — in this binding the
#   classification pin now covers every state that reaches it (measured: with the
#   floor deleted, an all-recipes-gutted Makefile is caught by the classification
#   pin, naming all eight targets), so the floor is a cheap restatement that
#   survives the pins being deleted rather than the thing doing the catching.
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
# WHAT IT DOES NOT PROVE
#
#   That CI runs it against the same inputs, in the same environment, or that the
#   command means the same thing there. Reach is a floor, not equivalence. The
#   sibling guards (conformance-coverage, assertion-keys, scenario-coverage) are
#   what prove a run examined anything.
#
#   And — stated plainly rather than left to be discovered — that a target's
#   recipe still runs the gate its NAME claims. Swap one for a command CI already
#   runs (`type-check:` running `ruff check --no-fix src/lazily/ tests/`) and all
#   three pins above hold: membership is unchanged, the oracle sees the new
#   command on both sides, the classification is still "carries a gate", the
#   anchor matches a real CI step, and every count is the same. Closing it needs a
#   per-target recipe anchor inside this guard — a second spelling of every recipe,
#   which is the mistake recorded in HOW A TARGET IS MATCHED below: it cost
#   lazily-cpp a hardcoded duplicate path plus a hand-written equality assertion,
#   a new drift surface invented to satisfy a drift detector. So it is out of
#   scope, and it bounds the honest claim for this work: a pin turns an invisible
#   drop into a reviewable edit, and a recipe swap is an equally reviewable edit
#   that stays equally undetected here. The reviewer of the diff is the control,
#   not this script.
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
#   its anchors is a subsequence of some CI command's token list, or when CI runs
#   `make <target>` directly. Every, not any: a target that runs two gates and is
#   half-covered by CI is a gap, and "any" would report it green.
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

# Command lines from every `run:` step. Comment lines inside a run body are
# stripped here — the whole reason this guard is a script.
ci_commands() {
	awk '
		function flush() { if (buf != "") { print buf; buf = "" } }
		{
			line = $0
			indent = match(line, /[^ ]/) - 1
			if (indent < 0) indent = 9999

			if (inblock) {
				if (line ~ /^[[:space:]]*$/) next
				if (indent <= block_indent) { flush(); inblock = 0 }
				else {
					sub(/^[[:space:]]+/, "", line)
					if (substr(line, 1, 1) == "#") next
					if (line ~ /\\[[:space:]]*$/) {
						sub(/\\[[:space:]]*$/, "", line)
						buf = buf " " line
						next
					}
					if (buf != "") { print buf " " line; buf = "" } else print line
					next
				}
			}

			if (line ~ /^[[:space:]]*(-[[:space:]]+)?run:[[:space:]]*[|>][-+]?[[:space:]]*$/) {
				inblock = 1
				block_indent = indent
				buf = ""
				next
			}
			if (line ~ /^[[:space:]]*(-[[:space:]]+)?run:[[:space:]]*[^|>[:space:]]/) {
				sub(/^[[:space:]]*(-[[:space:]]+)?run:[[:space:]]*/, "", line)
				print line
			}
		}
		END { flush() }
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

ci_raw="$(mktemp)"
ci_anchor="$(mktemp)"
trap 'rm -f "$ci_raw" "$ci_anchor"' EXIT
ci_commands "${workflows[@]}" >"$ci_raw"
anchors <"$ci_raw" | sort -u >"$ci_anchor"

if [ ! -s "$ci_anchor" ]; then
	echo "check-ci-reach: no run: steps found in ${workflows[*]} — a guard with an empty haystack passes everything" >&2
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
	if ! make_invokes "$target"; then
		while IFS= read -r a; do
			[ -n "$a" ] || continue
			if ! anchor_reached "$a"; then
				hit=0
				missing_anchors="$missing_anchors$a"$'\n'
			fi
		done <<<"$target_anchors"
	fi

	if is_excused "$target"; then
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
			printf '           no CI run: step matches `%s`\n' "$a"
		done <<<"$missing_anchors"
	fi
done <<<"$closure"

while IFS= read -r target; do
	[ -n "$target" ] || continue
	printf 'no gate  %-32s recipe runs no checkable command\n' "$target"
done <<<"$nogate"

# A guard that examined nothing must not report OK — the same vacuity rule the
# conformance guards apply (#lzvacuousrun).
if [ "$((reached + excused_ok + unreached_count + unreadable_count))" -eq 0 ]; then
	echo "check-ci-reach: '$ROOT_TARGET' has no prerequisite target carrying a gate — nothing was verified" >&2
	exit 1
fi

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

if [ "$unreached_count" -gt 0 ]; then
	echo >&2
	echo "check-ci-reach: $unreached_count target(s) run by 'make $ROOT_TARGET' that no CI run: step reaches:" >&2
	while IFS= read -r t; do
		[ -n "$t" ] || continue
		echo "  - $t" >&2
	done <<<"$unreached"
	echo >&2
	echo "Add a CI step that runs it, or add an excuse with a reason to $CONF." >&2
	status=1
fi

if [ "$status" -eq 0 ]; then
	echo "check-ci-reach: OK — $reached target(s) reached by CI, $excused_ok excused, $nogate_count carrying no gate"
fi
exit "$status"
