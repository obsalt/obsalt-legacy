# Provider ingest → same span tree

Vapi, Retell, Bland, and OpenAI Realtime do not emit Hamming’s hierarchy. Their webhooks/call objects are **partial evidence**. obsalt reconstructs `call.lifecycle` → `turn.*` → STT/LLM/TTS/tool spans with historical timestamps so Tempo shows the real waterfall.

That is the answer to “the provider already has a dashboard”: Hamming notes those UIs usually omit STT provider attempts, fallback chains, and tool execution timing. Reconstruction fills the gaps **in your** backend.

| Source | What we can reconstruct | What stays `unknown` |
| --- | --- | --- |
| Vapi `end-of-call-report` | Turns, tool calls, per-turn llm/tts/e2e from `metadata` / `performanceMetrics`, hangup | Often STT vendor unless `assistant.transcriber` is present |
| Retell `call_ended` | `latency.asr/llm/tts.values[]` as per-turn samples, `transcript_with_tool_calls`, `disconnection_reason` | Inside-provider spans |
| Bland post-call + `category=latency` | Transcript timestamp gaps as e2e/TTFA, `TTS: 218ms` lines, disposition | Fine-grained STT vendor |
| OpenAI Realtime event batch with `t_ms` | STT = speech_stopped→transcription.completed; TTFA = speech_stopped→first audio delta; tools from function_call events | Provider-internal TTS |

Reconstruction **must not** invent provider fallbacks. If the payload has one STT hop, emit one `stt.provider.{name}` child — never a fake Azure fallback.

After reconstruction, analysis (hangup taxonomy, hallucination, rubrics) attaches `evaluation.assertion_check` spans to the same root and writes the evidence packet (transcript, recording URL, redacted tool payloads) keyed by `call.id`.
