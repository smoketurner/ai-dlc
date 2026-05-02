# Common workspace commands. Run `make help` for a summary.
#
# Conventions:
# - All Python work goes through `uv run` so the workspace virtualenv is used.
# - All third-party tools are pinned in `pyproject.toml` dev deps; this file
#   never installs anything ad-hoc.
# - Commands are idempotent. `make all` runs the full local validation chain.

SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

UV     ?= uv
PYTEST ?= $(UV) run pytest -q
RUFF   ?= $(UV) run ruff
TY     ?= $(UV) run ty
PREK   ?= prek

# `make TARGETS=packages/common lint` to scope a command to a sub-tree.
TARGETS ?= .

.PHONY: help sync sync-frozen lock lint lint-fix format format-check type test test-integration test-live test-eval audit hooks hooks-install clean all ci

help:                          ## Show this help.
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

sync:                          ## Resolve and install all workspace packages.
	$(UV) sync --all-packages

sync-frozen:                   ## Install from the locked dependency set (CI mode).
	$(UV) sync --all-packages --frozen

lock:                          ## Recompute uv.lock from pyproject.tomls.
	$(UV) lock

lint:                          ## Ruff lint (warnings = errors).
	$(RUFF) check $(TARGETS)

lint-fix:                      ## Ruff lint with --fix.
	$(RUFF) check --fix $(TARGETS)

format:                        ## Ruff format (writes changes).
	$(RUFF) format $(TARGETS)

format-check:                  ## Ruff format --check (no writes).
	$(RUFF) format --check $(TARGETS)

type:                          ## ty type-check.
	$(TY) check $(TARGETS)

test:                          ## Run unit tests.
	$(PYTEST) -m "not integration and not live_aws and not eval"

test-integration:              ## Run moto-backed integration tests.
	$(PYTEST) -m integration

test-live:                     ## Run live-AWS tests against the dev account.
	$(PYTEST) -m live_aws

test-eval:                     ## Run agent eval cases (live AWS, gated).
	$(PYTEST) -m eval

audit:                         ## pip-audit against the lockfile.
	$(UV) run pip-audit --strict --disable-pip -r <($(UV) export --frozen --format requirements-txt)

hooks-install:                 ## Install git hooks via prek.
	$(PREK) install

hooks:                         ## Run prek (= pre-commit) on all files.
	$(PREK) run --all-files

clean:                         ## Remove caches and build artifacts.
	rm -rf .ruff_cache .ty_cache .pytest_cache .mypy_cache htmlcov .coverage*
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name '*.egg-info' -prune -exec rm -rf {} +

all: lint format-check type test  ## The full local validation chain.

ci: sync-frozen lint format-check type test audit  ## What CI runs on every PR.
