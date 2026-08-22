"""T10 / §6.1 / §12.5: fail-closed auth, tenant isolation, SSRF."""

from __future__ import annotations

import json

import httpx
import pytest
from obsalt.domain.enums import VerifyOutcome
from obsalt.egress import EgressDenied, validate_destination
from obsalt.ingest.headers import RawHeaders
from obsalt.ingest.receive import receive_webhook
from obsalt.plugin.types import BackfillCursor, ConnectionConfig
from obsalt_retell.plugin import RetellPlugin
from obsalt_vapi.plugin import VapiPlugin
from tests.conftest import (
    RETELL_FIXTURES,
    VAPI_FIXTURES,
    retell_headers,
    vapi_headers,
    vapi_state,
)


def test_vapi_fails_closed_without_secret() -> None:
    plugin = VapiPlugin()
    raw = (VAPI_FIXTURES / "raw" / "end_of_call.json").read_bytes()
    cfg = ConnectionConfig(
        org_id="acme",
        provider="vapi",
        connection_id="c1",
        ingest_key_hash="x",
        secrets={},
        settings={"auth_mode": "legacy_secret"},
    )
    result = plugin.authenticate(raw, RawHeaders.from_mapping(vapi_headers()).as_list(), cfg)
    assert result.outcome is VerifyOutcome.MISSING_CREDENTIAL
    assert result.ok is False


def test_retell_fails_closed_without_api_key() -> None:
    plugin = RetellPlugin()
    raw = (RETELL_FIXTURES / "raw" / "call_ended.json").read_bytes()
    cfg = ConnectionConfig(
        org_id="acme",
        provider="retell",
        connection_id="c1",
        ingest_key_hash="x",
        secrets={},
    )
    result = plugin.authenticate(raw, RawHeaders.from_mapping(retell_headers(raw)).as_list(), cfg)
    assert result.outcome is VerifyOutcome.MISSING_CREDENTIAL
    assert result.ok is False


def test_blank_secret_is_not_fail_open() -> None:
    plugin = VapiPlugin()
    raw = (VAPI_FIXTURES / "raw" / "end_of_call.json").read_bytes()
    cfg = ConnectionConfig(
        org_id="acme",
        provider="vapi",
        connection_id="c1",
        ingest_key_hash="x",
        secrets={"legacy_secret": ""},
        settings={"auth_mode": "legacy_secret"},
    )
    result = plugin.authenticate(raw, [(b"x-vapi-secret", b"")], cfg)
    assert result.ok is False


def test_bad_signature_is_rejected_on_the_receive_path() -> None:
    state = vapi_state()
    raw = (VAPI_FIXTURES / "raw" / "end_of_call.json").read_bytes()
    result = receive_webhook(
        provider="vapi",
        ingest_key="ik",
        raw=raw,
        headers=RawHeaders.from_mapping({"x-vapi-secret": "wrong", "content-type": "application/json"}),
        resolver=state.resolver,
        plugin=VapiPlugin(),
        objects=state.objects,
        inbox=state.inbox,
    )
    assert result.created is False
    assert result.rejected in {"bad_signature", VerifyOutcome.BAD_SIGNATURE.value}
    assert result.response.status_code == 401


def test_unknown_ingest_key_is_404() -> None:
    state = vapi_state()
    raw = (VAPI_FIXTURES / "raw" / "end_of_call.json").read_bytes()
    result = receive_webhook(
        provider="vapi",
        ingest_key="nope",
        raw=raw,
        headers=RawHeaders.from_mapping(vapi_headers()),
        resolver=state.resolver,
        plugin=VapiPlugin(),
        objects=state.objects,
        inbox=state.inbox,
    )
    assert result.response.status_code == 404


def test_ssrf_blocks_loopback_link_local_and_plain_http() -> None:
    with pytest.raises(EgressDenied):
        validate_destination("http://127.0.0.1/v1/traces")
    with pytest.raises(EgressDenied):
        validate_destination("http://169.254.169.254/latest/meta-data/")
    with pytest.raises(EgressDenied):
        validate_destination("https://127.0.0.1/internal")
    with pytest.raises(EgressDenied):
        validate_destination("ftp://example.com/x")


def test_vapi_backfill_does_not_follow_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> list:
            return []

    def _get(url: str, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return _Response()

    monkeypatch.setattr(httpx, "get", _get)
    monkeypatch.setattr("obsalt.egress.validate_destination", lambda url, **_kwargs: None)
    page = VapiPlugin().scan(
        ConnectionConfig(
            org_id="acme",
            provider="vapi",
            connection_id="c1",
            ingest_key_hash="x",
            secrets={"api_key": "vk"},
        ),
        BackfillCursor(),
    )
    assert captured.get("follow_redirects") is False
    assert page.items == []


def test_mixed_org_otlp_resource_is_rejected() -> None:
    from obsalt.otel.tenancy import reject_tenant_assertions
    from obsalt.plugin.types import ReadableSpan
    from tests.conftest import api_client, example_state

    spans = [
        ReadableSpan(
            name="turn",
            trace_id="aa" * 16,
            span_id="bb" * 8,
            start_unix_nano=1,
            end_unix_nano=2,
            attributes={"obsalt.org": "other"},
        )
    ]
    assert reject_tenant_assertions(spans, "acme") is not None
    client = api_client(example_state())
    res = client.post(
        "/v1/traces",
        content=json.dumps(
            {
                "resourceSpans": [
                    {
                        "resource": {"attributes": [{"key": "obsalt.org", "value": {"stringValue": "other"}}]},
                        "scopeSpans": [
                            {
                                "spans": [
                                    {
                                        "traceId": "aa" * 16,
                                        "spanId": "bb" * 8,
                                        "name": "turn",
                                        "startTimeUnixNano": "1",
                                        "endTimeUnixNano": "2",
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }
        ),
        headers={"X-API-Key": "k", "content-type": "application/json"},
    )
    assert res.status_code == 401
