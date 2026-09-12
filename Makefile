.PHONY: init build test lint lint-fix format-check format-fix type-check clean publish-test publish bench bench-scale compile conformance-coverage assertion-ordering-check ci-reach

# ---- One run id per make invocation (#lzstalemanifest) ----
#
# `scripts/check-conformance-coverage.sh` is the ONE guard in this binding whose
# evidence is written by a DIFFERENT process than the one that reads it: pytest
# records `build/conformance-fixtures-loaded.txt`, the script reads it back. Every
# other rung (assertion-block bind, unconsumed key, read-but-unasserted key,
# unreplayed scenario) runs inside `pytest_sessionfinish`, in the same process
# that opened the fixtures, so a stale file is unreachable for those.
#
# Before this, that split guard had no freshness check at all, and the manifest
# outlives any single invocation: `make conformance-coverage` on its own — or a
# `poe test -k something` that APPENDS a handful of reads onto a full previous
# run's list — was accepted as evidence that "these bytes were really read" by
# THIS run. Demonstrated, not argued: with a 145-line manifest left on disk from
# an earlier run and no pytest invoked at all, the guard printed
# "conformance coverage OK: 145/156 ... these bytes were really read" and exited 0.
#
# So: one id per invocation, generated ONCE here, exported to every step. The
# `test` target's first command stamps it as the manifest's first line and the
# coverage guard refuses any manifest not carrying THIS id.
#
# `:=`, not `=`. A recursively-expanded variable would re-run `$(shell ...)` at
# every reference, so the truncate step and the guard would stamp and demand
# DIFFERENT ids — a fail-closed outcome, but a baffling one.
LAZILY_CONFORMANCE_RUN_ID := make-$(shell date -u +%Y%m%dT%H%M%S)-$(shell od -An -N6 -tx1 /dev/urandom | tr -d ' \n')
export LAZILY_CONFORMANCE_RUN_ID

# Install development dependencies and package in editable mode
init: PY_VERSION = $(shell [ -f .python-version ] && \
	cat .python-version || \
	uv run python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" \
)
init:
	@echo "Using Python version: $(PY_VERSION)"

	@if command -v mise >/dev/null 2>&1; then \
		mise install; \
	fi

	uv venv .venv --python "$(PY_VERSION)" --no-project --clear --seed $(VENV_ARGS)

	@if [ -n "$(ALL)" ]; then \
		uv sync --python "$(PY_VERSION)" --all-groups --all-extras $(SYNC_ARGS); \
	else \
		uv sync --python "$(PY_VERSION)" $(SYNC_ARGS); \
	fi

# Run tests
#
# Delegates to the poe tasks rather than repeating the manifest env var and the
# truncation here. CI runs `poe precommit`, so any setup that lives only in this
# file is setup CI does not do — which is how the coverage guard came to run
# nowhere but a developer's machine (#lzguardsnotinci).
test:
	uv run poe conformance_manifest
	uv run poe test

# Compile the reactive core with mypyc (in-place .so files). Idempotent —
# rebuild after editing src/lazily/{slot,cell,signal,effect,batch}.py to run
# tests/benchmarks against the compiled code. If mypyc is unavailable this is a
# no-op and tests/benches fall back to the pure-Python sources.
compile:
	-uv run mypyc --follow-imports=silent --config-file=pyproject.toml \
		src/lazily/slot.py src/lazily/cell.py src/lazily/signal.py \
		src/lazily/effect.py src/lazily/batch.py

# Run tests with coverage
test-cov:
	uv run pytest tests/ --cov=lazily --cov-report=html --cov-report=term-missing

# The formatting and lint GATES (#lzruffautofixvacuity).
#
# Both are spelled non-repairing on purpose. A gate that rewrites the tree it is
# judging cannot fail: `ruff format` reformats and exits 0, and a bare
# `ruff check` used to auto-fix because pyproject set `[tool.ruff] fix = true`,
# so `make check` repaired its way to green and a developer could not reproduce
# what CI enforces. `fix = true` is gone from pyproject as well, so a bare
# `ruff check` gates everywhere; `--no-fix` here is the belt to that braces —
# re-adding the setting cannot silently un-gate this target.
#
# The repairing forms live in `format-fix` / `lint-fix`, named so that running
# one is a decision rather than a side effect. Neither is in `check`.
format-check:
	uv run ruff format --check src/lazily/ tests/

lint:
	uv run ruff check --no-fix src/lazily/ tests/

# Repairing forms — rewrite the tree. Never in `check`.
format-fix:
	uv run ruff format src/lazily/ tests/

lint-fix:
	uv run ruff check --fix src/lazily/ tests/

# Type check
type-check:
	poe ty

# Run all checks
.PHONY: test-interop-peer
test-interop-peer:
	uv run poe interop_peer

assertion-ordering-check:
	python3 ../lazily-spec/scripts/check-assertion-ordering.py --binding py --root .

check: format-check lint type-check test conformance-coverage test-interop-peer assertion-ordering-check ci-reach

# Run the micro-benchmark suite (see BENCHMARKS.md)
bench:
	uv run python -m lazily.benchmarks

# Run the large spreadsheet-shaped scale suite (see BENCHMARKS.md).
# Override size/viewport with LAZILY_SCALE_N / LAZILY_SCALE_VIEWPORT, e.g.:
#   LAZILY_SCALE_N=5000000 make bench-scale   # Google Sheets 10M-cell workbook
bench-scale:
	uv run python -m lazily.scale_bench

# Clean build artifacts
clean:
	rm -rf dist/ build/ *.egg-info/
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete

# Build package
build: clean
	python -m build

# Publish to TestPyPI
publish-test: build
	python -m twine upload --repository testpypi dist/*

# Publish to PyPI
publish: build
	@echo "WARNING: This will publish to the real PyPI!"
	@read -p "Are you sure? (y/N) " confirm && [ "$$confirm" = "y" ]
	python -m twine upload dist/*

# Conformance-coverage guard (#portconformancecoverage). Static: fails when the
# canonical corpus grows a fixture no test in this repo even names. Naming is not
# replaying — see the script header for what this does and does not prove.
conformance-coverage:
	uv run poe conformance_coverage

# CI-reachability guard (#lzcheckcireachguard). Fails when a target above runs a
# gate no CI workflow step reaches — the drift that hid #lzinteroppeerci in every
# binding for months. In this binding it also catches a second shape of the same
# problem: a workflow that runs one opaque aggregate command, under which a gate
# can quietly stop running without any step name changing. It guards itself:
# `ci-reach` is in `check`, so CI has to run it too or this target reports itself
# missing.
ci-reach:
	./scripts/check-ci-reach.sh
