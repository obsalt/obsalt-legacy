# Glossary

- **OTLP** — a protocol, not an obsalt server.
- **VoiceCall** — a library class in your agent process.
- **ingest_key** — per-connection identifier in the webhook URL; hashed at rest.
- **RawEnvelope** — verbatim inbound bytes plus inbox metadata.
- **NormalizedEvent** — a small fact a decoder emits.
- **CallRevision** — a complete immutable snapshot. Postgres points at the active one.
- **TimelineFidelity** — derived from the measurements actually present on the call.
- **obsalt.pii.*** — the only place conversational content may appear on spans.
