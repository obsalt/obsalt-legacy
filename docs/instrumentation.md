# Instrument an agent

Use this page as the **span API reference** for Path B (you own STT / LLM / TTS).

Product path for Pipecat: [Custom agents](custom-agents.md). Decision: [Choose a path](choose-a-path.md).

If Vapi / Retell / Bland runs the call, skip this page — [provider guides](providers/index.md).

Prefer **`VoiceCall`** (spans + snapshot). `VoiceCallTracer` is the low-level span helper the server also uses when it reconstructs a tree.

## 1. Configure export

OTLP is a protocol. Call this in the **agent** process.

```python
from obsalt import setup_tracing

setup_tracing(otlp_endpoint="http://localhost:4318", environment="dev")
```

Production uses `BatchSpanProcessor`. Tests should pass an in-memory exporter and `batch=False`:

```python
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from obsalt import setup_tracing

exporter = InMemorySpanExporter()
setup_tracing(span_exporter=exporter, batch=False)
```

## 2. VoiceCall (recommended)

```python
from obsalt import VoiceCall, ObsaltClient, setup_tracing

setup_tracing(otlp_endpoint="http://localhost:4318")
client = ObsaltClient(api_key="secret")

with VoiceCall.start(
    call_id="c1",
    workspace_id="acme",
    agent_id="support",
    client=client,
) as call:
    with call.turn(0, "user", text="book Friday") as turn:
        with turn.stt("deepgram") as stt:
            stt.set(confidence=0.91, latency_ms=412)
            with stt.provider_attempt("deepgram"):
                pass
        with turn.llm("gpt-4o", provider="openai") as llm:
            llm.set(ttft_ms=340, tokens_in=200, tokens_out=80, finish_reason="stop")
            with llm.tool("lookup_order", {"order_id": "1"}) as tool:
                tool.set(execution_ms=120, status_code=200)
                tool.set_result({"ok": True})
        with turn.tts("elevenlabs") as tts:
            tts.set(synthesis_ms=290, first_audio_ms=70)
        with turn.playout() as play:
            play.set(playout_ms=1400)
    call.set_call_outcome(duration_ms=45_000, status="ended")
```

`call.obsalt_call_id` is `call.id` on every span (uuid5 of workspace + provider + `call_id`). `call.provider_call_id` is `"c1"`.

`text=` on `turn()` is evidence. It is not a span attribute. `provider_attempt` hops are stored on the snapshot (`turn.metadata.stt_attempts`) so `/v1/ui` can show a Deepgram timeout → Azure fallback after the call ends.

## 3. VoiceCallTracer (spans only)

Same helpers, no snapshot. Use when you truly do not want evidence.

```python
from obsalt import VoiceCallTracer

with VoiceCallTracer.start(call_id="c1", workspace_id="acme", agent_id="support") as call:
    with call.turn(0, "user") as turn:
        with turn.stt("deepgram") as stt:
            stt.set(confidence=0.91, latency_ms=412)
    call.set_call_outcome(duration_ms=45_000, status="ended")
```

Open a child span around each independently failing step. If Deepgram times out and Azure succeeds:

```python
with stt.provider_attempt("deepgram") as attempt:
    attempt.fail("timeout")
with stt.provider_attempt("azure", fallback=True) as attempt:
    attempt.set(latency_ms=400, confidence=0.91)
```

## 4. Continue the trace in another process

Inject W3C `traceparent` on the way out; pass the same headers into `VoiceCall.start` / `VoiceCallTracer.start`.

```python
from obsalt import VoiceCallTracer
from obsalt.tracing import inject_traceparent, extract_traceparent

with VoiceCallTracer.start(call_id="c1", workspace_id="acme", agent_id="web") as call:
    headers = inject_traceparent({})

with VoiceCallTracer.start(call_id="c1", workspace_id="acme", agent_id="worker", headers=headers) as call:
    with call.evaluate("grounded-claims") as ev:
        ev.set(**{"assertion.result": "pass", "assertion.score": 1.0})
```

Format: `traceparent: 00-{32-hex-trace-id}-{16-hex-span-id}-01`.

Third-party STT/TTS APIs will not return `traceparent`. Bracket those HTTP calls with `stt.provider.{name}` / `tts.synthesis`.

## What not to put on spans

Do not pass transcript text, prompts, tool arguments, tool results, phone numbers, or emails to `set()`. Store those as evidence (`turn(text=...)`, `tool(..., arguments=)`, snapshot). Spans carry timings, model names, and join keys only.

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

## Evidence without VoiceCall

[`CallRecorder`](providers/native.md) builds the same snapshot without in-process OTel. The server then reconstructs the span tree.
