"""Shared OTLP span clock and stage-semantics checks.

T1: INTERVAL measurements require a real start/end. Leftover HTTP or internal
spans must not be mapped to E2E just because they have timestamps.
"""

from __future__ import annotations

from obsalt.domain.enums import Stage


def valid_span_interval(span: object) -> bool:
    """True when both unix-nano clocks are present, positive, and ordered."""

    start = getattr(span, "start_unix_nano", 0) or 0
    end = getattr(span, "end_unix_nano", 0) or 0
    try:
        start_i = int(start)
        end_i = int(end)
    except (TypeError, ValueError):
        return False
    return start_i > 0 and end_i > start_i


def stage_from_span_semantics(name: str, attrs: dict[str, object] | None = None) -> Stage | None:
    """Map a span to a stage only when its *name* (or a documented kind) is recognizable.

    Attribute *keys* are ignored: ``elevenlabs.conversation_id`` on an HTTP span
    must not mint an invented E2E interval. Returns None for leftovers.
    """

    n = (name or "").lower()
    attrs = attrs or {}
    kind = str(attrs.get("openinference.span.kind") or "").upper()
    if any(token in n for token in ("stt", "transcri", "asr", "speech_to_text")):
        return Stage.STT
    if any(token in n for token in ("tts", "synthe", "ttfa")):
        return Stage.TTS
    if any(token in n for token in ("llm", "inference", "chat", "generate", "completion")):
        return Stage.LLM
    if any(token in n for token in ("tool", "function")):
        return Stage.TOOL
    if any(token in n for token in ("vad", "endpoint", "eou")):
        return Stage.ENDPOINTING
    if n in {"user_input", "speech.user_input"} or n.startswith("user_input"):
        return Stage.USER_INPUT
    if n in {"generation", "speech.generation"}:
        return Stage.GENERATION
    if "playout" in n:
        return Stage.PLAYOUT
    if any(
        token in n
        for token in (
            "conversation",
            "call.lifecycle",
            "session",
            "e2e",
            "end_to_end",
            "room.duration",
        )
    ):
        return Stage.E2E
    if n == "call" or n.startswith("call."):
        return Stage.E2E
    if kind == "LLM":
        return Stage.LLM
    if kind == "TOOL":
        return Stage.TOOL
    return None
