# obsalt

Core package: domain model, plugin host, durable ingest, assembly, analysis,
HTTP API, and UI. **Core ships no providers.** Install a source plugin such
as `obsalt-vapi` or `obsalt-retell`.

```bash
pip install "obsalt[vapi,retell]"
docker compose up -d
obsalt serve
```

`obsalt demo` launches the same stack with a not-for-production banner.

Plugins are trusted, operator-installed code. Start with the
[documentation index](../../docs/README.md).
