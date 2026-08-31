"""Seed talks to the public HTTP surface only: signed webhooks and OTLP."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from obsalt.api import create_test_app
from obsalt.config import Settings
from obsalt.ops.seed import SEED_SECRET, connection_body, run_seed, signed_headers
from obsalt.runtime import in_memory_state
from tests.helpers import auth


def _seed_client() -> TestClient:
    settings = Settings(environment="test", trace_grace_seconds=0)
    state = in_memory_state(settings, org_id="local", api_key="dev-key")
    return TestClient(create_test_app(settings, state))


def test_seed_posts_webhooks_and_otlp_and_lists_calls() -> None:
    client = _seed_client()
    now = datetime(2026, 8, 29, 18, 0, tzinfo=UTC)
    report = run_seed(
        client=client,
        api_key="dev-key",
        count=1,
        window_days=7,
        poll_timeout=5.0,
        now=now,
        base_url="http://testserver",
    )
    assert report.posted >= 8
    assert report.calls >= 8
    assert set(report.sources) >= {
        "vapi",
        "retell",
        "elevenlabs",
        "cartesia",
        "pipecat",
        "livekit",
        "openai_realtime",
        "gemini_live",
    }
    listed = client.get(
        "/v1/calls",
        params={"start": report.start, "end": report.end, "limit": 100},
        headers=auth("dev-key"),
    )
    assert listed.status_code == 200
    items = listed.json()["items"]
    sources = {item["source"] for item in items}
    assert {"vapi", "retell", "elevenlabs", "cartesia", "pipecat", "livekit"} <= sources
    assert "openai_realtime" in sources or "gemini_live" in sources
    vapi_id = next(item["id"] for item in items if item["source"] == "vapi")
    timeline = client.get(f"/v1/calls/{vapi_id}/timeline", headers=auth("dev-key"))
    assert timeline.status_code == 200
    body = timeline.json()
    assert body["draw_stage_waterfall"] is False
    pipecat_id = next(item["id"] for item in items if item["source"] == "pipecat")
    pipe = client.get(f"/v1/calls/{pipecat_id}/timeline", headers=auth("dev-key"))
    assert pipe.status_code == 200
    assert pipe.json()["draw_stage_waterfall"] is True


def test_seed_surfaces_a_bad_vapi_secret() -> None:
    client = _seed_client()
    created = client.post(
        "/v1/connections",
        headers={**auth("dev-key"), "content-type": "application/json"},
        json=connection_body("vapi"),
    )
    assert created.status_code == 200
    ingest_key = created.json()["ingest_key"]
    raw = b'{"message":{"type":"end-of-call-report","call":{"id":"x"}}}'
    headers = signed_headers("vapi", raw, secret="wrong-secret")
    posted = client.post(f"/v1/ingest/vapi/{ingest_key}", content=raw, headers=headers)
    assert posted.status_code != 200


def test_connections_list_includes_settings_not_secrets() -> None:
    client = _seed_client()
    created = client.post(
        "/v1/connections",
        headers={**auth("dev-key"), "content-type": "application/json"},
        json=connection_body("vapi"),
    )
    assert created.status_code == 200
    listed = client.get("/v1/connections", headers=auth("dev-key"))
    assert listed.status_code == 200
    seed_rows = [
        item
        for item in listed.json()["items"]
        if item.get("provider") == "vapi" and (item.get("settings") or {}).get("seed")
    ]
    assert seed_rows
    assert SEED_SECRET not in listed.text
    assert "ingest_key" not in seed_rows[0]
    assert seed_rows[0]["settings"].get("auth_mode") == "legacy_secret"
