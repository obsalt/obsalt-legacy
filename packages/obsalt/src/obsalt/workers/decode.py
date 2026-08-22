"""Decode worker: raw envelope -> stamp -> redact -> assemble candidate.

Promotion (CH write + PG CAS) is a separate step so crash-point tests can
stop between stages.
"""

from __future__ import annotations

from collections.abc import Iterable
from uuid import uuid4

from obsalt.assemble.assembler import fold_events, stamp_events
from obsalt.domain.events import CallObserved, NormalizedEvent
from obsalt.domain.models import CallRevision
from obsalt.plugin.protocol import FidelityDeclaration, RawEnvelope, RedactionPolicy
from obsalt.redact.default import DefaultRedactor


def decode_envelope(
    envelope: RawEnvelope,
    *,
    plugin: object,
    declaration: FidelityDeclaration | None,
    source_call_id: str | None = None,
    redactor: DefaultRedactor | None = None,
    policy: RedactionPolicy | None = None,
    processing_run_id: str | None = None,
    revision: int = 1,
) -> CallRevision:
    raw_events = list(plugin.decode(envelope))  # type: ignore[attr-defined]
    run_id = processing_run_id or str(uuid4())
    stamped = stamp_events(
        raw_events,
        org_id=envelope.org_id,
        source=envelope.provider,
        envelope_id=envelope.envelope_id,
        decoder_version=getattr(plugin, "DECODER_VERSION", envelope.provider + "/1"),
        processing_run_id=run_id,
    )
    redacted = (redactor or DefaultRedactor()).redact(stamped, policy or RedactionPolicy())
    call_id = source_call_id or _infer_source_call_id(redacted.events)
    if not call_id:
        raise ValueError("decoder produced no CallObserved.source_call_id")
    return fold_events(
        redacted.events,
        org_id=envelope.org_id,
        source=envelope.provider,
        source_call_id=call_id,
        processing_run_id=run_id,
        revision=revision,
        declaration=declaration,
        decoder_version=getattr(plugin, "DECODER_VERSION", envelope.provider + "/1"),
    )


def _infer_source_call_id(events: Iterable[NormalizedEvent]) -> str | None:
    for event in events:
        if isinstance(event, CallObserved):
            return event.source_call_id
    return None
