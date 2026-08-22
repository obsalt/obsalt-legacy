from __future__ import annotations

from obsalt.domain.events import EventBase, NormalizedEvent
from obsalt.util import canonical_json, sha256_text, utcnow

ASSEMBLER_VERSION = "1"


def fact_id_for(event: EventBase) -> str:
    return sha256_text(canonical_json(event.identity_parts()))


def stamp_event(
    event: NormalizedEvent,
    *,
    org_id: str,
    call_key: str,
    envelope_id: str,
    decoder_version: str,
    processing_run_id: str,
    envelope_sequence: int,
) -> NormalizedEvent:
    data = event.model_dump()
    data.update(
        {
            "org_id": org_id,
            "call_key": call_key,
            "envelope_id": envelope_id,
            "decoder_version": decoder_version,
            "processing_run_id": processing_run_id,
            "envelope_sequence": envelope_sequence,
            "event_occurred_at": data.get("event_occurred_at") or utcnow(),
        }
    )
    stamped = event.__class__.model_validate(data)
    stamped.fact_id = fact_id_for(stamped)
    return stamped
