"""Crash-point tests for receive + drain. No docker required."""

from __future__ import annotations

from pathlib import Path

from obsalt.assemble.promote import MemoryPointerStore
from obsalt.config import Settings
from obsalt.crypto.primitives import hmac_hex
from obsalt.domain.enums import EnvelopeState, VerifyOutcome
from obsalt.ingest.headers import RawHeaders
from obsalt.ingest.receive import receive_webhook
from obsalt.plugin.host import LoadedPlugin
from obsalt.plugin.types import ConnectionConfig, RawEnvelope, TombstoneHints
from obsalt.runtime import AppState
from obsalt.testing.fakes import MemoryInbox, MemoryObjectStore, MemoryResolver
from obsalt.worker.drain import drain_once, process_after_ack, process_outbox
from obsalt.worker.process import MemoryRevisionSink
from obsalt_example.plugin import ExamplePlugin

FIXTURES = Path(__file__).resolve().parents[1] / "packages" / "obsalt-example" / "src" / "obsalt_example" / "fixtures"


class CrashingObjectStore:
    def __init__(self, inner: MemoryObjectStore, *, fail_times: int = 1) -> None:
        self.inner = inner
        self.fail_times = fail_times
        self.puts = 0

    def put(self, key: str, body: bytes, *, content_type: str = "application/octet-stream") -> None:
        self.puts += 1
        if self.puts <= self.fail_times:
            raise RuntimeError("object put crashed")
        self.inner.put(key, body, content_type=content_type)

    def get(self, key: str) -> bytes:
        return self.inner.get(key)

    def delete(self, key: str) -> None:
        self.inner.delete(key)


class CrashingInbox:
    """Fails at accept. When after_commit=True, the inner write happens first (resume on retry)."""

    def __init__(self, inner: MemoryInbox, *, fail_accepts: int = 1, after_commit: bool = True) -> None:
        self.inner = inner
        self.fail_accepts = fail_accepts
        self.after_commit = after_commit
        self.accepts = 0

    def accept(self, envelope: RawEnvelope, *, tombstone_hints: TombstoneHints) -> tuple[RawEnvelope, bool]:
        self.accepts += 1
        if self.accepts <= self.fail_accepts and not self.after_commit:
            raise RuntimeError("inbox accept crashed")
        result = self.inner.accept(envelope, tombstone_hints=tombstone_hints)
        if self.accepts <= self.fail_accepts and self.after_commit:
            raise RuntimeError("inbox accept crashed after commit")
        return result

    def is_tombstoned(self, org_id: str, hints: TombstoneHints) -> bool:
        return self.inner.is_tombstoned(org_id, hints)

    def tombstone(self, org_id: str, hints: TombstoneHints) -> None:
        self.inner.tombstone(org_id, hints)

    def claim_outbox(self, limit: int = 32) -> list[RawEnvelope]:
        return self.inner.claim_outbox(limit)

    def mark_assembled(self, envelope_id: str) -> None:
        self.inner.mark_assembled(envelope_id)

    def mark_failed(self, envelope_id: str, error: str) -> None:
        self.inner.mark_failed(envelope_id, error)

    def get_by_id(self, envelope_id: str) -> RawEnvelope | None:
        return self.inner.get_by_id(envelope_id)


class DownLeases:
    def pop_ready(self, limit: int = 32) -> list[str]:
        raise RuntimeError("redis down")

    def acquire(self, envelope_id: str, owner: str, ttl: int) -> bool:
        raise RuntimeError("redis down")

    def release(self, envelope_id: str) -> None:
        raise RuntimeError("redis down")


def _plugin() -> ExamplePlugin:
    return ExamplePlugin()


def _raw() -> bytes:
    return (FIXTURES / "raw" / "call_ended.json").read_bytes()


def _headers(raw: bytes, secret: str = "s") -> RawHeaders:
    return RawHeaders.from_mapping(
        {"x-obsalt-example-signature": hmac_hex(secret, raw), "content-type": "application/json"}
    )


def _resolver(secret: str = "s") -> MemoryResolver:
    resolver = MemoryResolver()
    resolver.add(
        ConnectionConfig(
            org_id="acme",
            provider="example",
            connection_id="c1",
            ingest_key_hash="",
            secrets={"hmac_secret": secret},
        ),
        "ik",
    )
    return resolver


def _state(
    *,
    objects: MemoryObjectStore | CrashingObjectStore | None = None,
    inbox: MemoryInbox | CrashingInbox | None = None,
    resolver: MemoryResolver | None = None,
) -> AppState:
    plugin = _plugin()
    return AppState(
        settings=Settings(),
        plugins=[LoadedPlugin(plugin)],
        resolver=resolver or _resolver(),
        objects=objects or MemoryObjectStore(),
        inbox=inbox or MemoryInbox(),
        pointers=MemoryPointerStore(),
        sink=MemoryRevisionSink(),
    )


def _receive(objects, inbox, resolver=None):
    raw = _raw()
    return receive_webhook(
        provider="example",
        ingest_key="ik",
        raw=raw,
        headers=_headers(raw),
        resolver=resolver or _resolver(),
        plugin=_plugin(),
        objects=objects,
        inbox=inbox,
    )


def test_put_crash_then_retry_succeeds() -> None:
    inner = MemoryObjectStore()
    objects = CrashingObjectStore(inner, fail_times=1)
    inbox = MemoryInbox()
    try:
        _receive(objects, inbox)
        raise AssertionError("put should have crashed")
    except RuntimeError as exc:
        assert "put crashed" in str(exc)
    assert inner.blobs == {}
    assert inbox.envelopes == {}

    result = _receive(objects, inbox)
    assert result.created is True
    assert result.envelope is not None
    assert result.envelope.state is EnvelopeState.QUEUED
    assert inner.blobs


def test_accept_crash_after_put_resumes_incomplete_acceptance() -> None:
    objects = MemoryObjectStore()
    inner = MemoryInbox()
    inbox = CrashingInbox(inner, fail_accepts=1, after_commit=True)
    try:
        _receive(objects, inbox)
        raise AssertionError("accept should have crashed")
    except RuntimeError as exc:
        assert "accept crashed" in str(exc)
    assert objects.blobs
    stored = next(iter(inner.envelopes.values()))
    assert stored.state is not EnvelopeState.ASSEMBLED
    assert stored.envelope_id in inner.outbox

    result = _receive(objects, inbox)
    assert result.created is False
    assert result.envelope is not None
    assert result.envelope.envelope_id == stored.envelope_id
    assert result.envelope.state is not EnvelopeState.ASSEMBLED
    assert stored.envelope_id in inner.outbox


def test_tombstoned_call_cannot_be_resurrected_by_replay() -> None:
    objects = MemoryObjectStore()
    inbox = MemoryInbox()
    first = _receive(objects, inbox)
    assert first.created is True
    assert first.envelope is not None
    inbox.tombstone("acme", TombstoneHints(source_call_id="ex-1"))

    replay = _receive(objects, inbox)
    assert replay.rejected == "tombstoned"
    assert replay.envelope is None

    state = _state(objects=objects, inbox=inbox)
    processed = process_outbox(state, limit=8)
    assert processed == 0
    assert state.sink.revisions == {}
    assert inbox.get_by_id(first.envelope.envelope_id) is not None
    assert inbox.get_by_id(first.envelope.envelope_id).state is EnvelopeState.TOMBSTONED


def test_missing_credentials_fail_closed() -> None:
    resolver = MemoryResolver()
    resolver.add(
        ConnectionConfig(
            org_id="acme",
            provider="example",
            connection_id="c1",
            ingest_key_hash="",
            secrets={},
        ),
        "ik",
    )
    raw = _raw()
    result = receive_webhook(
        provider="example",
        ingest_key="ik",
        raw=raw,
        headers=_headers(raw),
        resolver=resolver,
        plugin=_plugin(),
        objects=MemoryObjectStore(),
        inbox=MemoryInbox(),
    )
    assert result.response.status_code == 401
    assert result.rejected == VerifyOutcome.MISSING_CREDENTIAL.value
    assert result.envelope is None


def test_drain_once_assembles_after_receive() -> None:
    state = _state()
    result = _receive(state.objects, state.inbox, state.resolver)
    assert result.created is True
    n = drain_once(state)
    assert n == 1
    assert result.envelope is not None
    stored = state.inbox.get_by_id(result.envelope.envelope_id)
    assert stored is not None
    assert stored.state is EnvelopeState.ASSEMBLED
    assert state.sink.revisions
    assert state.pointers.get("acme", next(iter(state.sink.revisions.values())).call_id)


def test_process_after_ack_is_safe_as_background_task() -> None:
    state = _state()
    result = _receive(state.objects, state.inbox, state.resolver)
    assert result.envelope is not None
    process_after_ack(state, result.envelope)
    stored = state.inbox.get_by_id(result.envelope.envelope_id)
    assert stored is not None and stored.state is EnvelopeState.ASSEMBLED


def test_tombstone_before_drain_does_not_persist_normalized() -> None:
    state = _state()
    result = _receive(state.objects, state.inbox, state.resolver)
    assert result.envelope is not None
    state.inbox.tombstone("acme", TombstoneHints(source_call_id="ex-1"))
    n = drain_once(state)
    assert n == 0
    assert state.sink.revisions == {}


def test_unknown_ingest_key_does_not_write_blob() -> None:
    objects = MemoryObjectStore()
    inbox = MemoryInbox()
    raw = _raw()
    result = receive_webhook(
        provider="example",
        ingest_key="no-such-key",
        raw=raw,
        headers=_headers(raw),
        resolver=_resolver(),
        plugin=_plugin(),
        objects=objects,
        inbox=inbox,
    )
    assert result.response.status_code == 404
    assert result.rejected == "unknown ingest key"
    assert objects.blobs == {}
    assert inbox.by_id == {}


def test_compressed_body_over_limit_rejects_before_auth() -> None:
    from obsalt.ingest.receive import ReceiveLimits

    objects = MemoryObjectStore()
    inbox = MemoryInbox()
    raw = _raw()
    result = receive_webhook(
        provider="example",
        ingest_key="ik",
        raw=raw,
        headers=_headers(raw),
        resolver=_resolver(),
        plugin=_plugin(),
        objects=objects,
        inbox=inbox,
        limits=ReceiveLimits(compressed_bytes=8),
    )
    assert result.response.status_code == 413
    assert result.envelope is None
    assert objects.blobs == {}
    assert inbox.by_id == {}


def test_duplicate_singleton_header_rejects_without_persist() -> None:
    objects = MemoryObjectStore()
    inbox = MemoryInbox()
    raw = _raw()
    sig = hmac_hex("s", raw).encode("latin-1")
    result = receive_webhook(
        provider="example",
        ingest_key="ik",
        raw=raw,
        headers=RawHeaders(
            [
                (b"x-obsalt-example-signature", sig),
                (b"x-obsalt-example-signature", sig),
                (b"content-type", b"application/json"),
            ]
        ),
        resolver=_resolver(),
        plugin=_plugin(),
        objects=objects,
        inbox=inbox,
    )
    assert result.response.status_code == 400
    assert result.envelope is None
    assert objects.blobs == {}
    assert inbox.by_id == {}


def test_redis_down_falls_back_to_inbox_claim() -> None:
    state = _state()
    state.leases = DownLeases()  # type: ignore[attr-defined]
    result = _receive(state.objects, state.inbox, state.resolver)
    assert result.created is True
    n = process_outbox(state, limit=8)
    assert n == 1
    assert result.envelope is not None
    stored = state.inbox.get_by_id(result.envelope.envelope_id)
    assert stored is not None and stored.state is EnvelopeState.ASSEMBLED
