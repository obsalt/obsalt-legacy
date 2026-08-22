# Choose a path

Who owns STT / LLM / TTS?

- **A hosted platform** (Vapi, Retell, ElevenLabs, Cartesia Line) → Path A: signed webhook to `POST /v1/ingest/{provider}/{ingest_key}`. Do not import `VoiceCall` for those calls.
- **Your agent** (Pipecat, LiveKit, OpenAI Realtime, Gemini Live) → Path B: emit OTLP from the process. `VoiceCall` is a thin tracer.

Mixing both on the same call is almost always a mistake.

Hosted platforms in the survey do not push standard OTLP to an arbitrary collector. The webhook receiver is the ingest path that exists. obsalt does **not** reconstruct a waterfall from their summary statistics.

Custom agents have real clocks. Their spans are forwarded with identity preserved. Cascade stages appear only when they exist; speech-to-speech sources use `user_input` / `generation` / `playout`.
