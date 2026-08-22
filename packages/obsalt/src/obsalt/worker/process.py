"""Decode → redact → assemble → promote. Runs in a worker, never on the webhook path."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from obsalt.analysis.hallucination import extract_candidate_claims
from obsalt.analysis.hangup import classify_provider_reason
from obsalt.analysis.tier1 import analyze_tier1
from obsalt.assemble.assembler import Assembler
from obsalt.assemble.facts import stamp_event
from obsalt.assemble.promote import RevisionPointerStore, promote
from obsalt.domain.enums import EnvelopeState
from obsalt.domain.events import CallObserved, NormalizedEvent, OutcomeObserved
from obsalt.domain.models import AnalysisResult, CallRevision, FidelityDeclaration
from obsalt.plugin.contract import WebhookSource
from obsalt.plugin.types import RawEnvelope
from obsalt.redact.choke import redact_events
from obsalt.util import call_id_for, new_id


class RevisionSink:
    def write(self, revision: CallRevision) -> None:
        raise NotImplementedError

    def get(self, org_id: str, call_id: str, revision: str) -> CallRevision | None:
        raise NotImplementedError

    def delete_call(self, org_id: str, call_id: str) -> None:
        raise NotImplementedError


class MemoryRevisionSink(RevisionSink):
    def __init__(self) -> None:
        self.revisions: dict[tuple[str, str, str], CallRevision] = {}
        self.analysis: dict[tuple[str, str, str], list[AnalysisResult]] = {}

    def write(self, revision: CallRevision) -> None:
        self.revisions[(revision.org_id, revision.call_id, revision.revision)] = revision

    def get(self, org_id: str, call_id: str, revision: str) -> CallRevision | None:
        return self.revisions.get((org_id, call_id, revision))

    def list_for_call(self, org_id: str, call_id: str) -> list[CallRevision]:
        return [r for (o, c, _), r in self.revisions.items() if o == org_id and c == call_id]

    def delete_call(self, org_id: str, call_id: str) -> None:
        for key in [k for k in self.revisions if k[0] == org_id and k[1] == call_id]:
            self.revisions.pop(key, None)
            self.analysis.pop(key, None)

    def write_analysis(self, org_id: str, call_id: str, revision: str, results: list[AnalysisResult]) -> None:
        self.analysis[(org_id, call_id, revision)] = results


def process_envelope(
    envelope: RawEnvelope,
    plugin: WebhookSource,
    *,
    declaration: FidelityDeclaration,
    pointers: RevisionPointerStore,
    sink: RevisionSink,
    decoder_version: str,
    source: str,
) -> CallRevision:
    if envelope.state is EnvelopeState.TOMBSTONED:
        raise RuntimeError("refusing to decode a tombstoned envelope")
    events = list(plugin.decode(envelope))
    return process_normalized_events(
        events,
        org_id=envelope.org_id,
        source=source,
        source_call_id=envelope.source_call_id,
        envelope_id=envelope.envelope_id,
        declaration=declaration,
        pointers=pointers,
        sink=sink,
        decoder_version=decoder_version,
    )


def process_normalized_events(
    events: Sequence[NormalizedEvent],
    *,
    org_id: str,
    source: str,
    source_call_id: str | None,
    envelope_id: str,
    declaration: FidelityDeclaration,
    pointers: RevisionPointerStore,
    sink: RevisionSink,
    decoder_version: str,
) -> CallRevision:
    run_id = new_id()
    resolved_source = source_call_id or _source_call_id(events) or envelope_id
    call_id = call_id_for(org_id, source, resolved_source)
    stamped: list[NormalizedEvent] = []
    for index, event in enumerate(events):
        if isinstance(event, OutcomeObserved) and event.reason is None:
            reason, party = classify_provider_reason(source, event.provider_code)
            event = event.model_copy(update={"reason": reason, "party": party})
        stamped.append(
            stamp_event(
                event,
                org_id=org_id,
                call_key=call_id,
                envelope_id=envelope_id,
                decoder_version=decoder_version,
                processing_run_id=run_id,
                envelope_sequence=index,
            )
        )
    redacted = redact_events(stamped)
    assembler = Assembler(declaration, decoder_version=decoder_version, processing_run_id=run_id)
    expected = pointers.get(org_id, call_id)
    candidate = assembler.assemble(org_id, call_id, source, list(redacted.events))
    frontier = frozenset(e.fact_id for e in redacted.events if e.fact_id)
    analysis: list[AnalysisResult] = []
    if not candidate.conflicts:
        analysis = list(analyze_tier1(candidate))
        claims = extract_candidate_claims(candidate)
        if claims:
            from obsalt.domain.enums import AnalysisState
            from obsalt.domain.models import AnalysisExecution

            analysis.append(
                AnalysisResult(
                    execution=AnalysisExecution(
                        call_id=candidate.call_id,
                        revision=candidate.revision,
                        analyzer_id="hallucination",
                        analyzer_version="1",
                        state=AnalysisState.COMPLETED,
                    ),
                    payload={"candidates": claims},
                )
            )
    sink.write(candidate)
    result = promote(pointers, candidate, expected=expected, fact_frontier=frontier)
    if result.promoted:
        writer = getattr(sink, "write_analysis", None)
        if writer is not None:
            writer(org_id, call_id, candidate.revision, analysis)
    return candidate


def drain_inbox(state: object, *, limit: int = 32) -> int:
    """Claim outbox work and assemble. Safe to call after the webhook ack is sent."""
    from obsalt.worker.drain import drain_once

    return drain_once(state, limit=limit)


def _source_call_id(events: Iterable[NormalizedEvent]) -> str | None:
    for event in events:
        if isinstance(event, CallObserved):
            return event.source_call_id
    return None
