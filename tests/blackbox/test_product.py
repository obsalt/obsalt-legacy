from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from obsalt.api import create_app
from obsalt.assemble.promote import MemoryPointerStore
from obsalt.config import Settings
from obsalt.crypto.primitives import hmac_hex
from obsalt.domain.enums import KeyScope, Statistic
from obsalt.domain.models import AggregateMeasurement, CallRevision, StageMeasurement
from obsalt.ingest.headers import RawHeaders
from obsalt.ingest.receive import ReceiveLimits, object_key_for, receive_webhook
from obsalt.plugin.host import LoadedPlugin
from obsalt.plugin.types import ConnectionConfig
from obsalt.query import latency_rollup, sample_percentile
from obsalt.runtime import AppState
from obsalt.testing.fakes import MemoryInbox, MemoryObjectStore, MemoryResolver
from obsalt.worker.process import MemoryRevisionSink, drain_inbox
from obsalt_example.plugin import ExamplePlugin

FIXTURES = (
    Path(__file__).resolve().parents[2]
    / "packages"
    / "obsalt-example"
    / "src"
    / "obsalt_example"
    / "fixtures"
)


class ExplodingStore(MemoryObjectStore):
    def __init__(self, fail_first: bool = True) -> None:
        super().__init__()
        self.fail_first = fail_first
        self.puts = 0

    def put(self, key: str, body: bytes, *, content_type: str = "application/octet-stream") -> None:
        self.puts += 1
        if self.fail_first:
            self.fail_first = False
            raise RuntimeError("crash after read, before durable put")
        super().put(key, body, content_type=content_type)


class ExplodingInbox(MemoryInbox):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next_accept = True

    def accept(self, envelope, *, tombstone_hints):  # type: ignore[no-untyped-def]
        if self.fail_next_accept:
            self.fail_next_accept = False
            raise RuntimeError("crash after object put, before inbox commit")
        return super().accept(envelope, tombstone_hints=tombstone_hints)


def _state(**kwargs) -> AppState:
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
    kwargs.setdefault("settings", Settings())
    kwargs.setdefault("plugins", [LoadedPlugin(plugin)])
    kwargs.setdefault("resolver", resolver)
    kwargs.setdefault("objects", MemoryObjectStore())
    kwargs.setdefault("inbox", MemoryInbox())
    kwargs.setdefault("pointers", MemoryPointerStore())
    kwargs.setdefault("sink", MemoryRevisionSink())
    kwargs.setdefault("keys", {"k": ("acme", frozenset(KeyScope))})
    return AppState(**kwargs)


def _signed_raw() -> tuple[bytes, dict[str, str]]:
    raw = (FIXTURES / "raw" / "call_ended.json").read_bytes()
    return raw, {
        "x-obsalt-example-signature": hmac_hex("s", raw),
        "content-type": "application/json",
    }


def test_ingest_ack_then_worker_assembles() -> None:
    state = _state()
    client = TestClient(create_app(Settings(), state))
    raw, headers = _signed_raw()
    res = client.post("/v1/ingest/example/ik", content=raw, headers=headers)
    assert res.status_code == 200
    listed = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z",
        headers={"X-API-Key": "k"},
    )
    assert listed.json()["items"]
    detail = client.get(f"/v1/calls/{listed.json()['items'][0]['id']}", headers={"X-API-Key": "k"})
    assert detail.json()["decoder_version"]
    assert detail.json()["coverage"]


def test_crash_before_object_put_retries_cleanly() -> None:
    plugin = ExamplePlugin()
    resolver = MemoryResolver()
    resolver.add(
        ConnectionConfig(
            org_id="acme",
            provider="example",
            connection_id="c",
            ingest_key_hash="",
            secrets={"hmac_secret": "s"},
        ),
        "ik",
    )
    objects = ExplodingStore()
    inbox = MemoryInbox()
    raw, headers = _signed_raw()
    header_pairs = RawHeaders.from_mapping(headers)
    try:
        receive_webhook(
            provider="example",
            ingest_key="ik",
            raw=raw,
            headers=header_pairs,
            resolver=resolver,
            plugin=plugin,
            objects=objects,
            inbox=inbox,
            limits=ReceiveLimits(),
        )
        raise AssertionError("expected crash")
    except RuntimeError:
        pass
    assert inbox.outbox == []
    objects.fail_first = False
    result = receive_webhook(
        provider="example",
        ingest_key="ik",
        raw=raw,
        headers=header_pairs,
        resolver=resolver,
        plugin=plugin,
        objects=objects,
        inbox=inbox,
        limits=ReceiveLimits(),
    )
    assert result.envelope is not None
    assert result.created is True


def test_crash_after_put_resumes_incomplete_acceptance() -> None:
    plugin = ExamplePlugin()
    resolver = MemoryResolver()
    resolver.add(
        ConnectionConfig(
            org_id="acme",
            provider="example",
            connection_id="c",
            ingest_key_hash="",
            secrets={"hmac_secret": "s"},
        ),
        "ik",
    )
    objects = MemoryObjectStore()
    inbox = ExplodingInbox()
    raw, headers = _signed_raw()
    header_pairs = RawHeaders.from_mapping(headers)
    try:
        receive_webhook(
            provider="example",
            ingest_key="ik",
            raw=raw,
            headers=header_pairs,
            resolver=resolver,
            plugin=plugin,
            objects=objects,
            inbox=inbox,
            limits=ReceiveLimits(),
        )
        raise AssertionError("expected crash")
    except RuntimeError:
        pass
    key = next(iter(objects.blobs))
    assert key.startswith("org/acme/raw/")
    result = receive_webhook(
        provider="example",
        ingest_key="ik",
        raw=raw,
        headers=header_pairs,
        resolver=resolver,
        plugin=plugin,
        objects=objects,
        inbox=inbox,
        limits=ReceiveLimits(),
    )
    assert result.created is True
    assert (
        object_key_for(
            "acme", "example", result.envelope.delivery_key, result.envelope.content_sha256
        )
        in objects.blobs
    )  # type: ignore[union-attr]


def test_delete_by_call_cannot_be_replayed() -> None:
    state = _state()
    client = TestClient(create_app(Settings(), state))
    raw, headers = _signed_raw()
    client.post("/v1/ingest/example/ik", content=raw, headers=headers)
    items = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z",
        headers={"X-API-Key": "k"},
    ).json()["items"]
    call_id = items[0]["id"]
    source_call_id = client.get(f"/v1/calls/{call_id}", headers={"X-API-Key": "k"}).json()[
        "source_call_id"
    ]
    deleted = client.post(
        "/v1/privacy/deletion-requests",
        headers={"X-API-Key": "k"},
        json={"call_id": call_id, "source_call_id": source_call_id},
    )
    assert deleted.json()["undoable"] is False
    listed = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z",
        headers={"X-API-Key": "k"},
    )
    assert listed.json()["items"] == []
    # Replay of the raw webhook is acknowledged but must not resurrect the call.
    client.post("/v1/ingest/example/ik", content=raw, headers=headers)
    drain_inbox(state)
    listed = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z",
        headers={"X-API-Key": "k"},
    )
    assert listed.json()["items"] == []


def test_aggregates_never_enter_sample_percentiles() -> None:
    samples = [
        StageMeasurement(
            fact_id="a",
            stage="e2e",  # type: ignore[arg-type]
            metric="duration",  # type: ignore[arg-type]
            value_ms=580,
            placement="unplaced",  # type: ignore[arg-type]
            provenance="provider_reported",  # type: ignore[arg-type]
        )
    ]
    aggs = [
        AggregateMeasurement(
            fact_id="b",
            stage="e2e",  # type: ignore[arg-type]
            metric="duration",  # type: ignore[arg-type]
            statistic=Statistic.P95,
            value_ms=10_000,
            provenance="provider_reported",  # type: ignore[arg-type]
        )
    ]
    call = CallRevision(
        org_id="o",
        call_id="c",
        revision="r",
        source="retell",
        source_call_id="s",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        stage_measurements=samples,
        aggregate_measurements=aggs,
    )
    rollup = latency_rollup([call], as_of_generation="g")
    assert rollup["as_of_generation"] == "g"
    assert rollup["aggregates_excluded"] == 1
    assert rollup["items"][0]["p50"] == 580
    assert sample_percentile([580, 10_000], 50) != 580


def test_missing_hmac_fails_closed() -> None:
    plugin = ExamplePlugin()
    resolver = MemoryResolver()
    resolver.add(
        ConnectionConfig(
            org_id="acme", provider="example", connection_id="c", ingest_key_hash="", secrets={}
        ),
        "ik",
    )
    raw, headers = _signed_raw()
    result = receive_webhook(
        provider="example",
        ingest_key="ik",
        raw=raw,
        headers=RawHeaders.from_mapping(headers),
        resolver=resolver,
        plugin=plugin,
        objects=MemoryObjectStore(),
        inbox=MemoryInbox(),
    )
    assert result.response.status_code == 401
    assert result.rejected == "missing_credential"
