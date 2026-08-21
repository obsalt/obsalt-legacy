# Instrument an agent

Use this guide when **your process** owns STT, the LLM, tools, and TTS — Pipecat, LiveKit, a custom loop, or an OpenAI Realtime sidecar.

You will emit OpenTelemetry spans as the conversation happens. obsalt does not need to be running as a server for this path.

If a hosted platform (Vapi, Retell, Bland) runs the call, skip this page and use the [provider guides](providers/index.md).

Pipecat / LiveKit sketches: [Custom agents](providers/custom-agent.md). Runnable file: [examples/instrument_agent.py](../examples/instrument_agent.py).

## 1. Configure export

```python
from obsalt import setup_tracing

setup_tracing(otlp_endpoint="http://localhost:4318", environment="dev")
```

`setup_tracing` is process-wide. The HTTP server calls it automatically when `OBSALT_OTLP_ENDPOINT` is set; agent processes must call it themselves.

Production uses `BatchSpanProcessor`. Tests should pass an in-memory exporter and `batch=False` so spans flush immediately:

```python
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from obsalt import setup_tracing

exporter = InMemorySpanExporter()
setup_tracing(span_exporter=exporter, batch=False)
```

## 2. Open a call, then a turn

```python
from obsalt import VoiceCallTracer

with VoiceCallTracer.start(call_id="c1", workspace_id="acme", agent_id="support") as call:
    with call.turn(0, "user") as turn:
        with turn.stt("deepgram") as stt:
            stt.set(confidence=0.91, latency_ms=412)
            with stt.provider_attempt("deepgram"):
                pass  # the HTTP call to Deepgram goes here
        with turn.llm("gpt-4o", provider="openai") as llm:
            llm.set(ttft_ms=340, tokens_in=200, tokens_out=80, finish_reason="stop")
            with llm.tool("lookup_order") as tool:
                tool.set(execution_ms=120, status_code=200)
        with turn.tts("elevenlabs") as tts:
            tts.set(synthesis_ms=290, first_audio_ms=70)
        with turn.playout() as play:
            play.set(playout_ms=1400)
    with call.finalize_transcript("written"):
        pass
    call.set_call_outcome(duration_ms=45_000, status="ended")
```

`workspace_id` is your tenant. It becomes `workspace.id` on every span.

Open a child span around each independently failing step. If Deepgram times out and Azure succeeds, record both attempts:

```python
with stt.provider_attempt("deepgram") as attempt:
    attempt.fail("timeout")
with stt.provider_attempt("azure", fallback=True) as attempt:
    attempt.set(latency_ms=400, confidence=0.91)
```

## 3. Continue the trace in another process

A call often starts in one service and continues in another. Inject W3C `traceparent` on the way out; pass the same headers (or extracted context) into `VoiceCallTracer.start`.

```python
from obsalt import VoiceCallTracer
from obsalt.tracing import inject_traceparent, extract_traceparent

with VoiceCallTracer.start(call_id="c1", workspace_id="acme", agent_id="web") as call:
    headers = inject_traceparent({})          # sets headers["traceparent"]

# later, in a worker:
with VoiceCallTracer.start(call_id="c1", workspace_id="acme", agent_id="worker", headers=headers) as call:
    with call.evaluate("grounded-claims") as ev:
        ev.set(**{"assertion.result": "pass", "assertion.score": 1.0})

# equivalent:
ctx = extract_traceparent(headers)
VoiceCallTracer.start(..., context=ctx)
```

Format: `traceparent: 00-{32-hex-trace-id}-{16-hex-span-id}-01`.

Third-party STT/TTS APIs will not return `traceparent`. Bracket those HTTP calls with `stt.provider.{name}` / `tts.synthesis`. You will not see inside Deepgram; you will see timeout vs fallback.

## What not to put on spans

Do not pass transcript text, prompts, tool arguments, tool results, phone numbers, or emails to `set()`. Store those as evidence (see [Native snapshots](providers/native.md)). Spans should carry timings, model names, and join keys only.

## `set()` shortcuts

| Keyword | Span attributes |
| --- | --- |
| `confidence` | `stt.confidence` |
| `latency_ms` | `stt.latency_ms` |
| `ttft_ms` | `llm.ttft_ms` |
| `tokens_in` / `tokens_out` | `llm.tokens.*` and `gen_ai.usage.*` |
| `finish_reason` | `llm.finish_reason` |
| `execution_ms` | `tool.execution_ms` |
| `status_code` | `tool.status_code` |
| `synthesis_ms` / `first_audio_ms` | `tts.*` |
| `playout_ms` | `audio.playout_ms` |
| `end_of_utterance_ms` | `vad.end_of_utterance_ms` |
| `retry_count` | `tool.retry_count` |

Any other keyword is used as the attribute name as-is.

## Evidence

`VoiceCallTracer` only emits telemetry. To search transcripts or run evals, also POST a snapshot with [`CallRecorder`](providers/native.md) / `ObsaltClient`.
