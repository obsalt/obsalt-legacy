from __future__ import annotations

from fastapi.testclient import TestClient

from obsalt.api import create_app
from obsalt.client import ObsaltClient
from obsalt.config import Settings
from obsalt.pipeline import IngestPipeline
from obsalt.store import MemoryStore
from tests.conftest import load_fixture


def _client() -> ObsaltClient:
    store = MemoryStore()
    settings = Settings(api_keys="acme:test-key", require_auth=True)
    app = create_app(store=store, pipeline=IngestPipeline(store=store), settings=settings)
    http = TestClient(app)
    return ObsaltClient(http_client=http, api_key="test-key")


def test_client_ingest_and_lookup() -> None:
    client = _client()
    try:
        health = client.health()
        assert health["status"] == "ok"
        assert health["service"] == "obsalt"
        ingested = client.ingest("vapi", load_fixture("vapi_end_of_call.json"))
        assert ingested["finalized"] is True
        listed = client.list_calls()
        assert listed["count"] == 1
        detail = client.get_call(ingested["call_id"])
        assert detail["provider"] == "vapi"
        hits = client.search("refund")
        assert hits["hits"]
    finally:
        client.close()
