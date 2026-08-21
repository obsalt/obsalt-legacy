from __future__ import annotations

from typing import Protocol

from obsalt.domain.enums import LatencyComponent, Provider
from obsalt.domain.models import CanonicalCall, LatencyPercentiles, Rubric
from obsalt.evals.judges import default_rubrics
from obsalt.hangup.analyzer import HangupAnalyzer, HangupCluster
from obsalt.latency.stats import summarize
from obsalt.search.index import SearchHit, VectorIndex
from obsalt.tools.telemetry import ToolRollup, rollup_tools


class Store(Protocol):
    def upsert_call(self, call: CanonicalCall) -> CanonicalCall: ...
    def get_call(self, org_id: str, call_id: str) -> CanonicalCall | None: ...
    def get_by_provider_id(self, org_id: str, provider: Provider, provider_call_id: str) -> CanonicalCall | None: ...
    def list_calls(self, org_id: str, *, agent_id: str | None = None) -> list[CanonicalCall]: ...
    def upsert_rubric(self, rubric: Rubric) -> Rubric: ...
    def list_rubrics(self, org_id: str) -> list[Rubric]: ...
    def search(self, org_id: str, query: str, limit: int = 10) -> list[SearchHit]: ...
    def latency_rollup(self, org_id: str, *, agent_id: str | None = None) -> list[LatencyPercentiles]: ...
    def hangup_clusters(self, org_id: str) -> list[HangupCluster]: ...
    def tool_rollup(self, org_id: str, *, agent_id: str | None = None) -> list[ToolRollup]: ...


class MemoryStore:
    def __init__(self, index: VectorIndex | None = None, analyzer: HangupAnalyzer | None = None) -> None:
        self._calls: dict[str, CanonicalCall] = {}
        self._by_provider: dict[tuple[str, str, str], str] = {}
        self._rubrics: dict[str, Rubric] = {}
        self._seeded_orgs: set[str] = set()
        self.index = index or VectorIndex()
        self.analyzer = analyzer or HangupAnalyzer(self.index.embedder)

    def upsert_call(self, call: CanonicalCall) -> CanonicalCall:
        self._calls[call.id] = call
        self._by_provider[(call.org_id, call.provider.value, call.provider_call_id)] = call.id
        if call.finalized:
            text = call.transcript_text or " ".join(t.text for t in call.turns)
            self.index.upsert(call.id, text)
        return call

    def get_call(self, org_id: str, call_id: str) -> CanonicalCall | None:
        call = self._calls.get(call_id)
        if call is None or call.org_id != org_id:
            return None
        return call

    def get_by_provider_id(self, org_id: str, provider: Provider, provider_call_id: str) -> CanonicalCall | None:
        call_id = self._by_provider.get((org_id, provider.value, provider_call_id))
        if not call_id:
            return None
        return self.get_call(org_id, call_id)

    def list_calls(self, org_id: str, *, agent_id: str | None = None) -> list[CanonicalCall]:
        calls = [c for c in self._calls.values() if c.org_id == org_id]
        if agent_id:
            calls = [c for c in calls if c.agent_id == agent_id]
        calls.sort(key=lambda c: c.ingested_at, reverse=True)
        return calls

    def upsert_rubric(self, rubric: Rubric) -> Rubric:
        self._rubrics[rubric.id] = rubric
        return rubric

    def list_rubrics(self, org_id: str) -> list[Rubric]:
        if org_id not in self._seeded_orgs:
            for rubric in default_rubrics(org_id):
                self._rubrics.setdefault(rubric.id, rubric)
            self._seeded_orgs.add(org_id)
        return [r for r in self._rubrics.values() if r.org_id == org_id and r.enabled]

    def search(self, org_id: str, query: str, limit: int = 10) -> list[SearchHit]:
        allowed = {c.id for c in self.list_calls(org_id)}
        return self.index.search(query, limit=limit, allowed_ids=allowed)

    def latency_rollup(self, org_id: str, *, agent_id: str | None = None) -> list[LatencyPercentiles]:
        calls = self.list_calls(org_id, agent_id=agent_id)
        buckets: dict[LatencyComponent, list[float]] = {c: [] for c in LatencyComponent}
        for call in calls:
            for sample in call.latency_samples:
                buckets[sample.component].append(sample.duration_ms)
        return [summarize(component, values) for component, values in buckets.items() if values]

    def hangup_clusters(self, org_id: str) -> list[HangupCluster]:
        return self.analyzer.cluster(self.list_calls(org_id))

    def tool_rollup(self, org_id: str, *, agent_id: str | None = None) -> list[ToolRollup]:
        return rollup_tools(self.list_calls(org_id, agent_id=agent_id))
