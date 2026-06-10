# vmlease — canonical project commands (all run through `uv`).
#
# NOTE: `ruff format` is intentionally NOT used anywhere — it is banned in
# pyproject.toml (it has a code-mangling bug). `make format` applies ruff's safe
# lint autofixes instead. Use these targets; don't invoke the tools ad hoc.
#
# Output is kept quiet: `@` (no command echo), `uv run --quiet` (no env-resync
# noise), `ruff --output-format concise`, and `unittest --buffer` (a passing
# test's stdout/stderr is shown only if it fails). `pipefail` keeps a failing
# step in a piped recipe from being masked.

SHELL := /bin/bash
.SHELLFLAGS := -o pipefail -c
.DEFAULT_GOAL := help
.PHONY: help install lint lint-battery format typecheck test coverage check hooks clean

help:  ## show this help
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install:  ## sync the env + dev tools
	@uv sync --quiet

lint:  ## strict lint, no autofix
	@uv run --quiet ruff check -q --output-format concise src tests && echo "✓ lint clean (exit 0)"

lint-battery:  ## shellcheck the example battery bundle (severity-gated gate; requires shellcheck)
	@uv run --quiet vmlease lint --battery examples/compose-plugin-check/battery.toml --severity error --require-shellcheck

format:  ## apply ruff's SAFE autofixes (NOT `ruff format`, which is banned)
	@uv run --quiet ruff check --fix src tests

typecheck:  ## strict type check
	@uv run --quiet mypy --no-error-summary --strict src tests && echo "✓ types clean (exit 0)"

test:  ## run the suite (warnings are errors; per-test output shown only on failure)
	@uv run --quiet python -W error -m unittest discover -s tests -t . --buffer

coverage:  ## run the suite under coverage + report (fails below the floor)
	@uv run --quiet coverage run -m unittest discover -s tests -t . --buffer
	@uv run --quiet coverage report

check: lint lint-battery typecheck test coverage  ## the full gate (lint -> lint-battery -> typecheck -> test -> coverage)

hooks:  ## install the pre-commit gate
	@uv run --quiet pre-commit install

clean:  ## remove caches + coverage artifacts
	@rm -rf .mypy_cache .ruff_cache .coverage htmlcov dist build
	@find . -type d -name __pycache__ -prune -exec rm -rf {} +
