UV ?= uv

.DEFAULT_GOAL := help

.PHONY: help install lock lint format typecheck test test-unit test-integration ci doctor plugins serve worker up stores seed

help: ## Show available targets
	@awk 'BEGIN {FS = ":.*##"; printf "\nTargets\n"} /^[a-zA-Z_-]+:.*?##/ { printf "  %-18s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

install: ## Editable install of core, testkit, and first-party plugins via uv
	$(UV) sync --all-packages

lock: ## Regenerate the uv.lock lockfile
	$(UV) lock

lint: ## Ruff check + format check
	$(UV) run ruff check packages tests
	$(UV) run ruff format --check packages tests

format: ## Apply Ruff fixes
	$(UV) run ruff format packages tests
	$(UV) run ruff check --fix packages tests

typecheck: ## Strict mypy (informational in CI until the backlog is cleared)
	$(UV) run mypy

test: ## Full pytest suite
	$(UV) run pytest

test-unit: ## Fast path — no Docker
	$(UV) run pytest -m unit

test-integration: ## Cross-component tests on memory doubles
	$(UV) run pytest -m integration

ci: lint typecheck test ## Same gates as GitHub Actions (typecheck is informational)

up: ## Full stack — stores + serve + worker
	docker compose up -d

stores: ## Stores only (run obsalt serve / worker on the host)
	docker compose up -d postgres clickhouse redis minio

doctor: ## Probe plugins and the durable stack
	$(UV) run obsalt doctor

plugins: ## List installed source plugins
	$(UV) run obsalt plugins

serve: ## HTTP API + console (needs `make up`)
	$(UV) run obsalt serve

worker: ## Drain the outbox (needs `make up`)
	$(UV) run obsalt worker

seed: ## Replay vendored provider fixtures into the local stack via HTTP
	$(UV) run obsalt seed
