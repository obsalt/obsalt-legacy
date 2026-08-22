PYTHON ?= python3
PYTEST ?= pytest
RUFF ?= ruff
MYPY ?= mypy

.PHONY: install lint format typecheck test test-unit ci doctor

install:
	$(PYTHON) -m pip install -U pip
	$(PYTHON) -m pip install -r requirements-dev.txt

lint:
	$(RUFF) check packages tests
	$(RUFF) format --check packages tests

format:
	$(RUFF) format packages tests
	$(RUFF) check --fix packages tests

typecheck:
	$(MYPY)

test:
	$(PYTEST)

test-unit:
	$(PYTEST) -m unit

ci: lint typecheck test

doctor:
	obsalt doctor
