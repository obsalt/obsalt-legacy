# Live instrumentation

Use this when **you** own STT/LLM/TTS (Pipecat, LiveKit, OpenAI Realtime sidecar). The SDK starts `call.lifecycle` at dial and opens child spans at each decision point.

```python
from obsalt.tracing import VoiceCallTracer, inject_traceparent

with VoiceCallTracer.start(call_id="c1", workspace_id="acme", agent_id="support") as call:
    with call.turn(0) as turn:
        with turn.stt(provider="deepgram") as stt:
            stt.set(confidence=0.91, latency_ms=412)
            with stt.provider_attempt("deepgram") as attempt:
                attempt.set(**{"stt.latency_ms": 412, "stt.confidence": 0.91})
        with turn.llm(model="gpt-4o", provider="openai") as llm:
            llm.set(ttft_ms=340, tokens_in=200, tokens_out=80, finish_reason="stop")
            with llm.tool("lookup_order") as tool:
                tool.set(execution_ms=120, status_code=200)
        with turn.tts(provider="elevenlabs") as tts:
            tts.set(synthesis_ms=290, first_audio_ms=70)
        with turn.playout() as play:
            play.set(playout_ms=1400)
    with call.finalize_transcript("written"):
        pass
    call.set_call_outcome(duration_ms=45_000, status="ended")
```

Do not put transcript text, prompts, or tool payloads on `set()`. Those belong in the evidence store.

## W3C traceparent

A call often starts in TypeScript/Go and continues in Python. Inject on the way out, extract on the way in:

```python
from obsalt.tracing import VoiceCallTracer, inject_traceparent, extract_traceparent

with VoiceCallTracer.start(call_id="c1", workspace_id="acme", agent_id="web") as call:
    headers = inject_traceparent({})          # sets headers["traceparent"]

ctx = extract_traceparent(headers)
with VoiceCallTracer.start(call_id="c1", workspace_id="acme", agent_id="worker", context=ctx) as call:
    ...
# equivalent: VoiceCallTracer.start(..., headers=headers)
```

Format: `traceparent: 00-{32-hex-trace-id}-{16-hex-span-id}-01`

Third-party STT/TTS APIs will **not** return `traceparent`. Bracket those HTTP calls with `stt.provider.{name}` / `tts.synthesis` client spans. You will not see inside Deepgram; you will see timeout vs fallback.

## Overhead

Use `BatchSpanProcessor` in production (`setup_tracing(otlp_endpoint=...)`). Hamming measures 1–3% added latency. Tests use `SimpleSpanProcessor` + in-memory exporters (`setup_tracing(..., batch=False)`).
