"""Schema-validated fixtures are a CI gate (§13.1), including overlay reporting."""

from __future__ import annotations

import pytest
from tests.helpers import (
    CARTESIA_FIXTURES,
    ELEVEN_FIXTURES,
    EXAMPLE_FIXTURES,
    RETELL_FIXTURES,
    VAPI_FIXTURES,
)

from obsalt_testkit.schema import FixtureSuite, blocking_errors, validate_raw_fixtures


@pytest.mark.parametrize(
    "root",
    [EXAMPLE_FIXTURES, VAPI_FIXTURES, RETELL_FIXTURES, ELEVEN_FIXTURES, CARTESIA_FIXTURES],
)
def test_provider_raw_fixtures_pass_schema_harness(root) -> None:
    messages = validate_raw_fixtures(FixtureSuite(root))
    assert blocking_errors(messages) == []
