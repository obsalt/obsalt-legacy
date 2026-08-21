# Voice agent trace model

obsalt follows Hamming’s conversation-shaped OpenTelemetry model ([guide](https://hamming.ai/resources/opentelemetry-voice-agents-tracing-guide)). A voice trace models a **call**, not an HTTP request.

## Span tree

```
call.lifecycle
├── turn.0
│   ├── vad.end_of_utterance
│   ├── stt.transcription
│   │   ├── stt.provider.deepgram          # timeout
│   │   └── stt.provider.fallback.azure
│   ├── llm.inference
│   │   └── llm.tool_call.check_inventory
│   ├── tts.synthesis
│   └── audio.playout
├── webhook.dispatch
├── transcript.finalization
└── evaluation.assertion_check
```

| Span | Parent | Proves |
| --- | --- | --- |
| `call.lifecycle` | root | Full call, status, tenant |
| `turn.{index}` | `call.lifecycle` | Which exchange broke |
| `vad.end_of_utterance` | turn | Endpointing delay before STT/LLM |
| `stt.transcription` | turn | Whether bad input reached the LLM |
| `stt.provider.{name}` | `stt.transcription` | Per-attempt latency / timeout |
| `stt.provider.fallback.{name}` | `stt.transcription` | Fallback chain (the cascade Langfuse misses) |
| `stt.provider_selection` | `stt.transcription` | Language routing decision |
| `llm.inference` | turn | Model, TTFT, tokens, finish reason |
| `llm.tool_call.{name}` | `llm.inference` | Side effect timing and status |
| `tts.synthesis` | turn | Synthesis vs playback |
| `audio.playout` | turn | Dead air after TTS |
| `webhook.dispatch` | `call.lifecycle` | Outbound delivery / retries |
| `transcript.finalization` | `call.lifecycle` | Canonical transcript written (cascade 1) |
| `evaluation.assertion_check` | `call.lifecycle` | Guardrail / rubric / hallucination |

## Join keys (on every span)

`call.id` · `workspace.id` · `agent.id` · `turn.index` · `gen_ai.conversation.id`

Optional: `room.id`, `test_run.id`, `scenario.id`.

These join Tempo → evidence store (transcript, recording) without putting PII in the trace.

## Hamming 12 debugging attributes

`stt.provider` `stt.confidence` `stt.latency_ms` `llm.model` `llm.ttft_ms` `llm.tokens.input` `llm.tokens.output` `tts.provider` `tts.synthesis_ms` `tool.name` `tool.execution_ms` `call.duration_ms`

Plus: `llm.finish_reason`, `tts.first_audio_ms`, `tts.voice_id`, `vad.end_of_utterance_ms`, `call.status`, `call.error_type`.

## GenAI conventions (LLM + tools only)

OTel GenAI is still development-status and does **not** standardize STT/TTS/VAD/barge-in/SIP. Dual naming:

- LLM: `gen_ai.operation.name=chat`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.usage.input_tokens` / `output_tokens`
- Tools: `gen_ai.operation.name=execute_tool`, `gen_ai.tool.name`, `gen_ai.tool.call.id`, `error.type`

## What must not appear on spans by default

Transcript text, prompts, tool arguments, tool results, phone numbers, emails. Store those as evidence; set `evidence.transcript_id`, `evidence.recording_id`, `evidence.redaction_state=redacted`.
