# What you can see

| Question | Where |
| --- | --- |
| Where did time go **and** what was said? | `/v1/ui` per-call join + provenance panel |
| Fleet waterfalls / P95 of **real** spans | Your Tempo / Grafana |
| Hangup clusters, evals, search | obsalt HTTP API |

obsalt-derived metrics (`voice.call.duration`, `voice.stage.duration`, …) export
alongside forwarded OTLP. Provider aggregate latency (Retell p50/p95) is exported
as labelled gauges, **not** as span widths.
