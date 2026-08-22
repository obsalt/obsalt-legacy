"""Remaining rewrite-plan contracts that prior v2 PRs left unwired."""

from __future__ import annotations

import gzip
import time
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from obsalt.analysis.cluster import ClickHouseHangupClusterStore
from obsalt.analysis.contributions import ClickHouseRollupStore
from obsalt.analysis.tier2 import decide_tier2
from obsalt.api import create_app
from obsalt.assemble.promote import MemoryPointerStore
from obsalt.config import Settings
from obsalt.crypto.primitives import hmac_hex, jwt_hs256_sign
from obsalt.domain.enums import (
    AnalysisState,
    HangupReason,
    MeasurementPlacement,
    Metric,
    Provenance,
    Stage,
    ToolStatus,
    VerifyOutcome,
)
from obsalt.domain.events import GroundingObserved, StageObserved
from obsalt.domain.models import CallRevision, Hangup, StageMeasurement, ToolInvocation
from obsalt.ingest.headers import RawHeaders
from obsalt.ingest.otlp import receive_otlp_batch
from obsalt.ingest.receive import receive_webhook
from obsalt.otel.conventions import SPAN_STT_PROVIDER_ATTEMPT
from obsalt.otel.foreign import ForeignConventionMapper
from obsalt.otel.forwarder import forward_otlp_batch
from obsalt.plugin.types import ConnectionConfig, ReadableSpan
from obsalt.runtime import in_memory_state
from obsalt.store.postgres import PostgresInbox
from obsalt.testing.fakes import MemoryInbox, MemoryObjectStore, MemoryResolver
from obsalt.webhooks.outbound import maybe_emit_slo, mint_whsec
from obsalt.worker.process import MemoryRevisionSink, process_normalized_events
from obsalt_cartesia.plugin import CartesiaPlugin
from obsalt_elevenlabs.plugin import ElevenLabsPlugin
from obsalt_example.plugin import ExamplePlugin
from obsalt_pipecat.plugin import PipecatPlugin
from obsalt_testkit import decode_raw_fixture
from obsalt_vapi.plugin import VapiPlugin

ROOT = Path(__file__).resolve().parents[1]


class _RecordingCH:
    def __init__(self) -> None:
        self.commands: list[tuple[str, dict | None]] = []
        self.inserts: list[tuple[str, list, list | None]] = []

    def command(self, sql: str, parameters: dict | None = None) -> None:
        self.commands.append((sql, parameters))

    def insert(self, table: str, rows: list, column_names: list | None = None) -> None:
        self.inserts.append((table, rows, column_names))

    def query(self, sql: str, parameters: dict | None = None) -> object:
        return type("R", (), {"result_rows": []})()


def test_webhook_authenticates_gzip_wire_bytes() -> None:
    plugin = ExamplePlugin()
    raw = (
        ROOT
        / "packages/obsalt-example/src/obsalt_example/fixtures/raw/call_ended.json"
    ).read_bytes()
    wire = gzip.compress(raw)
    secret = "example-secret"
    sig = hmac_hex(secret, wire)
    cfg = ConnectionConfig(
        org_id="acme",
        provider="example",
        connection_id="c1",
        ingest_key_hash="x",
        secrets={"hmac_secret": secret},
    )
    resolver = MemoryResolver()
    resolver.add(cfg, "ik")
    objects = MemoryObjectStore()
    inbox = MemoryInbox()
    result = receive_webhook(
        provider="example",
        ingest_key="ik",
        raw=wire,
        headers=RawHeaders.from_mapping(
            {"x-obsalt-example-signature": sig, "content-encoding": "gzip"}
        ),
        resolver=resolver,
        plugin=plugin,
        objects=objects,
        inbox=inbox,
        content_encoding="gzip",
    )
    assert result.rejected is None
    assert result.envelope is not None
    assert result.envelope.body == wire
    assert result.envelope.headers.get("content-encoding") == "gzip"
    stored = objects.get(result.envelope.object_key)
    assert stored == wire


def test_webhook_auth_falls_back_to_expanded_body() -> None:
    plugin = ExamplePlugin()
    raw = (
        ROOT
        / "packages/obsalt-example/src/obsalt_example/fixtures/raw/call_ended.json"
    ).read_bytes()
    wire = gzip.compress(raw)
    secret = "example-secret"
    sig = hmac_hex(secret, raw)
    cfg = ConnectionConfig(
        org_id="acme",
        provider="example",
        connection_id="c1",
        ingest_key_hash="x",
        secrets={"hmac_secret": secret},
    )
    resolver = MemoryResolver()
    resolver.add(cfg, "ik")
    result = receive_webhook(
        provider="example",
        ingest_key="ik",
        raw=wire,
        headers=RawHeaders.from_mapping(
            {"x-obsalt-example-signature": sig, "content-encoding": "gzip"}
        ),
        resolver=resolver,
        plugin=plugin,
        objects=MemoryObjectStore(),
        inbox=MemoryInbox(),
        content_encoding="gzip",
    )
    assert result.rejected is None
    assert result.envelope is not None
    assert result.envelope.body == wire


def test_vapi_oauth2_uses_jwt_not_static_bearer() -> None:
    plugin = VapiPlugin()
    secret = "oauth-secret"
    token = jwt_hs256_sign({"sub": "vapi", "exp": time.time() + 60}, secret)
    cfg = ConnectionConfig(
        org_id="acme",
        provider="vapi",
        connection_id="c1",
        ingest_key_hash="x",
        secrets={"oauth_token": secret},
        settings={"auth_mode": "oauth2"},
    )
    ok = plugin.authenticate(b"{}", [("authorization", f"Bearer {token}".encode())], cfg)
    assert ok.ok
    expired = jwt_hs256_sign({"sub": "vapi", "exp": time.time() - 10}, secret)
    bad = plugin.authenticate(b"{}", [("authorization", f"Bearer {expired}".encode())], cfg)
    assert not bad.ok
    assert bad.outcome is VerifyOutcome.BAD_SIGNATURE
    static = plugin.authenticate(b"{}", [("authorization", b"Bearer oauth-secret")], cfg)
    assert not static.ok


def test_elevenlabs_and_cartesia_emit_user_grounding() -> None:
    eleven = decode_raw_fixture(
        ElevenLabsPlugin(),
        ROOT / "packages/obsalt-elevenlabs/src/obsalt_elevenlabs/fixtures/raw/post_call_transcription.json",
    )
    assert any(isinstance(event, GroundingObserved) and "refund" in event.content.lower() for event in eleven)
    cartesia = decode_raw_fixture(
        CartesiaPlugin(),
        ROOT / "packages/obsalt-cartesia/src/obsalt_cartesia/fixtures/raw/call_ended.json",
    )
    assert any(isinstance(event, GroundingObserved) and event.content for event in cartesia)


def test_pipecat_decodes_stt_provider_attempt_and_ttfb() -> None:
    plugin = PipecatPlugin()
    span = ReadableSpan(
        name=SPAN_STT_PROVIDER_ATTEMPT,
        trace_id="a" * 32,
        span_id="b" * 16,
        start_unix_nano=1_000_000_000,
        end_unix_nano=1_200_000_000,
        attributes={"metrics.ttfb": 42.0, "gen_ai.conversation.id": "conv-1"},
    )
    assert plugin.claims(span) > 0
    events = list(plugin.decode([span]))
    ttfb = [event for event in events if isinstance(event, StageObserved) and event.metric is Metric.TTFB]
    duration = [event for event in events if isinstance(event, StageObserved) and event.metric is Metric.DURATION]
    assert ttfb and ttfb[0].value_ms == 42.0
    assert duration and duration[0].stage is Stage.STT


def test_openinference_mapper_claims_and_decodes() -> None:
    mapper = ForeignConventionMapper()
    span = ReadableSpan(
        name="ChatCompletion",
        trace_id="c" * 32,
        span_id="d" * 16,
        start_unix_nano=1_000_000_000,
        end_unix_nano=2_000_000_000,
        attributes={"openinference.span.kind": "LLM", "input.value": "hello", "session.id": "sess-1"},
    )
    assert mapper.claims(span) == 20
    events = list(mapper.decode([span]))
    assert any(isinstance(event, StageObserved) and event.placement is MeasurementPlacement.INTERVAL for event in events)


def test_otlp_backpressure_returns_503() -> None:
    inbox = MemoryInbox()
    objects = MemoryObjectStore()
    from obsalt.domain.enums import EnvelopeState, ObservationalEventKind
    from obsalt.plugin.types import RawEnvelope, TombstoneHints
    from obsalt.util import utcnow

    for index in range(3):
        envelope = RawEnvelope(
            envelope_id=f"e{index}",
            org_id="acme",
            provider="otlp",
            connection_id="otlp",
            object_key=f"k{index}",
            delivery_key=f"d{index}",
            content_sha256=f"s{index}",
            state=EnvelopeState.QUEUED,
            event_kind=ObservationalEventKind.OTLP_BATCH,
            received_at=utcnow(),
            body=b"{}",
        )
        inbox.accept(envelope, tombstone_hints=TombstoneHints())
    result = receive_otlp_batch(
        org_id="acme",
        raw=b"{}",
        content_type="application/json",
        objects=objects,
        inbox=inbox,
        backpressure_limit=2,
    )
    assert result.status_code == 503
    assert result.rejected == "outbox backpressure"


def test_otlp_json_forward_does_not_rewrite_bytes() -> None:
    payload = b'{"resourceSpans":[{"pii":"leave-me"}]}'
    result = forward_otlp_batch(
        "http://127.0.0.1:1/v1/traces",
        payload,
        content_type="application/json",
        emit_pii=False,
        allow_http_localhost=True,
        timeout=0.05,
    )
    assert result.identity_preserved is True


def test_promotion_frontier_is_accepted_fact_ids() -> None:
    from datetime import date

    from obsalt.domain.enums import PipelineArchitecture, Signal
    from obsalt.domain.events import CallObserved, StageObserved
    from obsalt.domain.models import FidelityDeclaration

    declaration = FidelityDeclaration(
        source_format="test",
        possible_architectures=frozenset({PipelineArchitecture.CASCADE}),
        possible_placements=frozenset(MeasurementPlacement),
        provides=frozenset({Signal.STT_DURATION}),
        schema_source="test",
        schema_revision="1",
        verified_at=date(2026, 8, 22),
    )
    pointers = MemoryPointerStore()
    sink = MemoryRevisionSink()
    started = datetime(2026, 1, 1, tzinfo=UTC)
    ended = datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)
    revision = process_normalized_events(
        [
            CallObserved(source_call_id="src-1"),
            StageObserved(
                stage=Stage.STT,
                metric=Metric.DURATION,
                value_ms=12,
                placement=MeasurementPlacement.INTERVAL,
                started_at=started,
                ended_at=ended,
                provenance=Provenance.PROVIDER_REPORTED,
            ),
        ],
        org_id="acme",
        source="example",
        source_call_id="src-1",
        envelope_id="env-1",
        declaration=declaration,
        pointers=pointers,
        sink=sink,
        decoder_version="test/1",
    )
    stored = pointers.frontier("acme", revision.call_id)
    assert stored == frozenset(revision.accepted_fact_ids)
    assert stored


def test_quantile_tdigest_insert_sql_excludes_aggregates() -> None:
    client = _RecordingCH()
    store = ClickHouseRollupStore(client)
    revision = CallRevision(
        org_id="acme",
        call_id="c1",
        revision="r1",
        source="example",
        source_call_id="s1",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        stage_measurements=[
            StageMeasurement(
                fact_id="f1",
                stage=Stage.STT,
                metric=Metric.DURATION,
                value_ms=12.0,
                placement=MeasurementPlacement.INTERVAL,
                provenance=Provenance.PROVIDER_REPORTED,
            )
        ],
    )
    store.contribute(revision)
    assert any("quantileTDigestState" in sql for sql, _params in client.commands)
    assert all("aggregate" not in sql.lower() for sql, _params in client.commands)


def test_slo_breached_emitted_when_e2e_exceeds_threshold() -> None:
    state = in_memory_state(Settings(slo_e2e_ms=50))
    state.webhook_destinations.append(
        {
            "id": "d1",
            "org_id": "dev",
            "url": "http://127.0.0.1:1/hooks",
            "secret": mint_whsec(),
            "event_type": "slo.breached",
            "allow_http_localhost": "true",
        }
    )
    revision = CallRevision(
        org_id="dev",
        call_id="c-slo",
        revision="r1",
        source="example",
        source_call_id="slo",
        stage_measurements=[
            StageMeasurement(
                fact_id="e2e",
                stage=Stage.E2E,
                metric=Metric.DURATION,
                value_ms=500.0,
                placement=MeasurementPlacement.UNPLACED,
                provenance=Provenance.PROVIDER_REPORTED,
            )
        ],
    )
    maybe_emit_slo(state, revision)
    assert any(item.get("payload", {}).get("type") == "slo.breached" for item in state.webhook_outbox)


def test_tier2_triggers_tool_failure_and_watched_hangup() -> None:
    failed = CallRevision(
        org_id="o",
        call_id="c-tool",
        revision="r",
        source="example",
        source_call_id="s",
        tools=[ToolInvocation(id="t1", name="lookup", status=ToolStatus.ERROR)],
    )
    execution = decide_tier2(failed)
    assert execution.state is AnalysisState.PENDING
    hung = CallRevision(
        org_id="o",
        call_id="c-hang",
        revision="r",
        source="example",
        source_call_id="s",
        hangup=Hangup(reason=HangupReason.USER_HANGUP),
    )
    assert decide_tier2(hung).state is AnalysisState.PENDING


def test_deletion_sets_completed_at() -> None:
    from obsalt.ops.privacy import apply_deletion

    state = in_memory_state()
    result = apply_deletion(state, org_id="dev", call_id="missing")
    assert result["completed_at"]
    assert state.deletion_completions[-1]["status"] == "completed"


def test_key_rotation_keeps_overlap() -> None:
    settings = Settings()
    state = in_memory_state(settings)
    app = create_app(settings, state)
    client = TestClient(app)
    rotated = client.post("/v1/keys/rotate", headers={"X-API-Key": "dev-key"}, json={"overlap_seconds": 60})
    assert rotated.status_code == 200
    new_key = rotated.json()["key"]
    still_old = client.get("/health")
    assert still_old.status_code == 200
    listed = client.get("/v1/plugins", headers={"X-API-Key": "dev-key"})
    assert listed.status_code == 200
    listed_new = client.get("/v1/plugins", headers={"X-API-Key": new_key})
    assert listed_new.status_code == 200


def test_fair_claim_sql_partitions_by_org() -> None:
    source = Path(PostgresInbox.claim_outbox.__code__.co_filename).read_text()
    assert "PARTITION BY o.org_id" in source
    assert "ROW_NUMBER()" in source


def test_hangup_clusters_persist_payload() -> None:
    client = _RecordingCH()
    store = ClickHouseHangupClusterStore(client)
    revision = CallRevision(
        org_id="acme",
        call_id="c1",
        revision="r1",
        source="example",
        source_call_id="s1",
        hangup=Hangup(reason=HangupReason.USER_HANGUP),
        turns=[],
    )
    store.refresh("acme", [revision], "gen-1")
    assert any(table == "hangup_clusters" for table, _rows, _cols in client.inserts)
