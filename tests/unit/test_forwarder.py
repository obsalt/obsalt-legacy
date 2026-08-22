from __future__ import annotations

import pytest

from obsalt.egress import EgressDenied, validate_destination
from obsalt.otel.forwarder import ForwardOutcome, forward_otlp_batch


def test_ssrf_blocks_loopback_http() -> None:
    with pytest.raises(EgressDenied):
        validate_destination("http://127.0.0.1/v1/traces")
    with pytest.raises(EgressDenied):
        validate_destination("http://169.254.169.254/latest/meta-data/")
    result = forward_otlp_batch(
        "http://127.0.0.1/v1/traces",
        b"{}",
        content_type="application/json",
        allow_http_localhost=False,
    )
    assert result.outcome is ForwardOutcome.PERMANENT
    assert result.retryable is False


def test_otlp_json_forward_does_not_rewrite_bytes() -> None:
    payload = b'{"resourceSpans":[{"pii":"leave-me"}]}'
    result = forward_otlp_batch(
        "http://127.0.0.1:1/v1/traces",
        payload,
        content_type="application/json",
        emit_pii=False,
        allow_http_localhost=True,
        timeout=0.05,
    )
    assert result.identity_preserved is True


def test_localhost_http_allowed_only_when_opted_in() -> None:
    validate_destination("http://127.0.0.1:9/v1/traces", allow_http_localhost=True)
    result = forward_otlp_batch(
        "http://127.0.0.1:1/v1/traces",
        b"raw-bytes-must-not-be-rewritten",
        content_type="application/x-protobuf",
        allow_http_localhost=True,
        timeout=0.05,
    )
    assert result.identity_preserved is True
    assert result.outcome in {ForwardOutcome.RETRYABLE, ForwardOutcome.PERMANENT}
