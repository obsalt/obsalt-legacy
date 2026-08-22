from __future__ import annotations

import time

from obsalt.auth.primitives import hmac_hex
from obsalt.domain.enums import EnvelopeState, VerifyOutcome
from obsalt.ingest.receive import ReceiveService
from obsalt.plugin.host import PluginHost
from obsalt.plugin.protocol import ConnectionConfig
from obsalt.storage.memory import MemoryInbox, MemoryObjects, MemoryResolver
from obsalt_example.plugin import ExamplePlugin
from obsalt_retell.plugin import RetellPlugin
from obsalt_vapi.plugin import VapiPlugin


def _svc(*plugins: object, connections: dict) -> ReceiveService:
    host = PluginHost()
    for plugin in plugins:
        host._register(plugin, source="test")
    return ReceiveService(
        host=host,
        resolver=MemoryResolver(connections),
        objects=MemoryObjects(),
        inbox=MemoryInbox(),
    )


def test_vapi_missing_secret_fails_closed() -> None:
    cfg = ConnectionConfig(org_id="o", provider="vapi", connection_id="c", credentials={})
    svc = _svc(VapiPlugin(), connections={("vapi", "k"): cfg})
    result = svc.handle(provider="vapi", ingest_key="k", raw=b"{}", headers=[])
    assert result.verify and result.verify.outcome is VerifyOutcome.MISSING_CREDENTIAL
    assert result.response.status_code == 401


def test_vapi_legacy_secret_accepts() -> None:
    cfg = ConnectionConfig(
        org_id="o", provider="vapi", connection_id="c", credentials={"shared_secret": "s3cret"}
    )
    svc = _svc(VapiPlugin(), connections={("vapi", "k"): cfg})
    result = svc.handle(
        provider="vapi",
        ingest_key="k",
        raw=b'{"message":{"type":"end-of-call-report","call":{"id":"c1"}}}',
        headers=[(b"x-vapi-secret", b"s3cret")],
    )
    assert result.verify and result.verify.ok
    assert result.state is EnvelopeState.QUEUED
    assert result.response.status_code == 200


def test_retell_real_signature() -> None:
    body = b'{"event":"call_ended","call":{"call_id":"c1"}}'
    ts = str(int(time.time() * 1000))
    sig = hmac_hex("api-key", body + ts.encode())
    header = f"v={ts},d={sig}".encode()
    cfg = ConnectionConfig(
        org_id="o", provider="retell", connection_id="c", credentials={"api_key": "api-key"}
    )
    svc = _svc(
        RetellPlugin(),
        connections={("retell", "k"): cfg},
    )
    result = svc.handle(
        provider="retell", ingest_key="k", raw=body, headers=[(b"x-retell-signature", header)]
    )
    assert result.verify and result.verify.ok
    assert result.response.status_code == 204


def test_authorization_headers_are_not_archived() -> None:
    cfg = ConnectionConfig(
        org_id="o", provider="example", connection_id="c", credentials={"shared_secret": "s"}
    )
    objects = MemoryObjects()
    inbox = MemoryInbox()
    host = PluginHost()
    host._register(ExamplePlugin(), source="test")
    svc = ReceiveService(
        host=host, resolver=MemoryResolver({("example", "k"): cfg}), objects=objects, inbox=inbox
    )
    body = b'{"event":"call.completed","call_id":"c1"}'
    svc.handle(
        provider="example",
        ingest_key="k",
        raw=body,
        headers=[
            (b"x-example-secret", b"s"),
            (b"authorization", b"Bearer nope"),
            (b"content-type", b"application/json"),
        ],
    )
    meta = next(iter(objects.meta.values()))
    assert "authorization" not in {k.lower() for k in meta}
    assert "content-type" in {k.lower() for k in meta}


def test_tombstone_purges_orphan() -> None:
    cfg = ConnectionConfig(
        org_id="o", provider="example", connection_id="c", credentials={"shared_secret": "s"}
    )
    objects = MemoryObjects()
    inbox = MemoryInbox()
    inbox.add_tombstone(org_id="o", source_call_id="c1")
    host = PluginHost()
    host._register(ExamplePlugin(), source="test")
    svc = ReceiveService(
        host=host, resolver=MemoryResolver({("example", "k"): cfg}), objects=objects, inbox=inbox
    )
    result = svc.handle(
        provider="example",
        ingest_key="k",
        raw=b'{"event":"call.completed","call_id":"c1"}',
        headers=[(b"x-example-secret", b"s")],
    )
    assert result.state is EnvelopeState.TOMBSTONED
    assert objects.blobs == {}


def test_duplicate_delivery_resumes() -> None:
    cfg = ConnectionConfig(
        org_id="o", provider="example", connection_id="c", credentials={"shared_secret": "s"}
    )
    objects = MemoryObjects()
    inbox = MemoryInbox()
    host = PluginHost()
    host._register(ExamplePlugin(), source="test")
    svc = ReceiveService(
        host=host, resolver=MemoryResolver({("example", "k"): cfg}), objects=objects, inbox=inbox
    )
    body = b'{"event":"call.completed","call_id":"c1"}'
    headers = [(b"x-example-secret", b"s")]
    first = svc.handle(provider="example", ingest_key="k", raw=body, headers=headers)
    second = svc.handle(provider="example", ingest_key="k", raw=body, headers=headers)
    assert first.created is True
    assert second.created is False
    assert first.envelope_id == second.envelope_id


def test_crash_after_blob_before_inbox_is_retryable() -> None:
    cfg = ConnectionConfig(
        org_id="o", provider="example", connection_id="c", credentials={"shared_secret": "s"}
    )

    class BoomInbox(MemoryInbox):
        def accept(self, *args: object, **kwargs: object) -> tuple[str, bool]:
            raise RuntimeError("crash after blob")

    objects = MemoryObjects()
    host = PluginHost()
    host._register(ExamplePlugin(), source="test")
    boom = ReceiveService(
        host=host, resolver=MemoryResolver({("example", "k"): cfg}), objects=objects, inbox=BoomInbox()
    )
    body = b'{"event":"call.completed","call_id":"c1"}'
    headers = [(b"x-example-secret", b"s")]
    try:
        boom.handle(provider="example", ingest_key="k", raw=body, headers=headers)
        raise AssertionError("expected crash")
    except RuntimeError:
        pass
    assert objects.blobs
    recovered = ReceiveService(
        host=host, resolver=MemoryResolver({("example", "k"): cfg}), objects=objects, inbox=MemoryInbox()
    )
    result = recovered.handle(provider="example", ingest_key="k", raw=body, headers=headers)
    assert result.created is True
    assert result.state is EnvelopeState.QUEUED
