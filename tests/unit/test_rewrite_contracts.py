"""Contracts that the rewrite plan treats as load-bearing."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from obsalt.api.app import create_app
from obsalt.assemble.assembler import fold_events, stamp_events
from obsalt.config import Settings
from obsalt.crypto.keys import hash_secret
from obsalt.domain.enums import MeasurementPlacement, PipelineArchitecture, Provenance, Speaker, Stage, Metric
from obsalt.domain.events import CallObserved, StageObserved, TurnObserved
from obsalt.otel.mappers import SpanView
from obsalt.plugin.protocol import ConnectionConfig, RedactedDocument
from obsalt.runtime import ApiPrincipal, Runtime
from obsalt.search.embedder import NgramEmbedder
from obsalt.search.hybrid import hybrid_search
from obsalt_example.plugin import ExamplePlugin
from obsalt_pipecat.plugin import PipecatPlugin


def _runtime() -> tuple[Runtime, str, str]:
    runtime = Runtime.create(Settings(demo=True), extra_plugins=[ExamplePlugin()])
    creds = runtime.bootstrap_dev_org("acme")
    runtime.api_keys[hash_secret(creds["api_key"])] = ApiPrincipal(
        org_id="acme", scope="admin", kind="service", role="owner"
    )
    runtime.put_connection(
        ConnectionConfig(org_id="acme", provider="example", connection_id="ex", credentials={"shared_secret": "s"}),
        creds["ingest_key"],
    )
    return runtime, creds["api_key"], creds["ingest_key"]


def test_stamp_applies_call_key_to_every_event() -> None:
    events = [
        CallObserved(source_call_id="c1", agent_id="a"),
        TurnObserved(turn_index=0, speaker=Speaker.USER, text="hi"),
        StageObserved(
            stage=Stage.STT,
            metric=Metric.DURATION,
            value_ms=10,
            placement=MeasurementPlacement.UNPLACED,
            provenance=Provenance.PROVIDER_REPORTED,
        ),
    ]
    stamped = stamp_events(
        events, org_id="o", source="example", envelope_id="e", decoder_version="example/1", processing_run_id="r"
    )
    assert all(event.call_key == "o:example:c1" for event in stamped)
    assert all(event.org_id == "o" for event in stamped)
    assert all(event.fact_id for event in stamped)


def test_replay_keeps_prior_revision() -> None:
    runtime, _key, ingest = _runtime()
    client = TestClient(create_app(runtime))
    raw = b'{"event":"call.completed","call_id":"replay-1","turns":[{"index":0,"speaker":"user","text":"hi","started_at":"2026-08-22T12:00:00Z","ended_at":"2026-08-22T12:00:01Z"}],"outcome":{"code":"completed"}}'
    client.post(f"/v1/ingest/example/{ingest}", content=raw, headers={"x-example-secret": "s"})
    first = next(iter(runtime.calls.values()))
    first_rev = first.revision
    replayed = runtime.replay_org("acme", provider="example")
    assert replayed >= 1
    current = runtime.get_revision("acme", first.call_id)
    assert current is not None
    assert current.revision == first_rev + 1
    prior = runtime.get_revision("acme", first.call_id, first_rev)
    assert prior is not None
    assert prior.revision == first_rev


def test_hybrid_search_finds_refund_language() -> None:
    docs = [
        ("a", 1, "customer asked about a refund for order ORD-1"),
        ("b", 1, "weather is nice today"),
    ]
    hits = hybrid_search("customers asking about refunds", docs)
    assert hits
    assert hits[0].call_id == "a"


def test_ngram_embedder_is_not_a_document_hash() -> None:
    embedder = NgramEmbedder()
    left = embedder.embed_sync([RedactedDocument(document_id="1", text="refund the invoice")])[0].values
    right = embedder.embed_sync([RedactedDocument(document_id="2", text="please refund my invoice")])[0].values
    assert left != right
    assert sum(a * b for a, b in zip(left, right, strict=False)) > 0.2


def test_pipecat_interval_has_real_timestamps() -> None:
    start = datetime(2026, 8, 22, 12, 0, 0, tzinfo=timezone.utc).timestamp() * 1e9
    end = start + 120_000_000
    span = SpanView(
        name="stt.transcription",
        attributes={"gen_ai.conversation.id": "pipe-1", "gen_ai.provider.name": "pipecat"},
        start_time=start,
        end_time=end,
    )
    events = list(PipecatPlugin().decode([span]))
    stages = [e for e in events if isinstance(e, StageObserved)]
    assert stages
    assert stages[0].placement is MeasurementPlacement.INTERVAL
    assert stages[0].started_at and stages[0].ended_at


def test_otlp_json_assembles_and_rejects_mixed_org() -> None:
    runtime, key, _ingest = _runtime()
    runtime.host._register(PipecatPlugin(), source="test")
    client = TestClient(create_app(runtime))
    payload = {
        "resourceSpans": [
            {
                "resource": {"attributes": [{"key": "obsalt.org", "value": {"stringValue": "acme"}}]},
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "name": "stt.transcription",
                                "startTimeUnixNano": 1755777600000000000,
                                "endTimeUnixNano": 1755777600120000000,
                                "attributes": [
                                    {"key": "gen_ai.conversation.id", "value": {"stringValue": "otlp-1"}},
                                    {"key": "gen_ai.provider.name", "value": {"stringValue": "pipecat"}},
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }
    ok = client.post("/v1/traces", json=payload, headers={"X-API-Key": key, "content-type": "application/json"})
    assert ok.status_code == 200
    mixed = {
        "resourceSpans": [
            {
                "resource": {"attributes": [{"key": "obsalt.org", "value": {"stringValue": "other"}}]},
                "scopeSpans": [{"spans": [{"name": "turn", "attributes": []}]}],
            }
        ]
    }
    denied = client.post("/v1/traces", json=mixed, headers={"X-API-Key": key, "content-type": "application/json"})
    assert denied.status_code == 400


def test_ui_surfaces_and_analyze() -> None:
    runtime, key, ingest = _runtime()
    client = TestClient(create_app(runtime))
    raw = b'{"event":"call.completed","call_id":"ui-1","system_prompt":"be honest","turns":[{"index":0,"speaker":"user","text":"refund please","started_at":"2026-08-22T12:00:00Z","ended_at":"2026-08-22T12:00:01Z"},{"index":1,"speaker":"agent","text":"I refunded ORD-99999","started_at":"2026-08-22T12:00:01Z","ended_at":"2026-08-22T12:00:02Z"}],"outcome":{"code":"user_hangup"}}'
    client.post(f"/v1/ingest/example/{ingest}", content=raw, headers={"x-example-secret": "s"})
    headers = {"X-API-Key": key}
    assert client.get("/v1/ui/latency", headers=headers).status_code == 200
    assert client.get("/v1/ui/hangups", headers=headers).status_code == 200
    assert client.get("/v1/ui/quality", headers=headers).status_code == 200
    assert client.get("/v1/ui/search?q=refund", headers=headers).status_code == 200
    assert client.get("/v1/ui/settings", headers=headers).status_code == 200
    listed = client.get(
        "/v1/calls",
        headers=headers,
        params={"from": "2020-01-01T00:00:00Z", "to": "2030-01-01T00:00:00Z"},
    )
    call_id = listed.json()["items"][0]["call_id"]
    analyzed = client.post(f"/v1/calls/{call_id}/analyze", headers=headers)
    assert analyzed.status_code == 200
    assert analyzed.json()["state"] in {"completed", "pending", "failed"}


def test_unplaced_and_interval_fold() -> None:
    events = [
        CallObserved(source_call_id="c1", architecture=PipelineArchitecture.CASCADE),
        StageObserved(
            stage=Stage.LLM,
            metric=Metric.DURATION,
            value_ms=20,
            placement=MeasurementPlacement.UNPLACED,
            provenance=Provenance.PROVIDER_REPORTED,
            source_path="x",
        ),
    ]
    revision = fold_events(events, org_id="o", source="vapi", source_call_id="c1")
    assert revision.lifecycle.timeline_fidelity.value in {"call_level", "none"}
