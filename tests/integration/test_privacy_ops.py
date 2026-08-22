"""Deletion, retention sweep, backfill tombstones, and export."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from obsalt.api import create_app
from obsalt.config import Settings
from obsalt.domain.enums import EnvelopeState, Speaker
from obsalt.domain.events import CallObserved, TurnObserved
from obsalt.ops.backfill import run_backfill
from obsalt.ops.parquet import export_revisions
from obsalt.ops.privacy import apply_deletion
from obsalt.ops.retention import replay_horizon, sweep_raw
from obsalt.ops.schema_drift import compare_vendored
from obsalt.plugin.types import RawEnvelope, TombstoneHints
from obsalt.runtime import in_memory_state
from obsalt.util import utcnow
from obsalt.worker.process import process_normalized_events
from obsalt_testkit.schema import FixtureSuite, blocking_errors, validate_raw_fixtures
from tests.helpers import EXAMPLE_FIXTURES, example_state, fidelity_declaration


def test_delete_by_caller_and_range() -> None:
    state = example_state()
    process_normalized_events(
        [
            CallObserved(
                source_call_id="c1",
                from_number="+15551212",
                started_at=datetime(2026, 1, 2, tzinfo=UTC),
            ),
            TurnObserved(turn_index=0, speaker=Speaker.USER, text="hello"),
        ],
        org_id="acme",
        source="example",
        source_call_id="c1",
        envelope_id="e1",
        declaration=fidelity_declaration(),
        pointers=state.pointers,
        sink=state.sink,
        decoder_version="t/1",
    )
    keep = process_normalized_events(
        [
            CallObserved(
                source_call_id="c2",
                from_number="+15550000",
                started_at=datetime(2026, 6, 1, tzinfo=UTC),
            ),
            TurnObserved(turn_index=0, speaker=Speaker.USER, text="later"),
        ],
        org_id="acme",
        source="example",
        source_call_id="c2",
        envelope_id="e2",
        declaration=fidelity_declaration(),
        pointers=state.pointers,
        sink=state.sink,
        decoder_version="t/1",
    )
    ranged = apply_deletion(
        state,
        org_id="acme",
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 2, 1, tzinfo=UTC),
    )
    assert ranged["undoable"] is False
    assert keep.call_id not in ranged["deleted_calls"]
    result = apply_deletion(state, org_id="acme", caller="+15550000")
    assert result["deleted_calls"]
    client = TestClient(create_app(Settings(environment="test"), state))
    listed = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z",
        headers={"X-API-Key": "k"},
    )
    assert listed.json()["items"] == []


def test_deletion_sets_completed_at() -> None:
    state = in_memory_state()
    result = apply_deletion(state, org_id="dev", call_id="missing")
    assert result["completed_at"]
    assert state.deletion_completions[-1]["status"] == "completed"


def test_replay_horizon_and_raw_sweep() -> None:
    state = example_state()
    key = "org/acme/raw/example/old/abc"
    state.objects.put(key, b"{}", content_type="application/json")
    envelope = RawEnvelope(
        envelope_id="old",
        org_id="acme",
        provider="example",
        connection_id="c1",
        object_key=key,
        delivery_key="old",
        content_sha256="x",
        state=EnvelopeState.ASSEMBLED,
        received_at=utcnow() - timedelta(days=40),
        body=b"{}",
    )
    state.inbox.by_id[envelope.envelope_id] = envelope
    report = sweep_raw(state, raw_retention_days=30)
    assert report["purged_blobs"] == 1
    assert key not in state.objects.blobs
    horizon = replay_horizon(raw_retention_days=30)
    assert "replayable_after" in horizon


def test_parquet_export_writes_manifest(tmp_path: Path) -> None:
    state = example_state()
    rev = process_normalized_events(
        [CallObserved(source_call_id="c1")],
        org_id="acme",
        source="example",
        source_call_id="c1",
        envelope_id="e1",
        declaration=fidelity_declaration(),
        pointers=state.pointers,
        sink=state.sink,
        decoder_version="t/1",
    )
    dest = tmp_path / "export"
    manifest = export_revisions([rev], dest, as_of_generation="g1")
    assert (dest / "manifest.json").exists()
    assert manifest["call_count"] == 1
    assert "cannot revoke" in manifest["note"]


def test_backfill_runner_respects_tombstones() -> None:
    state = example_state()
    for cfg in state.resolver.connections.values():
        if cfg.provider == "example":
            cfg.settings["backfill_items"] = [{"id": "u1", "hash": "abc"}]
    state.inbox.tombstone("acme", TombstoneHints(source_call_id="u1"))
    report = run_backfill(state, org_id="acme", provider="example")
    assert report["status"] == "queued"
    assert report["envelopes"] == 0


def test_schema_fixture_harness_and_offline_drift() -> None:
    messages = validate_raw_fixtures(FixtureSuite(EXAMPLE_FIXTURES))
    assert blocking_errors(messages) == []
    report = compare_vendored(EXAMPLE_FIXTURES)
    assert report["status"] == "offline"
    assert report["diverged"] is False
