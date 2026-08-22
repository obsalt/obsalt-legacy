"""Authz, worker isolation, range tombstones, and health contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from obsalt.analysis.tier1 import analyze_tier1
from obsalt.api import create_app
from obsalt.config import Settings
from obsalt.crypto.primitives import hmac_hex
from obsalt.domain.enums import KeyScope, Role, Speaker
from obsalt.domain.models import CallRevision, Turn
from obsalt.ingest.headers import RawHeaders
from obsalt.ingest.receive import receive_webhook
from obsalt.ops.backup import expire_backups, record_backup, restore_allowed
from obsalt.ops.health import collect_health
from obsalt.ops.privacy import apply_deletion
from obsalt.otel.attributes import leftover_attributes
from obsalt.plugin.host import invoke_with_deadline
from obsalt.plugin.types import ReadableSpan, TombstoneHints
from obsalt.runtime import in_memory_state
from obsalt.security.sessions import ROLE_SCOPES, read_session, sign_session
from obsalt.util import utcnow
from obsalt_example.plugin import ExamplePlugin

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_RAW = ROOT / "packages/obsalt-example/src/obsalt_example/fixtures/raw/call_ended.json"


def _client(state=None) -> tuple[TestClient, object]:
    settings = Settings()
    state = state or in_memory_state(settings)
    return TestClient(create_app(settings, state)), state


def test_reviewer_cannot_rotate_keys_or_replay() -> None:
    client, state = _client()
    state.keys["reviewer"] = ("dev", frozenset({KeyScope.READ}))
    headers = {"X-API-Key": "reviewer"}
    assert client.post("/v1/keys/rotate", headers=headers, json={}).status_code == 403
    assert client.post("/v1/replay", headers=headers, json={}).status_code == 403
    listed = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z",
        headers=headers,
    )
    assert listed.status_code == 200


def test_analyst_can_analyze_admin_cannot_rotate() -> None:
    client, state = _client()
    state.keys["analyst"] = ("dev", frozenset({KeyScope.READ, KeyScope.ANALYZE}))
    state.keys["admin"] = ("dev", ROLE_SCOPES[Role.ADMIN])
    missing = client.post("/v1/calls/no-such-call/analyze", headers={"X-API-Key": "analyst"})
    assert missing.status_code == 404
    state.keys["reviewer"] = ("dev", frozenset({KeyScope.READ}))
    denied = client.post("/v1/calls/no-such-call/analyze", headers={"X-API-Key": "reviewer"})
    assert denied.status_code == 403
    rotated = client.post("/v1/keys/rotate", headers={"X-API-Key": "admin"}, json={})
    assert rotated.status_code == 403
    owner = client.post(
        "/v1/keys/rotate", headers={"X-API-Key": "dev-key"}, json={"overlap_seconds": 30}
    )
    assert owner.status_code == 200


def test_reviewer_session_cannot_replay() -> None:
    client, state = _client()
    token = sign_session("dev", state.settings.session_secret, role=Role.REVIEWER)
    info = read_session(token, state.settings.session_secret)
    assert info is not None
    res = client.post(
        "/v1/ui/replay",
        data={"csrf": info.csrf},
        cookies={"obsalt_session": token},
    )
    assert res.status_code == 403
    analyst = sign_session("dev", state.settings.session_secret, role=Role.ANALYST)
    settings = client.get("/v1/ui/settings", cookies={"obsalt_session": analyst})
    assert settings.status_code == 200


def test_plugin_deadline_times_out() -> None:
    def hang() -> str:
        import time

        time.sleep(1)
        return "done"

    try:
        invoke_with_deadline(hang, timeout_seconds=0.05)
    except TimeoutError as exc:
        assert "exceeded" in str(exc)
    else:
        raise AssertionError("deadline should raise TimeoutError")


def test_range_tombstone_blocks_receive() -> None:
    from obsalt.plugin.host import LoadedPlugin
    from obsalt.plugin.types import ConnectionConfig

    state = in_memory_state()
    if not any(plugin.name == "example" for plugin in state.plugins):
        state.plugins.append(LoadedPlugin(ExamplePlugin()))
        state.resolver.add(
            ConnectionConfig(
                org_id="dev",
                provider="example",
                connection_id="example-dev",
                ingest_key_hash="",
                secrets={"hmac_secret": "dev-secret"},
            ),
            "dev",
        )
    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = datetime(2026, 8, 31, tzinfo=UTC)
    apply_deletion(state, org_id="dev", start=start, end=end)
    mid = datetime(2026, 8, 22, 12, 0, 1, tzinfo=UTC)
    assert state.inbox.is_tombstoned("dev", TombstoneHints(event_time=mid))
    assert not state.inbox.is_tombstoned(
        "dev", TombstoneHints(event_time=datetime(2025, 1, 1, tzinfo=UTC))
    )
    raw = EXAMPLE_RAW.read_bytes()
    plugin = ExamplePlugin()
    result = receive_webhook(
        provider="example",
        ingest_key="dev",
        raw=raw,
        headers=RawHeaders([(b"x-obsalt-example-signature", hmac_hex("dev-secret", raw).encode())]),
        resolver=state.resolver,
        plugin=plugin,
        objects=state.objects,
        inbox=state.inbox,
    )
    assert result.rejected == "tombstoned"
    assert result.envelope is None


def test_dead_air_and_truncated_llm_flags() -> None:
    t0 = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    revision = CallRevision(
        org_id="o",
        call_id="c",
        revision="r",
        source="example",
        source_call_id="s",
        turns=[
            Turn(
                index=0,
                speaker=Speaker.USER,
                text="hello",
                started_at=t0,
                ended_at=t0 + timedelta(seconds=1),
            ),
            Turn(
                index=1,
                speaker=Speaker.AGENT,
                text="I can look that up...",
                started_at=t0 + timedelta(seconds=20),
                ended_at=t0 + timedelta(seconds=21),
            ),
        ],
    )
    flags = next(row for row in analyze_tier1(revision) if row.execution.analyzer_id == "flags")
    kinds = {item["kind"] for item in flags.payload["flags"]}
    assert "dead_air" in kinds
    assert "truncated_llm" in kinds
    length = CallRevision(
        org_id="o",
        call_id="c2",
        revision="r",
        source="example",
        source_call_id="s2",
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="complete answer")],
        unmapped_attributes={"finish_reason": "length"},
    )
    kinds = {
        item["kind"]
        for item in next(
            row for row in analyze_tier1(length) if row.execution.analyzer_id == "flags"
        ).payload["flags"]
    }
    assert "truncated_llm" in kinds


def test_backup_expiry_blocks_restore() -> None:
    state = in_memory_state()
    item = record_backup(state, taken_at=utcnow() - timedelta(days=40), retention_days=30)
    expired = expire_backups(state)
    assert expired["expired"] == 1
    blocked = restore_allowed(state, org_id="dev", source_call_id=None, backup_id=item["id"])
    assert blocked["allowed"] is False
    assert blocked["reason"] == "backup expired"
    state.inbox.tombstone("dev", TombstoneHints(source_call_id="gone"))
    tombstoned = restore_allowed(state, org_id="dev", source_call_id="gone", backup_id=item["id"])
    assert tombstoned["reason"] == "deletion cannot be undone"


def test_ready_reports_health_signals() -> None:
    client, state = _client()
    state.objects.put("org/dev/raw/example/orphan/sha", b"{}", content_type="application/json")
    body = client.get("/ready").json()
    assert body["status"] == "ready"
    assert "inbox_age_seconds" in body
    assert "outbox_depth" in body
    assert "dlq_depth" in body
    assert "orphan_blobs" in body
    assert "deletion_backlog" in body
    signals = collect_health(state)
    assert signals["orphan_blobs"] >= 1


def test_leftover_attributes_land_in_catchall() -> None:
    spans = [
        ReadableSpan(
            name="turn",
            trace_id="aa" * 16,
            span_id="bb" * 8,
            start_unix_nano=1,
            end_unix_nano=2,
            attributes={"custom.foo": "bar", "gen_ai.request.model": "gpt"},
        )
    ]
    leftover = leftover_attributes(spans)
    assert leftover["custom.foo"] == "bar"
    assert "gen_ai.request.model" not in leftover


def test_users_crud_is_owner_only() -> None:
    client, state = _client()
    state.keys["admin"] = ("dev", ROLE_SCOPES[Role.ADMIN])
    denied = client.post(
        "/v1/users",
        headers={"X-API-Key": "admin"},
        json={"email": "analyst@acme.test", "role": "analyst"},
    )
    assert denied.status_code == 403
    created = client.post(
        "/v1/users",
        headers={"X-API-Key": "dev-key"},
        json={"email": "analyst@acme.test", "role": "analyst"},
    )
    assert created.status_code == 200
    assert created.json()["role"] == "analyst"
    listed = client.get("/v1/users", headers={"X-API-Key": "admin"})
    assert listed.status_code == 200
    emails = {item["email"] for item in listed.json()["items"]}
    assert "analyst@acme.test" in emails
    user_id = created.json()["id"]
    reviewer = client.delete(f"/v1/users/{user_id}", headers={"X-API-Key": "admin"})
    assert reviewer.status_code == 403
    deleted = client.delete(f"/v1/users/{user_id}", headers={"X-API-Key": "dev-key"})
    assert deleted.status_code == 200


def test_cli_exposes_worker() -> None:
    from obsalt.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["worker", "--once"])
    assert args.command == "worker"
    assert args.once is True


def test_search_requires_time_range() -> None:
    client, _state = _client()
    missing = client.post("/v1/search", headers={"X-API-Key": "dev-key"}, json={"q": "refund"})
    assert missing.status_code == 400
    ok = client.post(
        "/v1/search",
        headers={"X-API-Key": "dev-key"},
        json={"q": "refund", "start": "2020-01-01T00:00:00Z", "end": "2030-01-01T00:00:00Z"},
    )
    assert ok.status_code == 200


def test_unrooted_revision_drops_root_owned_fields() -> None:
    from datetime import date

    from obsalt.assemble.assembler import Assembler
    from obsalt.domain.enums import (
        CallStatus,
        HangupParty,
        HangupReason,
        MeasurementPlacement,
        PipelineArchitecture,
        Signal,
    )
    from obsalt.domain.events import CallObserved, OutcomeObserved
    from obsalt.domain.models import FidelityDeclaration

    declaration = FidelityDeclaration(
        source_format="test",
        possible_architectures=frozenset({PipelineArchitecture.CASCADE}),
        possible_placements=frozenset(MeasurementPlacement),
        provides=frozenset({Signal.TURN_INTERVAL}),
        structurally_absent={},
        schema_source="test",
        schema_revision="1",
        verified_at=date(2026, 8, 22),
    )
    assembler = Assembler(declaration, decoder_version="t/1", processing_run_id="r")
    ended = datetime(2026, 8, 22, 12, 5, tzinfo=UTC)
    revision = assembler.assemble(
        "acme",
        "c1",
        "example",
        [
            CallObserved(source_call_id="s1", agent_id="support", ended_at=ended, cost=1.5),
            OutcomeObserved(
                provider_code="user_hangup",
                reason=HangupReason.USER_HANGUP,
                party=HangupParty.USER,
                ended_at=ended,
            ),
        ],
        rooted=False,
    )
    assert revision.status is CallStatus.UNROOTED
    assert revision.ended_at is None
    assert revision.hangup is None
    assert revision.cost is None
    assert revision.agent_id == "unknown"


def test_deletion_purges_queues() -> None:
    from obsalt.domain.enums import EnvelopeState
    from obsalt.otel.forward_queue import ForwardJob
    from obsalt.plugin.types import RawEnvelope

    state = in_memory_state()
    envelope = RawEnvelope(
        envelope_id="e-del",
        org_id="dev",
        provider="example",
        connection_id="c1",
        object_key="org/dev/raw/example/gone/sha",
        delivery_key="d-del",
        content_sha256="x",
        source_call_id="gone",
    )
    state.inbox.accept(envelope, tombstone_hints=TombstoneHints())
    state.inbox.dlq.append({"envelope_id": "e-del", "error": "x"})
    state.webhook_outbox.append(
        {"org_id": "dev", "call_id": "gone", "event_type": "call.finalized"}
    )
    state.forward_queue.enqueue(
        ForwardJob(
            job_id="j1",
            org_id="dev",
            object_key="org/dev/raw/otlp/gone",
            content_type="application/json",
            raw=b"{}",
        )
    )
    apply_deletion(state, org_id="dev", call_id="gone", source_call_id="gone")
    assert envelope.state is EnvelopeState.TOMBSTONED
    assert envelope.envelope_id not in state.inbox.outbox
    assert not any(row.get("envelope_id") == "e-del" for row in state.inbox.dlq)
    assert not any(item.get("call_id") == "gone" for item in state.webhook_outbox)
    assert not any(getattr(job, "job_id", None) == "j1" for job in state.forward_queue.pending)


def test_spend_is_per_org() -> None:
    from obsalt.runtime import add_org_spend, org_spend_usd

    state = in_memory_state()
    add_org_spend(state, "dev", 1.25)
    add_org_spend(state, "other", 9.0)
    assert org_spend_usd(state, "dev") == 1.25
    assert org_spend_usd(state, "other") == 9.0
