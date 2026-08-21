from __future__ import annotations

from opentelemetry import metrics
from opentelemetry.metrics import Meter

from obsalt.tracing import conventions as c

_METRIC_LABELS = ("agent", "environment", "stage", "outcome", "tool_name", "failure_type", "assertion_type", "stt_provider")


class VoiceMetrics:
    def __init__(self, meter: Meter | None = None) -> None:
        meter = meter or metrics.get_meter("obsalt")
        self._calls = meter.create_counter(c.METRIC_CALLS, description="Voice calls")
        hist_kwargs: dict = {
            "unit": "s",
            "description": "Pipeline stage latency",
        }
        try:
            self._latency = meter.create_histogram(
                c.METRIC_LATENCY,
                explicit_bucket_boundaries_advisory=list(c.LATENCY_BUCKETS),
                **hist_kwargs,
            )
        except TypeError:
            self._latency = meter.create_histogram(c.METRIC_LATENCY, **hist_kwargs)
        self._tool_fail = meter.create_counter(c.METRIC_TOOL_FAIL)
        self._low_conf = meter.create_counter(c.METRIC_LOW_CONFIDENCE)
        self._assert_fail = meter.create_counter(c.METRIC_ASSERT_FAIL)

    @staticmethod
    def _labels(**raw: str | None) -> dict[str, str]:
        # Drop high-cardinality / forbidden keys if callers slip.
        return {k: v for k, v in raw.items() if v and k in _METRIC_LABELS and k != "call_id"}

    def record_call(self, *, agent: str, environment: str = "prod", outcome: str = "ended") -> None:
        self._calls.add(1, self._labels(agent=agent, environment=environment, outcome=outcome))

    def record_stage(self, stage: str, duration_ms: float, *, agent: str, environment: str = "prod") -> None:
        self._latency.record(duration_ms / 1000.0, self._labels(agent=agent, environment=environment, stage=stage))

    def record_tool_failure(self, *, agent: str, tool_name: str, failure_type: str, environment: str = "prod") -> None:
        self._tool_fail.add(
            1,
            self._labels(agent=agent, environment=environment, tool_name=tool_name, failure_type=failure_type),
        )

    def record_low_confidence(self, *, agent: str, stt_provider: str, environment: str = "prod") -> None:
        self._low_conf.add(1, self._labels(agent=agent, environment=environment, stt_provider=stt_provider))

    def record_assertion_failure(self, *, agent: str, assertion_type: str, environment: str = "prod") -> None:
        self._assert_fail.add(
            1,
            self._labels(agent=agent, environment=environment, assertion_type=assertion_type),
        )
