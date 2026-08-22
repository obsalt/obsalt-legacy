"""Promotion, late envelopes, and conflict-blocked assembled marks."""

from __future__ import annotations

from obsalt.assemble.rehydrate import events_from_revision
from obsalt.domain.enums import (
    EnvelopeState,
    MeasurementPlacement,
    Metric,
    Provenance,
    Speaker,
    Stage,
)
from obsalt.domain.events import CallObserved, SnapshotBoundaryObserved, StageObserved, TurnObserved
from obsalt.plugin.types import RawEnvelope, TombstoneHints
from obsalt.util import utcnow
from obsalt.worker.drain import _after_promote
from obsalt.worker.process import process_normalized_events

from tests.helpers import example_state, fidelity_declaration


def test_late_events_fold_onto_previous_revision() -> None:
    state = example_state()
    first = process_normalized_events(
        [
            CallObserved(source_call_id="c1", agent_id="a"),
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
    second = process_normalized_events(
        [
            CallObserved(source_call_id="c1", agent_id="a"),
            TurnObserved(turn_index=1, speaker=Speaker.AGENT, text="hi back"),
        ],
        org_id="acme",
        source="example",
        source_call_id="c1",
        envelope_id="e2",
        declaration=fidelity_declaration(),
        pointers=state.pointers,
        sink=state.sink,
        decoder_version="t/1",
    )
    assert first.revision != second.revision
    assert state.sink.get("acme", first.call_id, first.revision) is not None
    assert {turn.text for turn in second.turns} == {"hello", "hi back"}
    assert state.pointers.get("acme", first.call_id) == second.revision


def test_rehydrate_does_not_emit_snapshot() -> None:
    state = example_state()
    rev = process_normalized_events(
        [
            CallObserved(source_call_id="c1"),
            SnapshotBoundaryObserved(authoritative_domains=["turn_observed"]),
            TurnObserved(turn_index=0, speaker=Speaker.USER, text="hi"),
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
    rebuilt = events_from_revision(rev)
    assert not any(event.type == "snapshot_boundary" for event in rebuilt)


def test_promotion_frontier_is_accepted_fact_ids() -> None:
    state = example_state()
    revision = process_normalized_events(
        [
            CallObserved(source_call_id="src-1"),
            StageObserved(
                stage=Stage.STT,
                metric=Metric.DURATION,
                value_ms=12,
                placement=MeasurementPlacement.INTERVAL,
                started_at=utcnow(),
                ended_at=utcnow(),
                provenance=Provenance.PROVIDER_REPORTED,
                source_path="stt",
            ),
        ],
        org_id="acme",
        source="example",
        source_call_id="src-1",
        envelope_id="env-1",
        declaration=fidelity_declaration(),
        pointers=state.pointers,
        sink=state.sink,
        decoder_version="test/1",
    )
    stored = state.pointers.frontier("acme", revision.call_id)
    assert stored == frozenset(revision.accepted_fact_ids)
    assert stored


def test_successful_promote_with_turns_marks_assembled() -> None:
    state = example_state()
    rev = process_normalized_events(
        [
            CallObserved(source_call_id="c1"),
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
    envelope = RawEnvelope(
        envelope_id="e1",
        org_id="acme",
        provider="example",
        connection_id="c1",
        object_key="k",
        delivery_key="e1",
        content_sha256="x",
        state=EnvelopeState.QUEUED,
        received_at=utcnow(),
    )
    state.inbox.by_id[envelope.envelope_id] = envelope
    state.inbox.outbox.append(envelope.envelope_id)
    _after_promote(state, envelope, rev, TombstoneHints(source_call_id="c1"))
    assert envelope.state is EnvelopeState.ASSEMBLED
    assert envelope.envelope_id not in state.inbox.failures


def test_conflicts_block_assembled_mark() -> None:
    state = example_state()
    first = process_normalized_events(
        [
            CallObserved(source_call_id="c1", agent_id="support"),
            StageObserved(
                stage=Stage.STT,
                metric=Metric.DURATION,
                value_ms=10,
                placement=MeasurementPlacement.UNPLACED,
                provenance=Provenance.PROVIDER_REPORTED,
                source_path="latency.stt",
            ),
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
    conflicted = process_normalized_events(
        [
            StageObserved(
                stage=Stage.STT,
                metric=Metric.DURATION,
                value_ms=99,
                placement=MeasurementPlacement.UNPLACED,
                provenance=Provenance.PROVIDER_REPORTED,
                source_path="latency.stt",
            ),
        ],
        org_id="acme",
        source="example",
        source_call_id="c1",
        envelope_id="e2",
        declaration=fidelity_declaration(),
        pointers=state.pointers,
        sink=state.sink,
        decoder_version="t/1",
    )
    assert conflicted.conflicts, "disagreeing StageObserved values must be a fact conflict"
    envelope = RawEnvelope(
        envelope_id="e2",
        org_id="acme",
        provider="example",
        connection_id="c1",
        object_key="k",
        delivery_key="e2",
        content_sha256="x",
        state=EnvelopeState.QUEUED,
        received_at=utcnow(),
    )
    state.inbox.by_id[envelope.envelope_id] = envelope
    state.inbox.outbox.append(envelope.envelope_id)
    _after_promote(state, envelope, conflicted, TombstoneHints(source_call_id="c1"))
    assert envelope.state is not EnvelopeState.ASSEMBLED
    assert envelope.envelope_id in state.inbox.failures
    assert state.pointers.get("acme", first.call_id) == first.revision


def test_evidence_blobs_are_org_namespaced() -> None:
    state = example_state()
    process_normalized_events(
        [
            CallObserved(source_call_id="c1"),
            TurnObserved(turn_index=0, speaker=Speaker.USER, text="refund please"),
        ],
        org_id="acme",
        source="example",
        source_call_id="c1",
        envelope_id="e1",
        declaration=fidelity_declaration(),
        pointers=state.pointers,
        sink=state.sink,
        decoder_version="t/1",
        objects=state.objects,
    )
    keys = list(state.objects.blobs)
    assert keys
    assert all(key.startswith("org/acme/evidence/") for key in keys)
