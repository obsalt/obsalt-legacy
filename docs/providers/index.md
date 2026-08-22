# Providers

Committed hosted source plugins: **Vapi**, **Retell**, **ElevenLabs**, **Cartesia Line**.

SDK + mapper: **OpenAI Realtime**, **Gemini Live** (speech-to-speech shape).

Convention mappers: **Pipecat**, **LiveKit**.

Bland is not in the committed set. Deepgram is `StreamSource`, unimplemented.

Each plugin vendors a pinned schema, captured payloads, and semantic unit assertions.
Word timestamps in Retell are **seconds**. Vapi `TurnLatency` fields are
`transcriberLatency` / `modelLatency` / `voiceLatency` / `turnLatency` /
`endpointingLatency` in milliseconds, **unplaced**.
