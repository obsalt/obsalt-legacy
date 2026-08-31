# Contributing

## I want to use obsalt

Start at the [README](README.md). The documentation index is
[docs/README.md](docs/README.md). This file is for changing the code.

## I want to change the code

```bash
uv sync --all-packages
make test-unit          # no Docker
make lint
```

Optional, once: `pre-commit install` (hooks live in
`.pre-commit-config.yaml`). CI already runs Ruff.

Read, in this order:

1. [Develop](docs/develop.md) — layout, commands, how to add a source
2. [Conventions](docs/conventions.md) — the rules a formatter cannot see
3. [AGENTS.md](AGENTS.md) — one-screen version for you and for coding agents

## Pull request bar

- One concern per PR.
- Tests for the behavior you changed. Plugin changes need fixture +
  golden + schema validation.
- No invented provider fields.
- Docs if you change a product surface, a public contract, or how
  someone connects an agent. Update the page a human would actually
  open — see the table in [Conventions](docs/conventions.md).
- `make lint` and `make test-unit` green. `make ci` before you call it
  done.

Use [`.github/pull_request_template.md`](.github/pull_request_template.md).
