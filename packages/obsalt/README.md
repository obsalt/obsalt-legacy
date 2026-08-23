# obsalt

The core package: domain model, plugin host, durable ingest, assembly,
analysis, HTTP API, and the web console.

**Core ships no providers.** Install a source plugin such as
`obsalt-vapi` or `obsalt-pipecat`. Memory stores are test doubles, not
a backend.

```bash
pip install "obsalt[vapi,retell]"
docker compose up -d
obsalt serve
obsalt worker    # production: decode does not run on the webhook path
```

`obsalt demo` launches the same stack with a not-for-production banner.
`obsalt doctor` probes the durable stack and lists plugins.

This is a **service** (plus a small `VoiceCall` tracer for custom
agents), not an SDK that replaces your voice platform.

Start at the [documentation index](../../docs/README.md),
[Voice agents](../../docs/concepts.md) if the domain is new, or
[What obsalt does](../../docs/product.md).
