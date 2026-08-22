"""Shared egress policy: private ranges, metadata, and redirect revalidation."""

from __future__ import annotations

import pytest

from obsalt.egress import EgressDenied, validate_destination, validate_redirect
from obsalt.otel.forwarder import ForwardOutcome, forward_otlp_batch
from obsalt.webhooks.outbound import deliver, mint_whsec, parse_whsec


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/v1/traces",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.8/internal",
        "http://192.168.1.1/admin",
        "http://172.16.0.1/x",
        "https://169.254.169.254/latest/meta-data/",
    ],
)
def test_ssrf_blocks_private_and_metadata(url: str) -> None:
    with pytest.raises(EgressDenied):
        validate_destination(url)


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


def test_redirect_to_metadata_is_blocked() -> None:
    with pytest.raises(EgressDenied):
        validate_redirect("https://1.1.1.1/hooks", "http://169.254.169.254/latest/meta-data/")
    with pytest.raises(EgressDenied):
        validate_redirect("https://1.1.1.1/hooks", "http://127.0.0.1/steal")


def test_relative_redirect_joins_then_revalidates() -> None:
    resolved = validate_redirect("https://1.1.1.1/hooks", "https://1.1.1.1/next")
    assert resolved == "https://1.1.1.1/next"


def test_forward_loopback_without_opt_in_is_permanent() -> None:
    result = forward_otlp_batch(
        "http://127.0.0.1/v1/traces",
        b"{}",
        content_type="application/json",
        allow_http_localhost=False,
    )
    assert result.outcome is ForwardOutcome.PERMANENT
    assert result.retryable is False


def test_outbound_webhook_https_only() -> None:
    secret = parse_whsec(mint_whsec())
    ok, detail = deliver("http://203.0.113.1/hooks", secret, b"{}", allow_http_localhost=False)
    assert ok is False
    assert "permanent" in detail


def test_nested_redirect_is_refused(monkeypatch) -> None:
    secret = parse_whsec(mint_whsec())

    class _Resp:
        def __init__(self, status_code: int, location: str = "") -> None:
            self.status_code = status_code
            self.headers = {"location": location}

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            self.posts = 0

        def post(self, url, **kwargs):
            self.posts += 1
            if self.posts == 1:
                return _Resp(302, "https://1.1.1.1/next")
            return _Resp(302, "https://1.1.1.1/again")

        def close(self) -> None:
            return None

    ok, detail = deliver(
        "https://1.1.1.1/hooks",
        secret,
        b"{}",
        allow_http_localhost=False,
        client=_Client(),
    )
    assert ok is False
    assert "nested redirect" in detail
