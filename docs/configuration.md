# Configuration

obsalt reads settings in this order (later wins nothing — **earlier wins**):

1. Constructor arguments (`Settings(api_keys=...)`) — tests and `create_app`
2. Environment variables `OBSALT_*`
3. `.env` in the working directory
4. `obsalt.toml` (`OBSALT_CONFIG`, else `./obsalt.toml`, else `~/.config/obsalt/obsalt.toml`)
5. Built-in defaults

```bash
obsalt init          # writes obsalt.toml + .env.example
obsalt doctor        # prints what was loaded, with secrets redacted
obsalt serve -c /etc/obsalt.toml
```

## Files

### `obsalt.toml`

Checked-in template: [`obsalt.toml.example`](../obsalt.toml.example). `obsalt.toml` is gitignored because it often holds keys.

```toml
[server]
host = "0.0.0.0"
port = 8080

[auth]
api_keys = "acme:change-me"
require_auth = false

[export]
otlp_endpoint = "http://localhost:4318"
environment = "dev"
service_name = "obsalt"

[webhooks]
vapi_secret = ""
retell_secret = ""
bland_secret = ""
```

A wrapping `[obsalt]` table is also accepted. Flat keys at the root (`port = 8080`) work too.

### `.env`

Template: [`.env.example`](../.env.example). Preferred place for secrets.

```bash
OBSALT_API_KEYS=acme:a-long-random-secret
OBSALT_REQUIRE_AUTH=true
OBSALT_OTLP_ENDPOINT=http://localhost:4318
OBSALT_VAPI_SECRET=...
```

## Environment variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `OBSALT_CONFIG` | Path to `obsalt.toml` | `./obsalt.toml` then `~/.config/obsalt/obsalt.toml` |
| `OBSALT_HOST` | Bind address | `0.0.0.0` |
| `OBSALT_PORT` | Bind port | `8080` |
| `OBSALT_API_KEYS` | `org:secret,org2:secret2` | `demo:demo-secret` |
| `OBSALT_REQUIRE_AUTH` | Reject missing/invalid keys | `false` |
| `OBSALT_OTLP_ENDPOINT` | OTLP HTTP **base**, e.g. `http://localhost:4318` | unset (no export) |
| `OBSALT_ENVIRONMENT` | `deployment.environment` on resources + metric label | `dev` |
| `OBSALT_SERVICE_NAME` | `service.name` | `obsalt` |
| `OBSALT_VAPI_SECRET` | Vapi `x-vapi-secret` | empty (skip check) |
| `OBSALT_RETELL_SECRET` | HMAC-SHA256 of the raw body (`x-retell-signature`) | empty (skip check) |
| `OBSALT_BLAND_SECRET` | `x-webhook-secret` or `Authorization: Bearer` | empty (skip check) |

`OBSALT_OTLP_ENDPOINT` is the collector base. obsalt posts traces to `{endpoint}/v1/traces` and metrics to `{endpoint}/v1/metrics`.

## API keys and tenants

Format: `org_id:secret` pairs, comma-separated.

```bash
OBSALT_API_KEYS=acme:sk_live_acme,beta:sk_live_beta
```

- The **secret** is what clients send (`X-API-Key` or `Authorization: Bearer`).
- The **org** becomes `workspace.id` on spans and `org_id` on stored calls.
- Two orgs must not share a secret (`obsalt doctor` fails if they do).
- `CallRecorder(org_id=...)` / `VoiceCall(workspace_id=...)` must use the **same** tenant as the API key if you want Tempo `call.id` to equal `GET /v1/calls/{id}`. The server trusts the API key, not the snapshot body, for `org_id`.

### Auth behavior

| `REQUIRE_AUTH` | Header | Result |
| --- | --- | --- |
| `true` | missing or unknown | `401` |
| `false` | missing | first configured org (or `demo`) |
| either | known secret | that org |
| either | unknown secret | `401` |

Local demos can leave `require_auth = false`. Do not do that on a public URL. `obsalt doctor` warns when auth is open or when `demo-secret` is still configured.

Provider HMAC is **in addition** to this. Empty webhook secret = signature skipped.

## CLI

```text
obsalt init [--force] [--dir PATH]
obsalt doctor [-c obsalt.toml]
obsalt serve [-c obsalt.toml] [--host] [--port] [--reload]
obsalt parse FILE|-- [-] [--provider ...] [--org ...] [--json]
obsalt version
```

`obsalt --port 8080` is accepted as `serve` for compatibility.

### `obsalt parse`

Runs the same adapters + analysis as the server, in-process, with an in-memory store. Use it to verify a vendor payload before you expose a webhook.

```bash
obsalt parse ./end-of-call.json              # auto-detect provider
obsalt parse ./body.json --provider retell
obsalt parse - --provider bland              # stdin
obsalt parse ./call.json --json              # full CanonicalCall
```

## Production checklist

`obsalt doctor` should be clean of `FAIL` lines. Before a public listener:

1. `OBSALT_REQUIRE_AUTH=true`
2. Rotate off `demo-secret` / `change-me`
3. Set the HMAC secret for every provider you enable
4. Point `OBSALT_OTLP_ENDPOINT` at a collector you actually run
5. `OBSALT_ENVIRONMENT=prod` (low-cardinality metric label)
6. Put a reverse proxy in front if you need TLS; obsalt speaks HTTP
7. Remember the store is **in-memory** in v0.1 — a restart loses calls. Plan a sidecar disk or do not treat `/v1/calls` as durable yet.

## Library configuration

The HTTP server calls `setup_tracing` when `otlp_endpoint` is set. Agent processes (Path B) that only use the SDK must call it themselves:

```python
from obsalt import setup_tracing, Settings

settings = Settings()  # same env / toml as the server
setup_tracing(
    otlp_endpoint=settings.otlp_endpoint or None,
    service_name=settings.service_name,
    environment=settings.environment,
)
```

Tests should pass an in-memory exporter and `batch=False` so spans flush immediately. See [Instrument an agent](instrumentation.md) and [Custom agents](custom-agents.md).
