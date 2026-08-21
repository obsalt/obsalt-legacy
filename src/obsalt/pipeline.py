from __future__ import annotations

from typing import Any

from obsalt.adapters.registry import AdapterRegistry, merge_calls
from obsalt.domain.enums import Provider
from obsalt.domain.models import CanonicalCall, IngestResult
from obsalt.domain.redact import redact_value
from obsalt.evals.judges import HeuristicJudge, Judge
from obsalt.hallucination.detector import detect_hallucinations
from obsalt.hangup.analyzer import HangupAnalyzer
from obsalt.latency.breakdown import enrich_latency
from obsalt.store import MemoryStore, Store
from obsalt.tools.telemetry import enrich_call_tools
from obsalt.tracing.emitter import emit_call_trace, emit_eval_spans, record_call_metrics
from obsalt.tracing.metrics import VoiceMetrics
from obsalt.util import utcnow


class IngestPipeline:
    def __init__(
        self,
        store: Store | None = None,
        registry: AdapterRegistry | None = None,
        metrics: VoiceMetrics | None = None,
        judge: Judge | None = None,
        analyzer: HangupAnalyzer | None = None,
        environment: str = "dev",
    ) -> None:
        self.store = store or MemoryStore()
        self.registry = registry or AdapterRegistry()
        self.metrics = metrics
        self.judge = judge or HeuristicJudge()
        self.analyzer = analyzer or HangupAnalyzer()
        self.environment = environment

    def ingest(self, provider: Provider, payload: dict[str, Any], *, org_id: str) -> IngestResult:
        parsed = self.registry.parse(provider, payload, org_id=org_id)
        if parsed is None:
            raise ValueError(f"Unable to parse {provider.value} payload: missing call id")
        incoming = parsed.call
        existing = self.store.get_by_provider_id(org_id, incoming.provider, incoming.provider_call_id)
        created = existing is None
        call = incoming if existing is None else merge_calls(existing, incoming)
        call.updated_at = utcnow()
        _sanitize_tools(call)

        if parsed.terminal:
            call = self.finalize(call)
            status = "finalized"
        else:
            call.finalized = False
            self.store.upsert_call(call)
            status = "merged" if not created else "accepted"

        return IngestResult(
            call_id=call.id,
            provider_call_id=call.provider_call_id,
            status=status,
            created=created,
            finalized=call.finalized,
        )

    def finalize(self, call: CanonicalCall) -> CanonicalCall:
        enrich_latency(call)
        enrich_call_tools(call)
        call.hallucinations = detect_hallucinations(call)
        self.analyzer.analyze_call(call)
        rubrics = self.store.list_rubrics(call.org_id)
        call.evals = [self.judge.evaluate(call, rubric) for rubric in rubrics]
        call.finalized = True
        call.updated_at = utcnow()
        self.store.upsert_call(call)
        if call.metadata.get("spans_exported"):
            if self.metrics is not None:
                record_call_metrics(call, self.metrics, environment=self.environment)
            traceparent = call.metadata.get("traceparent")
            if isinstance(traceparent, str) and traceparent:
                emit_eval_spans(
                    call,
                    headers={"traceparent": traceparent},
                    metrics=self.metrics,
                    environment=self.environment,
                )
        else:
            emit_call_trace(call, metrics=self.metrics, environment=self.environment)
        return call

    def reevaluate(self, org_id: str, call_id: str) -> CanonicalCall | None:
        call = self.store.get_call(org_id, call_id)
        if call is None:
            return None
        return self.finalize(call)


def _sanitize_tools(call: CanonicalCall) -> None:
    """Strip secrets from tool argument values as soon as a payload is stored."""
    for tool in call.tools:
        args = tool.metadata.get("arguments")
        if args is not None:
            tool.metadata["arguments"] = redact_value(None, args)
