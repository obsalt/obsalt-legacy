"""Pytest fixtures for the layered v2 suite."""

from __future__ import annotations

import pytest
from obsalt.domain.models import FidelityDeclaration

from tests.helpers import fidelity_declaration


@pytest.fixture
def decl() -> FidelityDeclaration:
    return fidelity_declaration()
