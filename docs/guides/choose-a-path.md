# Choose a path

Who owns STT / LLM / TTS?

- **A hosted platform** (Vapi, Retell, ElevenLabs, Cartesia Line) → Path A:
  signed webhook to `POST /v1/ingest/{provider}/{ingest_key}`. Do not import
  `VoiceCall` for those calls.
- **Your agent** (Pipecat, LiveKit, OpenAI Realtime, Gemini Live) → Path B:
  emit OTLP from the process. `VoiceCall` is a thin tracer.

Mixing both on the same call is almost always a mistake. You will get two
partial records that do not join cleanly, and you will not know which clock
is authoritative.

## Path A — hosted webhook

Hosted platforms in the survey do not push standard OTLP to an arbitrary
collector. The webhook receiver is the ingest path that exists. obsalt does
**not** reconstruct a waterfall from their summary statistics.

Typical fidelity:

| Source | Architecture | Fidelity | Waterfall |
| --- | --- | --- | --- |
| Vapi | cascade | `turn_level` | no — stage durations unplaced |
| Retell | cascade | `turn_level` | no — word intervals in seconds; stage distributions are call-level |
| ElevenLabs post-call JSON | cascade | `message_level` | no — whole-second message anchors |
| Cartesia Line | cascade | `turn_level` | no — real turn intervals + unplaced STT/TTS TTFBs |

That is still enough for hangup analysis, evals, search, tool telemetry, and
honest latency chips. It is not enough for a stage waterfall. The UI says so.

Continue: [Hosted webhook](hosted-webhook.md).

## Path B — custom-agent OTLP

Custom agents have real clocks. Their spans are forwarded with identity
preserved. Cascade stages appear only when they exist. Speech-to-speech
sources use `user_input` / `generation` / `playout`.

| Source | Architecture | Fidelity | Waterfall |
| --- | --- | --- | --- |
| Pipecat (OTLP) | cascade or S2S | `stage_level` | yes, where intervals are real |
| LiveKit (OTLP) | cascade or S2S | `stage_level` | yes |
| obsalt SDK | either | `stage_level` | yes — we control the clock |
| OpenAI Realtime | speech-to-speech | `stage_level` for stages that exist | partial — no STT/LLM/TTS split |
| Gemini Live | speech-to-speech | `stage_level` for stages that exist | partial |

Continue: [Custom agent](custom-agent.md).

## A quick test

If you cannot answer "what is the start timestamp of the LLM stage on turn
3?", you are on Path A and should not expect a waterfall. If you can, and
you own the process, you are on Path B.
