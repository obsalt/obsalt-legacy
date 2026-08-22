# obsalt

Core package for obsalt v2: domain model, plugin host, durable ingest, assembly,
analysis, HTTP API, and UI. **Core ships no providers.** Install a source plugin
such as `obsalt-vapi` or `obsalt-retell`.

```bash
pip install obsalt obsalt-vapi obsalt-retell
obsalt demo   # ephemeral full stack — not for production
obsalt serve
```

Plugins are trusted, operator-installed code. See `docs/plugins.md`.
