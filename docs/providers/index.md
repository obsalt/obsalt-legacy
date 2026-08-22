# Providers

| Package | Role | v2 status |
| --- | --- | --- |
| `obsalt-vapi` | Hosted webhook | committed |
| `obsalt-retell` | Hosted webhook | committed |
| `obsalt-elevenlabs` | Hosted webhook + OTLP-shaped mapper | committed |
| `obsalt-cartesia` | Hosted webhook | committed |
| `obsalt-openai-realtime` | SDK + S2S mapper | committed |
| `obsalt-gemini-live` | SDK + S2S mapper | committed |
| `obsalt-pipecat` | Convention mapper | committed |
| `obsalt-livekit` | Convention mapper | committed |
| Bland | surveyed future / community | dropped from the committed set |
| Deepgram | `StreamSource` | designed for, not built |

Each plugin ships vendored schemas, captured payloads, golden outputs, and a
fidelity declaration. Coverage is derived from decode output, not from the
declaration alone.
