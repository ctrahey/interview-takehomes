.DEFAULT_GOAL := help

# Override on the command line if `uv` on PATH is shadowed by something else,
# e.g. `make check UV=/path/to/real/uv`.
UV ?= uv

# Load FIREWORKS_API_KEY from ~/.fireworks-key if present, without ever
# printing its value. The file is treated as opaque data (its whole contents,
# trimmed of a trailing newline, become the key) -- it is never sourced or
# executed as a shell script, and an already-set env var always wins.
define load_key
if [ -z "$$FIREWORKS_API_KEY" ] && [ -f "$$HOME/.fireworks-key" ]; then \
	export FIREWORKS_API_KEY="$$(cat "$$HOME/.fireworks-key")"; \
fi
endef

.PHONY: help setup test lint format typecheck import-contract check eval serve demo clean

help:
	@echo "Targets: setup test lint typecheck import-contract check eval serve demo clean"

## Install the workspace (all members + dev dependency group).
setup:
	$(UV) sync --all-packages

## Run the offline test suite (packages/*/tests, tests/, evals/).
test:
	$(UV) run --all-packages pytest

## Ruff lint + format check.
lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

## Ruff auto-fix + format (not part of `check`; convenience only).
format:
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

## mypy over our four packages.
typecheck:
	$(UV) run mypy

## The t2s_core -> {foundation, t2s_api, t2s_cli} forbidden-import contract (D4).
import-contract:
	$(UV) run lint-imports --config pyproject.toml

## Everything CI runs, offline, no Fireworks key required.
check: lint typecheck import-contract test
	@echo "check: OK"

## Live evaluation run against the Fireworks API. Requires FIREWORKS_API_KEY.
## Not implemented until W4 (eval harness) lands.
eval:
	@$(load_key); \
	if [ -z "$$FIREWORKS_API_KEY" ]; then \
		echo "eval: FIREWORKS_API_KEY is not set (checked env and ~/.fireworks-key)."; \
		echo "      Set it, or run 'make test' for the offline suite instead."; \
	fi; \
	if [ ! -f evals/harness/__main__.py ] && [ ! -f evals/harness/run.py ]; then \
		echo "eval: not implemented yet (W4) -- evals/harness/ has no runner module."; \
		exit 1; \
	fi; \
	$(UV) run python -m evals.harness

## Run the FastAPI app locally. Not implemented until W5 (api) lands.
serve:
	@if ! $(UV) run python -c "import t2s_api.app" >/dev/null 2>&1; then \
		echo "serve: not implemented yet (W5) -- t2s_api.app:app does not exist."; \
		exit 1; \
	fi; \
	$(load_key); $(UV) run uvicorn t2s_api.app:app --reload

## End-to-end CLI demo. Not implemented until W1 + W7 land.
demo:
	@$(load_key); \
	if ! $(UV) run python -c "from t2s_cli.cli import cli" >/dev/null 2>&1; then \
		echo "demo: not implemented yet (W1/W7) -- t2s_cli has no runnable commands."; \
		exit 1; \
	fi; \
	if [ "$$($(UV) run t2s --help 2>/dev/null | grep -c Commands:)" = "0" ]; then \
		echo "demo: not implemented yet (W7) -- the CLI has no commands registered."; \
		exit 1; \
	fi; \
	$(UV) run t2s --help

## Remove caches and the virtual environment.
clean:
	rm -rf .venv .pytest_cache .mypy_cache .ruff_cache .import_linter_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
