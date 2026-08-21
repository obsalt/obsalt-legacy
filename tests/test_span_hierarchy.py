from __future__ import annotations

import re

from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from obsalt.tracing import conventions as c
from obsalt.tracing.setup import setup_tracing
from obsalt.tracing.tracer import VoiceCallTracer
from tests.span_helpers import attrs, span_forest


def _setup():
    exporter = InMemorySpanExporter()
    reader = InMemoryMetricReader()
    setup_tracing(span_exporter=exporter, metric_reader=reader, batch=False)
    return exporter, reader


def test_live_span_hierarchy_nests_provider_fallback_and_tools() -> None:
    exporter, _ = _setup()
    with VoiceCallTracer.start(call_id="c1", workspace_id="acme", agent_id="support") as call:
        with call.turn(0) as turn:
            with turn.stt("deepgram") as stt:
                stt.set(confidence=0.4, latency_ms=800)
                with stt.provider_attempt("deepgram") as attempt:
                    attempt.fail("timeout", "deadline exceeded")
                with stt.provider_attempt("azure", fallback=True) as attempt:
                    attempt.set(**{c.STT_LATENCY_MS: 400, c.STT_CONFIDENCE: 0.91})
            with turn.llm("gpt-4o", provider="openai") as llm:
                llm.set(ttft_ms=340, tokens_in=100, tokens_out=40, finish_reason="stop")
                with llm.tool("check_inventory", call_id="tool-1") as tool:
                    tool.set(execution_ms=350, status_code=200)
            with turn.tts("elevenlabs") as tts:
                tts.set(synthesis_ms=290, first_audio_ms=70)
            with turn.playout():
                pass
        with call.finalize_transcript("written"):
            pass
        with call.evaluate("grounded-claims") as ev:
            ev.set(**{c.ASSERTION_RESULT: "pass", c.ASSERTION_SCORE: 1.0})
        call.set_call_outcome(duration_ms=45_000, status="ended")

    spans = exporter.get_finished_spans()
    names = {s.name for s in spans}
    assert c.SPAN_CALL in names
    assert "turn.0" in names
    assert c.SPAN_STT in names
    assert "stt.provider.deepgram" in names
    assert "stt.provider.fallback.azure" in names
    assert c.SPAN_LLM in names
    assert "llm.tool_call.check_inventory" in names
    assert c.SPAN_TTS in names
    assert c.SPAN_PLAYOUT in names
    assert c.SPAN_TRANSCRIPT_FINAL in names
    assert c.SPAN_EVAL in names

    forest = span_forest(list(spans))
    assert forest["__roots__"] == [c.SPAN_CALL]
    assert "turn.0" in forest[c.SPAN_CALL]
    assert "stt.provider.deepgram" in forest[c.SPAN_STT]
    assert "stt.provider.fallback.azure" in forest[c.SPAN_STT]
    assert "llm.tool_call.check_inventory" in forest[c.SPAN_LLM]

    root = next(s for s in spans if s.name == c.SPAN_CALL)
    for span in spans:
        a = attrs(span)
        for key in c.JOIN_KEYS:
            assert key in a, f"{span.name} missing {key}"
        assert a[c.CALL_ID] == "c1"
        assert a[c.WORKSPACE_ID] == "acme"
        assert a[c.AGENT_ID] == "support"
    assert attrs(root)[c.CALL_DURATION_MS] == 45_000

    llm = next(s for s in spans if s.name == c.SPAN_LLM)
    la = attrs(llm)
    assert la[c.GENAI_OPERATION] == "chat"
    assert la[c.GENAI_REQUEST_MODEL] == "gpt-4o"
    assert la[c.LLM_TTFT_MS] == 340
    assert la[c.GENAI_USAGE_IN] == 100

    tool = next(s for s in spans if s.name.startswith("llm.tool_call."))
    ta = attrs(tool)
    assert ta[c.GENAI_OPERATION] == "execute_tool"
    assert ta[c.GENAI_TOOL_NAME] == "check_inventory"
    assert ta[c.TOOL_EXECUTION_MS] == 350

    deepgram = next(s for s in spans if s.name == "stt.provider.deepgram")
    assert deepgram.status.status_code.name == "ERROR"


def test_spans_are_reviewer_safe_by_default() -> None:
    exporter, _ = _setup()
    with VoiceCallTracer.start(call_id="c2", workspace_id="acme", agent_id="support") as call:
        with call.turn(0) as turn:
            with turn.stt("deepgram") as stt:
                stt.set(confidence=0.9, latency_ms=100)
            with turn.llm("gpt-4o") as llm:
                with llm.tool("send_email") as tool:
                    tool.set(execution_ms=20)
    email = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
    for span in exporter.get_finished_spans():
        a = attrs(span)
        for forbidden in c.PII_FORBIDDEN_ATTR_KEYS:
            assert forbidden not in a
        for value in a.values():
            if isinstance(value, str):
                assert email.search(value) is None
                assert "ssn" not in value.lower()
