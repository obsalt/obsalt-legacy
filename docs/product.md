# Product

obsalt is a self-hosted call analytics and quality system for AI voice agents.

The UI is scoped to exactly these surfaces: call list, call detail (timeline +
transcript + provenance), latency, hangups, quality, search, and settings.
No general charting. No custom dashboards.

The provenance panel is the differentiating surface. For every signal it shows
reported values with source path, derived values with derivation, and
`SignalCoverage` status — absent, unsupported, redacted, or decode failed —
with a reason.

See [rewrite-plan.md](rewrite-plan.md) §1 and §11.2.
