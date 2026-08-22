from __future__ import annotations

from fastapi.testclient import TestClient
from obsalt.api.app import create_app
from obsalt.config import Settings
from obsalt.crypto.keys import hash_secret
from obsalt.domain.enums import AnalysisExecutionState
from obsalt.export.egress import EgressDenied, validate_destination
from obsalt.export.webhooks import new_signing_secret, sign, verify
from obsalt.otel.conventions import SPAN_TOOL, SPAN_TURN, tool_span_name, turn_span_name
from obsalt.plugin.protocol import ConnectionConfig, RawEnvelope
from obsalt.runtime import ApiPrincipal, Runtime
from obsalt.workers.decode import decode_envelope
from obsalt_example.plugin import ExamplePlugin
from obsalt_example.stream import ExampleStreamSource


def _runtime() -> tuple[Runtime, str]:
    runtime = Runtime.create(Settings(demo=True), extra_plugins=[ExamplePlugin()])
    creds = runtime.bootstrap_dev_org("acme")
    runtime.api_keys[hash_secret(creds["api_key"])] = ApiPrincipal(
        org_id="acme", scope="admin", kind="service", role="owner"
    )
    runtime.put_connection(
        ConnectionConfig(
            org_id="acme",
            provider="example",
            connection_id="ex",
            credentials={"shared_secret": "s"},
        ),
        creds["ingest_key"],
    )
    return runtime, creds["api_key"], creds["ingest_key"]


def test_api_requires_time_range_and_auth() -> None:
    runtime, key, _ingest = _runtime()
    client = TestClient(create_app(runtime))
    assert client.get("/v1/calls").status_code == 401
    assert client.get("/v1/calls", headers={"X-API-Key": key}).status_code == 400
    resp = client.get(
        "/v1/calls",
        headers={"X-API-Key": key},
        params={"from": "2026-01-01T00:00:00Z", "to": "2026-12-31T00:00:00Z"},
    )
    assert resp.status_code == 200
    assert resp.json()["items"] == []


def test_ingest_to_call_detail() -> None:
    runtime, key, ingest = _runtime()
    from importlib.resources import files

    raw = (files("obsalt_example") / "fixtures" / "raw" / "call_completed.json").read_bytes()
    client = TestClient(create_app(runtime))
    posted = client.post(
        f"/v1/ingest/example/{ingest}",
        content=raw,
        headers={"x-example-secret": "s", "content-type": "application/json"},
    )
    assert posted.status_code == 200
    listed = client.get(
        "/v1/calls",
        headers={"X-API-Key": key},
        params={"from": "2020-01-01T00:00:00Z", "to": "2030-01-01T00:00:00Z"},
    )
    assert listed.json()["items"]
    call_id = listed.json()["items"][0]["call_id"]
    detail = client.get(f"/v1/calls/{call_id}", headers={"X-API-Key": key})
    body = detail.json()
    assert body["coverage"]
    timeline = client.get(f"/v1/calls/{call_id}/timeline", headers={"X-API-Key": key})
    assert "reason" in timeline.json()
    ui = client.get(f"/v1/ui/calls/{call_id}")
    assert ui.status_code == 200
    assert "Provenance" in ui.text


def test_span_names_are_low_cardinality() -> None:
    assert turn_span_name(7) == SPAN_TURN == "turn"
    assert tool_span_name("create_booking") == SPAN_TOOL == "execute_tool"


def test_standard_webhooks_roundtrip() -> None:
    secret = new_signing_secret()
    payload = b'{"type":"call.finalized","revision":1}'
    headers = sign(payload, secret=secret, msg_id="msg_1")
    assert verify(payload, headers, secret)
    assert not verify(payload + b"x", headers, secret)


def test_egress_blocks_loopback() -> None:
    try:
        validate_destination("http://127.0.0.1/hook")
        raise AssertionError("expected deny")
    except EgressDenied:
        pass
    try:
        validate_destination("https://169.254.169.254/latest")
        raise AssertionError("expected deny")
    except EgressDenied:
        pass


def test_stream_source_example() -> None:
    import asyncio

    async def run() -> int:
        n = 0
        async for _frame in ExampleStreamSource().frames(
            ConnectionConfig(org_id="o", provider="example", connection_id="c")
        ):
            n += 1
        return n

    assert asyncio.run(run()) == 1


def test_tier2_defaults_to_sampled_out() -> None:
    from obsalt.analysis.tier2 import should_run_tier2

    assert should_run_tier2() is AnalysisExecutionState.SAMPLED_OUT
    assert should_run_tier2(manual=True) is AnalysisExecutionState.PENDING
    assert should_run_tier2(budget_remaining=False) is AnalysisExecutionState.BUDGET_BLOCKED


def test_decode_worker_redacts() -> None:
    from obsalt_example.plugin import ExamplePlugin

    plugin = ExamplePlugin()
    raw = b'{"event":"call.completed","call_id":"c1","system_prompt":"x","turns":[{"index":0,"speaker":"user","text":"reach me at ada@example.com","started_at":"2026-08-22T12:00:00Z","ended_at":"2026-08-22T12:00:01Z"}],"outcome":{"code":"completed"}}'
    envelope = RawEnvelope(
        envelope_id="e",
        org_id="o",
        provider="example",
        connection_id="c",
        object_key="k",
        body=raw,
        delivery_key="e",
        received_at="2026-08-22T00:00:00+00:00",
    )
    revision = decode_envelope(envelope, plugin=plugin, declaration=plugin.fidelity)
    assert revision.turns
    assert "<email>" in (revision.turns[0].text or "")
