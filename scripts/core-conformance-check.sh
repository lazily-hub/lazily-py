#!/usr/bin/env bash
# Replay the canonical Core corpus and kill one assertion mutant per route.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SPEC_ROOT="${LAZILY_CORE_SPEC_ROOT:-$REPO_ROOT/../lazily-spec}"
REPORTS_ROOT="${LAZILY_CORE_REPORTS_ROOT:-$REPO_ROOT/build/core-conformance}"

export LAZILY_CORE_PROFILE_PATH="${LAZILY_CORE_PROFILE_PATH:-$SPEC_ROOT/profiles/core-v1.json}"
export LAZILY_CORE_SPEC_ROOT="$SPEC_ROOT"
export LAZILY_CORE_REPORT_PATH="${LAZILY_CORE_REPORT_PATH:-$REPORTS_ROOT/python.json}"
export LAZILY_CORE_REPORTS_ROOT="$REPORTS_ROOT"

mkdir -p "$REPORTS_ROOT"
exec uv run python "$REPO_ROOT/scripts/generate-core-conformance.py"
