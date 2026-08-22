# Providers

Committed hosted source plugins: **Vapi**, **Retell**, **ElevenLabs**,
**Cartesia Line**.

SDK + mapper: **OpenAI Realtime**, **Gemini Live** (speech-to-speech shape).

Convention mappers: **Pipecat**, **LiveKit**.

Bland is not in the committed set. Deepgram is `StreamSource`, unimplemented.
The survey matrix, provider list, URLs, and retrieval dates live beside the
plugin fixtures so the "hosted platforms do not push standard OTLP" claim
remains reproducible.

Each plugin vendors a pinned schema, captured payloads, and semantic unit
assertions. See [write a plugin](../guides/write-a-plugin.md).

## Fidelity

| Source | Architecture | Fidelity | Waterfall | Notes |
| --- | --- | --- | --- | --- |
| Pipecat (OTLP) | cascade or S2S | `stage_level` | yes | real spans |
| LiveKit (OTLP) | cascade or S2S | `stage_level` | yes | real spans |
| obsalt SDK | either | `stage_level` | yes | we control the clock |
| Cartesia Line | cascade | `turn_level` | no | real turn intervals + unplaced STT/TTS TTFBs |
| Vapi | cascade | `turn_level` | no | `secondsFromStart`+`duration` real; stage durations unplaced |
| Retell | cascade | `turn_level` | no | word intervals in seconds; stage distributions are call-level |
| ElevenLabs post-call JSON | cascade | `message_level` | no | whole-second message anchors, no documented end timestamps |
| ElevenLabs OTLP-shaped webhook | cascade | derived per span | where interval contract passes | mapper validates each span |
| OpenAI Realtime | speech_to_speech | `stage_level` for stages that exist | partial | no STT/LLM/TTS split |
| Gemini Live | speech_to_speech | `stage_level` for stages that exist | partial | same |

Speech-to-speech sources show `user_input` / `generation` / `playout`.
Cascade stages do not exist there and must not be shown as empty.

## Authentication

Provider authentication is a plugin capability. The schemes are incompatible
and some are operator-configurable.

| Provider | Status | Header | Scheme |
| --- | --- | --- | --- |
| Vapi | committed | configurable | Distinct static Bearer, OAuth2, and HMAC paths. HMAC includes algorithm, signature header, optional timestamp header, payload canonicalization. Legacy `X-Vapi-Secret`. |
| Retell | committed | `X-Retell-Signature` | `v={unix_ms},d={hex}`; `HMAC-SHA256(raw_body + timestamp)` keyed by the **API key**; ±5 min |
| ElevenLabs | committed | `ElevenLabs-Signature` | `t={unix},v0={hex}`; `HMAC-SHA256("{t}.{body}")`; 30-min two-sided tolerance |
| Cartesia Line | committed | `x-webhook-secret` | plain shared secret (weakest; documented as such) |
| Bland | surveyed future | `X-Webhook-Signature` | `HMAC-SHA256(body)` hex, no timestamp → no replay protection |
| Telnyx | surveyed future | `telnyx-signature-ed25519` + `telnyx-timestamp` | Ed25519 over `"{timestamp}\|{raw_body}"`; enforce timestamp freshness |

Fail-closed. Empty secret is not "skip verification." `VerifyResult` has
explicit malformed, missing-credential, bad-signature, stale, and replayed
outcomes.

## Units that have already bitten us

- Retell `words[].start/end` are **seconds**.
- Vapi `TurnLatency` fields are milliseconds and **unplaced**. Published
  keys are `transcriberLatency` / `modelLatency` / `voiceLatency` /
  `turnLatency` / `endpointingLatency`, not `stt` / `llm` / `tts` / `e2e`.
- ElevenLabs post-call message anchors are whole seconds.
- Bland `disposition_tag` is user-definable and LLM-assigned — unsound as a
  primary hangup key. `call_ended_by` is the reliable signal. Noted for
  whoever adds Bland back.
- If Bland is restored, `agent-action` rows ("Ended call", "Transferred
  call") must not be counted as tools. That polluted v0.1's rollup with
  fake 0ms 100%-success entries.

## Hangup mapping

Classification is provider code → normalized reason. Mapping tables are
generated from the provider's published enum and CI-checked for drift.
Unmapped values fail the plugin's coverage threshold instead of silently
becoming `unknown`.

Clustering groups by `(reason, party, closing-utterance semantics)` as a
scheduled job into a materialized table. Re-embedding every call on every
`/v1/hangups` request is forbidden.

"The call that lost you the customer" is a ranked score combining hangup
reason, closing sentiment, unresolved tool failures, hallucination flags,
and abnormal latency on the final turn — with the contributing factors
shown, not just a number.

## Naming map

| PyPI | Entry point | `decoder_version` prefix |
| --- | --- | --- |
| `obsalt-vapi` | `vapi` | `vapi/` |
| `obsalt-retell` | `retell` | `retell/` |
| `obsalt-elevenlabs` | `elevenlabs` | `elevenlabs/` |
| `obsalt-cartesia` | `cartesia` | `cartesia/` |
| `obsalt-openai-realtime` | `openai_realtime` | `openai-realtime/` |
| `obsalt-gemini-live` | `gemini_live` | `gemini-live/` |
| `obsalt-pipecat` | `pipecat` | `pipecat/` |
| `obsalt-livekit` | `livekit` | `livekit/` |
