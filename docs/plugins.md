# Plugins

Core ships no providers. Plugins register on the `obsalt.plugins` entry-point
group via `importlib.metadata`. First-party providers use the same group.

Capabilities: `webhook_source`, `otlp_mapper`, `rest_backfill`, `stream_source`
(declared, unimplemented in first-party packages — an example implementation
ships so Deepgram is additive), `sdk_instrumentation`, `authentication`,
`judge`, `embedder`, `redactor`.

Plugins are **trusted, operator-installed code**. They are pinned and
inventoried. Loading isolates version mismatches and ordinary exceptions. That
is not a security sandbox: an arbitrary wheel can still read process memory,
block, or exit. Tenant-installable plugins require a future out-of-process
runtime.

Authoring: depend on `obsalt-testkit`, subclass `DecoderConformanceTests`,
ship `fixtures/{schema,raw,expected}`. A fixture that fails the vendored vendor
schema fails CI unless it uses the reviewed overlay process.

```bash
pip install obsalt-testkit
obsalt-record --provider vapi --fixtures path/to/fixtures
```
