"""setup_tracing accepts a service origin or a complete traces URL."""

from __future__ import annotations

from obsalt.otel.conventions import traces_endpoint


def test_traces_endpoint_appends_when_given_origin() -> None:
    assert traces_endpoint("http://localhost:8080") == "http://localhost:8080/v1/traces"
    assert traces_endpoint("http://localhost:8080/") == "http://localhost:8080/v1/traces"


def test_traces_endpoint_does_not_double_append() -> None:
    assert traces_endpoint("http://localhost:8080/v1") == "http://localhost:8080/v1/traces"
    assert traces_endpoint("http://localhost:8080/v1/traces") == "http://localhost:8080/v1/traces"
    assert traces_endpoint("http://localhost:8080/v1/traces/") == "http://localhost:8080/v1/traces"
