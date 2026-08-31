"""Seed clones stay on the vendored schema; signers match plugin authenticate."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

import httpx
import pytest

from obsalt.cli import build_parser, cmd_seed
from obsalt.config import Settings
from obsalt.ingest.headers import RawHeaders
from obsalt.ops.seed import (
    RETELL_HANGUPS,
    SEED_SECRET,
    VAPI_HANGUPS,
    SeedError,
    build_corpus,
    environment_allowed,
    run_seed,
    selected_providers,
    signed_headers,
    wrap_otlp,
)
from obsalt.otel.receiver import parse_otlp_request, request_to_spans
from obsalt.plugin.types import ConnectionConfig
from obsalt_cartesia.plugin import CartesiaPlugin
from obsalt_elevenlabs.plugin import ElevenLabsPlugin
from obsalt_retell.plugin import RetellPlugin
from obsalt_vapi.plugin import VapiPlugin


def test_environment_allowed_rejects_production() -> None:
    assert environment_allowed(Settings(environment="dev")) is True
    assert environment_allowed(Settings(environment="test")) is True
    assert environment_allowed(Settings(environment="production")) is False


def test_unknown_provider_is_rejected() -> None:
    with pytest.raises(SeedError, match="unknown provider"):
        selected_providers(["not-a-plugin"], include_example=False)


def test_dry_run_corpus_covers_first_party_and_validates() -> None:
    envelopes = build_corpus(
        [
            "vapi",
            "retell",
            "elevenlabs",
            "cartesia",
            "pipecat",
            "livekit",
            "openai_realtime",
            "gemini_live",
        ],
        count=2,
        window_days=7,
        now=datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
    )
    by_provider: dict[str, list] = {}
    for item in envelopes:
        by_provider.setdefault(item.provider, []).append(item)
    assert set(by_provider) == {
        "vapi",
        "retell",
        "elevenlabs",
        "cartesia",
        "pipecat",
        "livekit",
        "openai_realtime",
        "gemini_live",
    }
    for rows in by_provider.values():
        assert len(rows) == 2
        assert rows[0].source_call_id != rows[1].source_call_id
        assert rows[0].body != rows[1].body


def test_vapi_and_retell_hangups_stay_in_vendor_enums() -> None:
    envelopes = build_corpus(
        ["vapi", "retell"],
        count=5,
        window_days=7,
        now=datetime(2026, 8, 29, tzinfo=UTC),
    )
    vapi_reasons = {
        json.loads(item.body)["message"]["endedReason"]
        for item in envelopes
        if item.provider == "vapi"
    }
    retell_reasons = {
        json.loads(item.body)["call"]["disconnection_reason"]
        for item in envelopes
        if item.provider == "retell"
    }
    assert vapi_reasons <= set(VAPI_HANGUPS)
    assert retell_reasons <= set(RETELL_HANGUPS)


def test_signed_headers_authenticate() -> None:
    raw = b'{"ok":true}'
    cases = [
        (
            "vapi",
            VapiPlugin(),
            {"legacy_secret": SEED_SECRET},
            {"auth_mode": "legacy_secret"},
        ),
        ("retell", RetellPlugin(), {"api_key": SEED_SECRET}, {}),
        ("elevenlabs", ElevenLabsPlugin(), {"webhook_secret": SEED_SECRET}, {}),
        ("cartesia", CartesiaPlugin(), {"webhook_secret": SEED_SECRET}, {}),
    ]
    for name, plugin, secrets, settings in cases:
        headers = signed_headers(name, raw)
        cfg = ConnectionConfig(
            org_id="local",
            provider=name,
            connection_id="c",
            ingest_key_hash="x",
            secrets=secrets,
            settings=settings,
        )
        result = plugin.authenticate(raw, RawHeaders.from_mapping(headers).as_list(), cfg)
        assert result.ok, f"{name}: {result.outcome} {result.detail}"


def test_otlp_wrap_is_parseable() -> None:
    envelopes = build_corpus(
        ["pipecat"],
        count=1,
        window_days=7,
        now=datetime(2026, 8, 29, tzinfo=UTC),
    )
    req = parse_otlp_request("application/json", envelopes[0].body, None)
    spans = request_to_spans(req)
    assert spans
    assert all(span.trace_id for span in spans)
    wrapped = wrap_otlp(
        [
            {
                "name": "turn",
                "trace_id": "aa" * 16,
                "span_id": "bb" * 8,
                "start_unix_nano": 1,
                "end_unix_nano": 2,
                "attributes": {"gen_ai.conversation.id": "c1"},
            }
        ]
    )
    assert wrapped["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["traceId"] == "aa" * 16


def test_run_seed_refuses_production(monkeypatch) -> None:
    monkeypatch.setattr("obsalt.ops.seed.environment_allowed", lambda _settings=None: False)
    with pytest.raises(SeedError, match="production"):
        run_seed(client=None, api_key="dev-key", dry_run=True)


def test_parser_exposes_seed() -> None:
    parser = build_parser()
    args = parser.parse_args(["seed", "--count", "1", "--dry-run", "--providers", "vapi"])
    assert args.command == "seed"
    assert args.count == 1
    assert args.dry_run is True
    assert args.providers == "vapi"


def test_seed_timeout_reports_ready_queue_not_just_start_worker() -> None:
    from obsalt.ops.seed import _wait_calls

    class _Resp:
        def __init__(self, payload: dict, status: int = 200, text: str = "") -> None:
            self.status_code = status
            self.text = text
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    class _Client:
        def get(self, path: str, **_kwargs: object) -> _Resp:
            if path == "/ready":
                return _Resp({"outbox_depth": 21, "dlq_depth": 86, "inbox_age_seconds": 0})
            return _Resp({"items": []})

    with pytest.raises(SeedError, match="dlq_depth=86") as caught:
        _wait_calls(_Client(), "dev-key", "s", "e", 8, timeout=0.01, sleep=lambda _s: None)
    assert "outbox did not drain" not in str(caught.value)


def test_seed_fails_fast_when_calls_list_is_500() -> None:
    from obsalt.ops.seed import _wait_calls

    class _Resp:
        def __init__(self, payload: dict, status: int = 200, text: str = "") -> None:
            self.status_code = status
            self.text = text
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    class _Client:
        def get(self, path: str, **_kwargs: object) -> _Resp:
            if path == "/ready":
                return _Resp({"outbox_depth": 0, "dlq_depth": 86, "inbox_age_seconds": 0})
            return _Resp({}, status=500, text="could not determine data type of parameter $6")

    with pytest.raises(SeedError, match="parameter \\$6"):
        _wait_calls(_Client(), "dev-key", "s", "e", 8, timeout=2.0, sleep=lambda _s: None)


def test_cmd_seed_exits_2_when_serve_is_down(monkeypatch, capsys) -> None:
    class _Boom:
        def __enter__(self) -> _Boom:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, *_args: object, **_kwargs: object) -> None:
            raise httpx.ConnectError("refused")

    monkeypatch.setattr("httpx.Client", lambda **_kwargs: _Boom())
    rc = cmd_seed(
        argparse.Namespace(
            base="http://localhost:8080",
            key="dev-key",
            count=1,
            providers=None,
            window_days=7,
            include_example=False,
            json=False,
            dry_run=False,
        )
    )
    assert rc == 2
    assert "Could not connect" in capsys.readouterr().err
