"""Medium tests: ops contracts that span health, backup, and unrooted traces."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from obsalt.api import create_test_app
from obsalt.config import Settings
from obsalt.ops.backup import expire_backups, record_backup, restore_allowed
from obsalt.ops.health import collect_health
from obsalt.ops.privacy import apply_deletion
from obsalt.otel.attributes import leftover_attributes
from obsalt.plugin.host import invoke_with_deadline
from obsalt.plugin.types import ReadableSpan, TombstoneHints
from obsalt.runtime import add_org_spend, in_memory_state, org_spend_usd
from obsalt.util import utcnow


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


def test_ready_reports_health_signals() -> None:
    settings = Settings(environment="test")
    state = in_memory_state(settings)
    client = TestClient(create_test_app(settings, state))
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
    state = in_memory_state()
    add_org_spend(state, "dev", 1.25)
    add_org_spend(state, "other", 9.0)
    assert org_spend_usd(state, "dev") == 1.25
    assert org_spend_usd(state, "other") == 9.0
