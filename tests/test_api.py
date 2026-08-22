from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from obsalt.api import AppState, create_app
from obsalt.assemble.promote import MemoryPointerStore
from obsalt.config import Settings
from obsalt.crypto.primitives import hmac_hex
from obsalt.domain.enums import KeyScope
from obsalt.plugin.types import ConnectionConfig
from obsalt.testing.fakes import MemoryInbox, MemoryObjectStore, MemoryResolver
from obsalt.worker.process import MemoryRevisionSink
from obsalt_example.plugin import ExamplePlugin

FIXTURES = Path(__file__).resolve().parents[1] / "packages" / "obsalt-example" / "src" / "obsalt_example" / "fixtures"


def _client() -> TestClient:
    plugin = ExamplePlugin()
    resolver = MemoryResolver()
    cfg = ConnectionConfig(
        org_id="acme",
        provider="example",
        connection_id="c1",
        ingest_key_hash="",
        secrets={"hmac_secret": "s"},
    )
    resolver.add(cfg, "ik")
    state = AppState(
        settings=Settings(),
        plugins=__import__("obsalt.plugin.host", fromlist=["LoadedPlugin"]).LoadedPlugin(plugin),  # type: ignore[arg-type]
        resolver=resolver,
        objects=MemoryObjectStore(),
        inbox=MemoryInbox(),
        pointers=MemoryPointerStore(),
        sink=MemoryRevisionSink(),
        keys={"k": ("acme", frozenset(KeyScope))},
    )
    # LoadedPlugin expects ObsaltPlugin; wrap properly
    from obsalt.plugin.host import LoadedPlugin

    state.plugins = [LoadedPlugin(plugin)]
    return TestClient(create_app(Settings(), state))


def test_health_and_plugins() -> None:
    client = _client()
    assert client.get("/health").json()["status"] == "ok"
    names = [p["name"] for p in client.get("/v1/plugins").json()["items"]]
    assert "example" in names


def test_list_calls_requires_time_range() -> None:
    client = _client()
    res = client.get("/v1/calls", headers={"X-API-Key": "k"})
    assert res.status_code == 400


def test_unknown_api_key_is_401_not_first_org() -> None:
    client = _client()
    res = client.get("/v1/calls?start=2026-01-01T00:00:00Z&end=2026-12-31T00:00:00Z", headers={"X-API-Key": "nope"})
    assert res.status_code == 401


def test_ingest_example_and_ui() -> None:
    client = _client()
    raw = (FIXTURES / "raw" / "call_ended.json").read_bytes()
    sig = hmac_hex("s", raw)
    res = client.post(
        "/v1/ingest/example/ik",
        content=raw,
        headers={"x-obsalt-example-signature": sig, "content-type": "application/json"},
    )
    assert res.status_code == 200
    listed = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z",
        headers={"X-API-Key": "k"},
    )
    assert listed.status_code == 200
    ui = client.get("/v1/ui")
    assert ui.status_code == 200
    assert b"obsalt" in ui.content
