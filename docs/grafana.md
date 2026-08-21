# Grafana routing

Do not put everything in Prometheus. Hamming’s split:

| Signal | Backend | Example |
| --- | --- | --- |
| Counts / histograms | Prometheus / Mimir | `voice_calls_total{agent,environment,outcome}` |
| Stage timings (debug a cascade) | Tempo via OTel traces | `stt.transcription`, `llm.inference` |
| Searchable call facts | Loki | `voice.turn.completed` JSON envelope |
| Transcript / audio / QA | Evidence store | `evidence.transcript_id` pointer only |

## Prometheus (low cardinality)

Implemented as OTel metrics (exportable to Prometheus):

- `voice_calls_total` — labels: `agent`, `environment`, `outcome` (not `call_id`)
- `voice_response_latency_seconds` — labels: `agent`, `environment`, `stage` (`stt` `llm` `tts` `e2e` `ttfa` `tool`)
- `voice_tool_failures_total` — `agent`, `tool_name`, `failure_type`
- `voice_low_confidence_turns_total` — `agent`, `stt_provider`
- `voice_assertion_failures_total` — `agent`, `assertion_type`

**Forbidden labels:** `call_id`, `user_id`, phone, transcript, prompt.

P95 by stage:

```
histogram_quantile(0.95,
  sum by (le, stage) (rate(voice_response_latency_seconds_bucket{environment="prod"}[5m])))
```

## Loki event envelope

See `obsalt.tracing.events.turn_completed_event`. Required fields: `canonical_call_id`, `trace_id`, `redaction_state`. `canonical_call_id` stays in the JSON body, not as a Loki label.
