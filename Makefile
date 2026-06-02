# vmlease — canonical project commands (all run through `uv`).
#
# NOTE: `ruff format` is intentionally NOT used anywhere — it is banned in
# pyproject.toml (it has a code-mangling bug). `make format` applies ruff's safe
# lint autofixes instead. Use these targets; don't invoke the tools ad hoc.

.DEFAULT_GOAL := help
.PHONY: help install lint format typecheck test coverage check hooks clean

help:  ## show this help
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install:  ## sync the env + dev tools
	uv sync

lint:  ## strict lint, no autofix
	uv run ruff check src tests

format:  ## apply ruff's SAFE autofixes (NOT `ruff format`, which is banned)
	uv run ruff check --fix src tests

typecheck:  ## strict type check
	uv run mypy --strict src tests

test:  ## run the suite (warnings are errors)
	uv run python -W error -m unittest discover -s tests -t .

coverage:  ## run the suite under coverage + report (fails below the floor)
	uv run coverage run -m unittest discover -s tests -t .
	uv run coverage report

check: lint typecheck test coverage  ## the full gate (lint -> typecheck -> test -> coverage)

hooks:  ## install the pre-commit gate
	uv run pre-commit install

clean:  ## remove caches + coverage artifacts
	rm -rf .mypy_cache .ruff_cache .coverage htmlcov dist build
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
