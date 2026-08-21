from __future__ import annotations

from fastapi.testclient import TestClient

from obsalt.api import create_app
from obsalt.config import Settings
from obsalt.pipeline import IngestPipeline
from obsalt.security import verify_retell, verify_vapi
from obsalt.store import MemoryStore
from tests.conftest import load_fixture


def test_webhook_signatures() -> None:
    assert verify_vapi({"x-vapi-secret": "s"}, "s")
    assert not verify_vapi({"x-vapi-secret": "nope"}, "s")
    body = b'{"event":"call_ended"}'
    assert verify_retell({"x-retell-signature": "deadbeef"}, body, "secret") is False
    import hashlib
    import hmac

    sig = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert verify_retell({"x-retell-signature": sig}, body, "secret")


def _client() -> TestClient:
    store = MemoryStore()
    settings = Settings(api_keys="acme:test-key", require_auth=True)
    app = create_app(store=store, pipeline=IngestPipeline(store=store), settings=settings)
    return TestClient(app)


def test_health() -> None:
    response = TestClient(create_app()).get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "obsalt"
    assert "version" in body
    assert body["store"] == "memory"


def test_ingest_to_search_latency_hangup_tools() -> None:
    client = _client()
    headers = {"X-API-Key": "test-key"}
    vapi = client.post("/v1/ingest/vapi", headers=headers, json=load_fixture("vapi_end_of_call.json"))
    assert vapi.status_code == 200
    assert vapi.json()["finalized"] is True
    retell = client.post("/v1/ingest/retell", headers=headers, json=load_fixture("retell_call_ended.json"))
    assert retell.status_code == 200

    unauthorized = client.post("/v1/ingest/vapi", json=load_fixture("vapi_end_of_call.json"))
    assert unauthorized.status_code == 401

    calls = client.get("/v1/calls", headers=headers).json()
    assert calls["count"] == 2

    search = client.post("/v1/search", headers=headers, json={"query": "customers asking about refunds"})
    assert search.status_code == 200
    assert search.json()["hits"][0]["call_id"] == vapi.json()["call_id"]

    latency = client.get("/v1/latency", headers=headers).json()["components"]
    names = {c["component"] for c in latency}
    assert {"stt", "llm", "tts"}.issubset(names)
    stt = next(c for c in latency if c["component"] == "stt")
    assert stt["p50_ms"] is not None
    assert stt["p95_ms"] is not None

    hangups = client.get("/v1/hangups", headers=headers).json()["clusters"]
    assert hangups
    user_cluster = next(c for c in hangups if c["reason"] == "user_hangup")
    assert user_cluster["lost_customer_call_id"] == vapi.json()["call_id"]

    tools = client.get("/v1/tools", headers=headers).json()["tools"]
    lookup = next(t for t in tools if t["name"] == "lookup_order")
    assert lookup["invocations"] >= 1
    assert lookup["success_rate"] == 0

    detail = client.get(f"/v1/calls/{vapi.json()['call_id']}", headers=headers).json()
    assert detail["hallucinations"]
    assert detail["evals"]

    created = client.post(
        "/v1/evals/rubrics",
        headers=headers,
        json={"name": "No invented IDs", "description": "The agent never invents order numbers.", "threshold": 0.9},
    )
    assert created.status_code == 200
    rerun = client.post(f"/v1/evals/run/{vapi.json()['call_id']}", headers=headers)
    assert rerun.status_code == 200
    assert any(e["rubric_name"] == "No invented IDs" for e in rerun.json()["evals"])


def test_openai_realtime_ingest_endpoint() -> None:
    client = _client()
    response = client.post(
        "/v1/ingest/openai-realtime",
        headers={"X-API-Key": "test-key"},
        json=load_fixture("openai_realtime_session.json"),
    )
    assert response.status_code == 200
    call = client.get(f"/v1/calls/{response.json()['call_id']}", headers={"X-API-Key": "test-key"}).json()
    assert call["provider"] == "openai_realtime"
    assert any(s["component"] == "stt" for s in call["latency_samples"])
