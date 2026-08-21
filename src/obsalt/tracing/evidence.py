from __future__ import annotations

from hashlib import sha256

from obsalt.domain.models import CanonicalCall


def transcript_artifact_id(call: CanonicalCall) -> str:
    body = call.transcript_text or "\n".join(f"{t.speaker.value}:{t.text}" for t in call.turns)
    return "tr_" + sha256(body.encode("utf-8")).hexdigest()[:16]


def recording_artifact_id(call: CanonicalCall) -> str | None:
    if not call.recording_url:
        return None
    return "rec_" + sha256(call.recording_url.encode("utf-8")).hexdigest()[:16]
