"""Shared fixtures for unit, integration, and black-box tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from obsalt.api import create_app, create_test_app
from obsalt.config import Settings
from obsalt.domain.models import FidelityDeclaration
from obsalt.runtime import AppState
from obsalt_example.plugin import ExamplePlugin

from tests.helpers import (
    EXAMPLE_FIXTURES,
    example_state,
    fidelity_declaration,
    signed_example_headers,
)
from tests.helpers import example_raw as load_example_raw

__all__ = ["EXAMPLE_FIXTURES", "signed_example_headers"]


@pytest.fixture
def decl() -> FidelityDeclaration:
    return fidelity_declaration()


@pytest.fixture
def example_plugin() -> ExamplePlugin:
    return ExamplePlugin()


@pytest.fixture
def example_raw() -> bytes:
    return load_example_raw()


@pytest.fixture
def memory_state() -> AppState:
    return example_state(org_spend={"acme": 0.0, "other": 0.0})


@pytest.fixture
def client(memory_state: AppState) -> TestClient:
    return TestClient(create_app(Settings(environment="test"), memory_state))


@pytest.fixture
def test_app() -> TestClient:
    return TestClient(create_test_app())
