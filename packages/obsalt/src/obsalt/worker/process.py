"""Decode → redact → assemble → promote. Runs in a worker, never on the webhook path."""

from __future__ import annotations

from collections.abc import Iterable

from obsalt.analysis.hangup import classify_provider_reason
from obsalt.analysis.tier1 import analyze_tier1
from obsalt.assemble.assembler import Assembler
from obsalt.assemble.facts import stamp_event
from obsalt.assemble.promote import RevisionPointerStore, promote
from obsalt.domain.events import CallObserved, NormalizedEvent, OutcomeObserved
from obsalt.domain.models import CallRevision, FidelityDeclaration
from obsalt.plugin.contract import WebhookSource
from obsalt.plugin.types import RawEnvelope
from obsalt.redact.choke import redact_events
from obsalt.util import call_id_for, new_id


class RevisionSink:
    def write(self, revision: CallRevision) -> None:
        raise NotImplementedError

    def get(self, org_id: str, call_id: str, revision: str) -> CallRevision | None:
        raise NotImplementedError


class MemoryRevisionSink(RevisionSink):
    def __init__(self) -> None:
        self.revisions: dict[tuple[str, str, str], CallRevision] = {}

    def write(self, revision: CallRevision) -> None:
        self.revisions[(revision.org_id, revision.call_id, revision.revision)] = revision

    def get(self, org_id: str, call_id: str, revision: str) -> CallRevision | None:
        return self.revisions.get((org_id, call_id, revision))

    def list_for_call(self, org_id: str, call_id: str) -> list[CallRevision]:
        return [r for (o, c, _), r in self.revisions.items() if o == org_id and c == call_id]


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
    events = list(plugin.decode(envelope))
    run_id = new_id()
    source_call_id = envelope.source_call_id or _source_call_id(events) or envelope.envelope_id
    call_id = call_id_for(envelope.org_id, source, source_call_id)
    stamped: list[NormalizedEvent] = []
    for index, event in enumerate(events):
        if isinstance(event, OutcomeObserved) and event.reason is None:
            reason, party = classify_provider_reason(source, event.provider_code)
            event = event.model_copy(update={"reason": reason, "party": party})
        stamped.append(
            stamp_event(
                event,
                org_id=envelope.org_id,
                call_key=call_id,
                envelope_id=envelope.envelope_id,
                decoder_version=decoder_version,
                processing_run_id=run_id,
                envelope_sequence=index,
            )
        )
    redacted = redact_events(stamped)
    assembler = Assembler(declaration, decoder_version=decoder_version, processing_run_id=run_id)
    expected = pointers.get(envelope.org_id, call_id)
    candidate = assembler.assemble(envelope.org_id, call_id, source, list(redacted.events))
    sink.write(candidate)
    frontier = frozenset(e.fact_id for e in redacted.events if e.fact_id)
    result = promote(pointers, candidate, expected=expected, fact_frontier=frontier)
    if result.promoted:
        analyze_tier1(candidate)
    return candidate


def _source_call_id(events: Iterable[NormalizedEvent]) -> str | None:
    for event in events:
        if isinstance(event, CallObserved):
            return event.source_call_id
    return None
