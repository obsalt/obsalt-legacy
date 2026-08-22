PYTHON ?= python3
PYTEST ?= pytest
RUFF ?= ruff
MYPY ?= mypy

.DEFAULT_GOAL := help

.PHONY: help install lint format typecheck test test-unit test-integration ci doctor plugins serve worker demo up

help: ## Show available targets
	@awk 'BEGIN {FS = ":.*##"; printf "\nTargets\n"} /^[a-zA-Z_-]+:.*?##/ { printf "  %-18s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

install: ## Editable install of core, testkit, and first-party plugins
	$(PYTHON) -m pip install -U pip
	$(PYTHON) -m pip install -r requirements-dev.txt

lint: ## Ruff check + format check
	$(RUFF) check packages tests
	$(RUFF) format --check packages tests

format: ## Apply Ruff fixes
	$(RUFF) format packages tests
	$(RUFF) check --fix packages tests

typecheck: ## Strict mypy (informational in CI until the backlog is cleared)
	$(MYPY)

test: ## Full pytest suite
	$(PYTEST)

test-unit: ## Fast path — no Docker
	$(PYTEST) -m unit

test-integration: ## Cross-component tests on memory doubles
	$(PYTEST) -m integration

ci: lint typecheck test ## Same gates as GitHub Actions (typecheck is informational)

up: ## Start Postgres, ClickHouse, Redis, MinIO
	docker compose up -d

doctor: ## Probe plugins and the durable stack
	obsalt doctor

plugins: ## List installed source plugins
	obsalt plugins

serve: ## HTTP API + console (needs `make up`)
	obsalt serve

worker: ## Drain the outbox (needs `make up`)
	obsalt worker

demo: ## Ephemeral stack + serve. Not for production
	obsalt demo
