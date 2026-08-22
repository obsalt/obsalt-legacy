"""Shared fixtures for unit, integration, and black-box tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from obsalt.api import create_test_app
from obsalt.config import Settings
from obsalt.domain.models import FidelityDeclaration
from obsalt.runtime import AppState
from obsalt_example.plugin import ExamplePlugin
from tests.helpers import example_raw as load_example_raw
from tests.helpers import example_state, fidelity_declaration

_PATH_MARKERS = (
    ("/unit/", "unit"),
    ("/integration/", "integration"),
    ("/blackbox/", "blackbox"),
    ("/contract/", "contract"),
    ("/conformance/", "conformance"),
    ("/property/", "property"),
    ("/security/", "security"),
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark tests from their directory so `pytest -m unit` is a real fast path."""

    for item in items:
        path = str(item.path).replace("\\", "/")
        for fragment, name in _PATH_MARKERS:
            if fragment in path:
                item.add_marker(getattr(pytest.mark, name))
                break


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
    return TestClient(create_test_app(Settings(environment="test"), memory_state))


@pytest.fixture
def test_app() -> TestClient:
    return TestClient(create_test_app())
