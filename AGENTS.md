# Working in this repository

This file is for humans and coding agents. It is the short form of
[docs/conventions.md](docs/conventions.md) plus the map of the tree.

obsalt is a **self-hosted call analytics and quality system for live AI
voice agents**. Core ships **no** providers. Memory stores are **test
doubles**, not a backend.

## Before you change code

1. Read [docs/product.md](docs/product.md) if you do not know what the
   six capabilities are.
2. Read [docs/architecture.md](docs/architecture.md) § “The rules we
   will not break” if you are touching ingest, assemble, or storage.
3. Match existing style. Smallest correct change. No drive-by refactors.

## Non-negotiable

- Do not draw a waterfall from unplaced durations.
- Do not decode on the webhook request path.
- Do not add a second storage backend or a SQLite mode.
- Do not let a payload field or OTLP attribute choose `org_id`.
- Do not treat missing Tier-2 output as a passing eval.
- Do not introduce `require_auth=false`.
- Do not invent provider fields in fixtures.
- Do not re-introduce a private “POST us a JSON snapshot” SDK. Custom
  agents emit OTLP. Hosted platforms POST signed webhooks.

## Where to edit

| Task | Start here |
| --- | --- |
| HTTP / console | `packages/obsalt/src/obsalt/api.py` |
| CLI | `packages/obsalt/src/obsalt/cli.py` |
| Settings / `.env.example` | `packages/obsalt/src/obsalt/config.py` (`ENV_EXAMPLE`) |
| Domain types | `packages/obsalt/src/obsalt/domain/` |
| Webhook / OTLP receive | `packages/obsalt/src/obsalt/ingest/` |
| Fold + promote | `packages/obsalt/src/obsalt/assemble/` |
| Plugin contract | `packages/obsalt/src/obsalt/plugin/` |
| Hosted decoder | `packages/obsalt-<provider>/` |
| `VoiceCall` | `packages/obsalt/src/obsalt/session.py` |
| Tests | `tests/<kind>/` — directory is the pytest marker |

## Commands

```bash
make install
make test-unit          # fast
make lint
make ci                 # lint + typecheck + full suite
obsalt parse FILE --provider vapi
obsalt record-golden FILE --provider vapi
```

Typecheck is strict and **informational in CI** until the mypy backlog
is cleared. Lint and tests are the required gates.

## Style in one screen

- `from __future__ import annotations`
- Absolute imports, Ruff line length 100
- Pydantic v2 + `StrEnum`
- Keep the product vocabulary (`CallRevision`, `NormalizedEvent`,
  `org_id`, `ingest_key`, …)
- Production CLI commands exit 2 when the durable stack is down
- Docs: update the page a user would actually open (see the table in
  `docs/conventions.md`)
