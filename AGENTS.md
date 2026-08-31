# Working in this repository

This file is for humans and coding agents. It is the short form of
[docs/conventions.md](docs/conventions.md) plus the map of the tree.
Docs index: [docs/README.md](docs/README.md). Compact machine map:
[docs/llms.txt](docs/llms.txt).

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
- There is no heuristic judge. Cheap path is `detect_claims`
  (`detector/2`): evidence-backed flags, not a Faithfulness table.
  English pack/rubrics stay `not_judged` until an LLM runner is enabled.
  Historical `heuristic/1` rows are never confirmed. Pack `tool_use`
  does not fail on effective tool failure alone.
- A console/API verdict is source-backed or detector-settled. Heuristics
  are not verdicts.
- Do not introduce `require_auth=false`.
- Do not invent provider fields in fixtures.
- Do not re-introduce a private “POST us a JSON snapshot” SDK. Custom
  agents emit OTLP. Hosted platforms POST signed webhooks.

## Where to edit

| Task | Start here |
| --- | --- |
| HTTP / console | `packages/obsalt/src/obsalt/api.py` |
| Console labels | `packages/obsalt/src/obsalt/ui/` |
| CLI | `packages/obsalt/src/obsalt/cli.py` |
| Settings / `.env.example` | `packages/obsalt/src/obsalt/config.py` (`ENV_EXAMPLE`) |
| Domain types | `packages/obsalt/src/obsalt/domain/` |
| Webhook / OTLP receive | `packages/obsalt/src/obsalt/ingest/` |
| Fold + promote | `packages/obsalt/src/obsalt/assemble/` |
| Analyzers / evals / rollups | `packages/obsalt/src/obsalt/analysis/` |
| Worker loops (decode / drain) | `packages/obsalt/src/obsalt/worker/` |
| Sweep / doctor / seed / parquet | `packages/obsalt/src/obsalt/ops/` |
| Plugin contract | `packages/obsalt/src/obsalt/plugin/` |
| Hosted decoder | `packages/obsalt-<provider>/` |
| `VoiceCall` | `packages/obsalt/src/obsalt/session.py` |
| Tests | `tests/<kind>/` — directory is the pytest marker |

## Commands

```bash
uv sync --all-packages   # or: make install
make test-unit          # fast
make lint
make ci                 # lint + typecheck + full suite
obsalt parse FILE --provider vapi
obsalt record-golden FILE --provider vapi
obsalt record-eval-case CALL_ID --org local --out case.json
obsalt seed             # HTTP replay of vendored fixtures into a running serve
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
  `docs/conventions.md`). Product docs, not a company page. Simple
  mermaid (`flowchart` / `sequenceDiagram`, no HTML labels) plus ASCII
  trees. No mermaid with punctuation in subgraph titles.
