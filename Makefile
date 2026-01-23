.PHONY: init build test lint format type-check clean publish-test publish

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
test:
	uv run pytest tests/ -v

# Run tests with coverage
test-cov:
	uv run pytest tests/ --cov=lazily --cov-report=html --cov-report=term-missing

# Format code with ruff
format:
	uv run ruff format lazily/ tests/

# Lint code with ruff
lint:
	ruff check lazily/ tests/

# Type check
type-check:
	mypy lazily/

# Run all checks
check: format lint type-check test

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