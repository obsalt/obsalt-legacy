"""Public HTTP contract — auth, range, tenancy, CSRF. No internals."""

from __future__ import annotations

from obsalt.assemble.promote import MemoryPointerStore
from obsalt.domain.enums import AnalysisState, KeyScope
from obsalt.domain.models import AnalysisExecution, AnalysisResult, CallRevision
from obsalt.plugin.host import LoadedPlugin
from obsalt.plugin.types import ConnectionConfig
from obsalt.runtime import AppState
from obsalt.testing.fakes import MemoryInbox, MemoryObjectStore, MemoryResolver
from obsalt.worker.process import MemoryRevisionSink
from obsalt_example.plugin import ExamplePlugin

from tests.helpers import EXAMPLE_FIXTURES, api_client, example_headers, example_state


def test_health_is_public() -> None:
    client = api_client(example_state())
    assert client.get("/health").json()["status"] == "ok"


def test_reads_require_an_api_key() -> None:
    client = api_client(example_state())
    listed = client.get("/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z")
    assert listed.status_code == 401


def test_unknown_api_key_is_401() -> None:
    client = api_client(example_state())
    listed = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z",
        headers={"X-API-Key": "nope"},
    )
    assert listed.status_code == 401


def test_list_and_quality_require_a_time_range() -> None:
    client = api_client(example_state())
    assert client.get("/v1/calls", headers={"X-API-Key": "k"}).status_code == 400
    assert client.get("/v1/quality", headers={"X-API-Key": "k"}).status_code == 400
    assert client.get("/v1/latency", headers={"X-API-Key": "k"}).status_code == 400


def test_cross_tenant_call_is_404() -> None:
    from obsalt.config import Settings
    from obsalt.search.index import MemorySearchIndex

    plugin = ExamplePlugin()
    resolver = MemoryResolver()
    resolver.add(
        ConnectionConfig(
            org_id="acme",
            provider="example",
            connection_id="c1",
            ingest_key_hash="",
            secrets={"hmac_secret": "s"},
        ),
        "ik",
    )
    state = AppState(
        settings=Settings(),
        plugins=[LoadedPlugin(plugin)],
        resolver=resolver,
        objects=MemoryObjectStore(),
        inbox=MemoryInbox(),
        pointers=MemoryPointerStore(),
        sink=MemoryRevisionSink(),
        keys={
            "acme-key": ("acme", frozenset(KeyScope)),
            "beta-key": ("beta", frozenset(KeyScope)),
        },
        rollup_generation="g1",
        search=MemorySearchIndex(),
    )
    client = api_client(state)
    raw = (EXAMPLE_FIXTURES / "raw" / "call_ended.json").read_bytes()
    ingest = client.post("/v1/ingest/example/ik", content=raw, headers=example_headers(raw))
    assert ingest.status_code == 200
    listed = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z",
        headers={"X-API-Key": "acme-key"},
    )
    call_id = listed.json()["items"][0]["id"]
    foreign = client.get(f"/v1/calls/{call_id}", headers={"X-API-Key": "beta-key"})
    assert foreign.status_code == 404
    beta_list = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z",
        headers={"X-API-Key": "beta-key"},
    )
    assert beta_list.json()["items"] == []


def test_quality_does_not_count_another_orgs_analysis() -> None:
    from datetime import UTC, datetime

    from obsalt.config import Settings
    from obsalt.domain.enums import Speaker
    from obsalt.domain.models import Turn
    from obsalt.search.index import MemorySearchIndex

    plugin = ExamplePlugin()
    resolver = MemoryResolver()
    resolver.add(
        ConnectionConfig(
            org_id="acme",
            provider="example",
            connection_id="c1",
            ingest_key_hash="",
            secrets={"hmac_secret": "s"},
        ),
        "ik",
    )
    sink = MemoryRevisionSink()
    pointers = MemoryPointerStore()
    acme_call = CallRevision(
        org_id="acme",
        call_id="acme-1",
        revision="r-acme",
        source="example",
        source_call_id="ex-acme",
        started_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
        turns=[Turn(index=0, speaker=Speaker.USER, text="hello")],
    )
    beta_call = CallRevision(
        org_id="beta",
        call_id="beta-1",
        revision="r-beta",
        source="example",
        source_call_id="ex-beta",
        started_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
        turns=[Turn(index=0, speaker=Speaker.USER, text="invented $99")],
    )
    sink.write(acme_call)
    sink.write(beta_call)
    pointers.compare_and_swap("acme", "acme-1", None, "r-acme", fact_frontier=frozenset())
    pointers.compare_and_swap("beta", "beta-1", None, "r-beta", fact_frontier=frozenset())
    sink.write_analysis(
        "beta",
        "beta-1",
        "r-beta",
        [
            AnalysisResult(
                execution=AnalysisExecution(
                    call_id="beta-1",
                    revision="r-beta",
                    analyzer_id="hallucination",
                    analyzer_version="1",
                    state=AnalysisState.COMPLETED,
                ),
                payload={"claims": [{"kind": "price_claim", "verdict": "contradicted"}]},
            )
        ],
    )
    state = AppState(
        settings=Settings(),
        plugins=[LoadedPlugin(plugin)],
        resolver=resolver,
        objects=MemoryObjectStore(),
        inbox=MemoryInbox(),
        pointers=pointers,
        sink=sink,
        keys={
            "acme-key": ("acme", frozenset(KeyScope)),
            "beta-key": ("beta", frozenset(KeyScope)),
        },
        rollup_generation="g1",
        search=MemorySearchIndex(),
    )
    client = api_client(state)
    acme_quality = client.get(
        "/v1/quality?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z",
        headers={"X-API-Key": "acme-key"},
    )
    assert acme_quality.status_code == 200
    assert acme_quality.json()["hallucinations"]["count"] == 0
    beta_quality = client.get(
        "/v1/quality?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z",
        headers={"X-API-Key": "beta-key"},
    )
    assert beta_quality.json()["hallucinations"]["count"] == 1


def test_csrf_required_for_ui_mutation() -> None:
    state = example_state()
    client = api_client(state)
    raw = (EXAMPLE_FIXTURES / "raw" / "call_ended.json").read_bytes()
    client.post("/v1/ingest/example/ik", content=raw, headers=example_headers(raw))
    listed = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z",
        headers={"X-API-Key": "k"},
    )
    call_id = listed.json()["items"][0]["id"]
    login = client.post("/v1/ui/login", data={"api_key": "k"}, follow_redirects=False)
    assert login.status_code == 303
    denied = client.post(f"/v1/ui/calls/{call_id}/analyze", data={"csrf": "nope"}, follow_redirects=False)
    assert denied.status_code == 403


def test_delete_by_call_id_only_blocks_replay() -> None:
    state = example_state()
    client = api_client(state)
    raw = (EXAMPLE_FIXTURES / "raw" / "call_ended.json").read_bytes()
    client.post("/v1/ingest/example/ik", content=raw, headers=example_headers(raw))
    listed = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z",
        headers={"X-API-Key": "k"},
    )
    call_id = listed.json()["items"][0]["id"]
    deleted = client.post(
        "/v1/privacy/deletion-requests",
        headers={"X-API-Key": "k"},
        json={"call_id": call_id},
    )
    assert deleted.status_code == 200
    assert deleted.json()["undoable"] is False
    client.post("/v1/ingest/example/ik", content=raw, headers=example_headers(raw))
    listed = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z",
        headers={"X-API-Key": "k"},
    )
    assert listed.json()["items"] == []
