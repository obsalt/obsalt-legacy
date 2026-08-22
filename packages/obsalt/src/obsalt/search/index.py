"""In-memory hybrid search index — test double for Postgres tsvector + pgvector HNSW.

Do not grow this into a second production engine. Production search is one
Postgres projection with HNSW; this class exists so unit tests can exercise
filter + lexical + vector + RRF without a database.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from obsalt.domain.models import CallRevision
from obsalt.plugin.types import RedactedDocument
from obsalt.search.hybrid import TOKEN_RE, LocalEmbedder, rrf

INDEX_VERSION = "1"
EMBEDDER_VERSION = "local/256"


@dataclass
class _Document:
    call_id: str
    revision: str
    org_id: str
    agent_id: str
    source: str
    hangup_reason: str | None
    text: str
    tokens: set[str]
    vector: list[float]
    facets: dict[str, str | None] = field(default_factory=dict)


class MemorySearchIndex:
    """Test double for the pgvector HNSW + tsvector search projection."""

    def __init__(self, embedder: LocalEmbedder | None = None) -> None:
        self._embedder = embedder or LocalEmbedder(dim=256)
        self._docs: dict[str, _Document] = {}
        self.index_version = INDEX_VERSION
        self.embedder_version = EMBEDDER_VERSION

    def upsert_revision(self, call: CallRevision, *, index_version: str = "1") -> None:
        self.index(call)

    def index(self, call: CallRevision) -> None:
        text = _transcript(call)
        vector = _embed_sync(self._embedder, call.call_id, text)
        hangup_reason = call.hangup.reason.value if call.hangup else None
        key = f"{call.org_id}:{call.call_id}"
        self._docs[key] = _Document(
            call_id=call.call_id,
            revision=call.revision,
            org_id=call.org_id,
            agent_id=call.agent_id,
            source=call.source,
            hangup_reason=hangup_reason,
            text=text,
            tokens=set(TOKEN_RE.findall(text.lower())),
            vector=vector,
            facets={
                "org_id": call.org_id,
                "agent_id": call.agent_id,
                "source": call.source,
                "hangup_reason": hangup_reason,
                "call_id": call.call_id,
            },
        )

    def query(
        self,
        q: str,
        *,
        filters: dict[str, Any] | None = None,
        limit: int = 20,
        k: int = 60,
    ) -> dict[str, Any]:
        """Filter, then lexical + vector, fused with ``rrf`` from hybrid.py."""
        candidates = [doc for doc in self._docs.values() if _matches(doc, filters)]
        candidates.sort(key=lambda doc: doc.call_id)
        if not q.strip():
            items = [_hit(doc, 0.0) for doc in candidates[:limit]]
            return {
                "items": items,
                "lexical_ids": [],
                "vector_ids": [],
                "fused_ids": [doc.call_id for doc in candidates[:limit]],
                "index_version": self.index_version,
                "embedder_version": self.embedder_version,
            }

        query_tokens = set(TOKEN_RE.findall(q.lower()))
        lexical_ids = _lexical_rank(query_tokens, candidates)
        qvec = _embed_sync(self._embedder, "query", q)
        vector_ids = _vector_rank(qvec, candidates)
        fused_ids = rrf(vector_ids, lexical_ids, k=k)
        by_id = {doc.call_id: doc for doc in candidates}
        scores = _rrf_scores(vector_ids, lexical_ids, k=k)
        items = []
        for call_id in fused_ids[:limit]:
            doc = by_id[call_id]
            items.append(_hit(doc, scores.get(call_id, 0.0)))
        return {
            "items": items,
            "lexical_ids": lexical_ids,
            "vector_ids": vector_ids,
            "fused_ids": fused_ids,
            "index_version": self.index_version,
            "embedder_version": self.embedder_version,
        }


def _transcript(call: CallRevision) -> str:
    return "\n".join(f"{turn.speaker.value}: {turn.text}" for turn in call.turns)


def _matches(doc: _Document, filters: dict[str, Any] | None) -> bool:
    if not filters:
        return True
    for key, expected in filters.items():
        if expected is None:
            continue
        actual = doc.facets.get(key)
        if actual is None and key not in doc.facets:
            continue
        if str(actual) != str(expected):
            return False
    return True


def _lexical_rank(query_tokens: set[str], docs: Sequence[_Document]) -> list[str]:
    scored: list[tuple[float, str]] = []
    for doc in docs:
        if not query_tokens:
            continue
        overlap = len(query_tokens & doc.tokens)
        if overlap <= 0:
            continue
        tf = sum(1.0 for token in TOKEN_RE.findall(doc.text.lower()) if token in query_tokens)
        scored.append((tf, doc.call_id))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [call_id for _score, call_id in scored]


def _vector_rank(query: Sequence[float], docs: Sequence[_Document]) -> list[str]:
    scored = [(_dot(query, doc.vector), doc.call_id) for doc in docs]
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [call_id for _score, call_id in scored]


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    return float(sum(a * b for a, b in zip(left, right, strict=True)))


def _rrf_scores(vector_ids: Sequence[str], lexical_ids: Sequence[str], *, k: int) -> dict[str, float]:
    scores: dict[str, float] = {}
    for rank, item in enumerate(vector_ids, start=1):
        scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    for rank, item in enumerate(lexical_ids, start=1):
        scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return scores


def _hit(doc: _Document, score: float) -> dict[str, Any]:
    return {
        "call_id": doc.call_id,
        "revision": doc.revision,
        "score": score,
        "org_id": doc.org_id,
        "agent_id": doc.agent_id,
        "source": doc.source,
    }


def _embed_sync(embedder: LocalEmbedder, doc_id: str, text: str) -> list[float]:
    vectors = asyncio.run(embedder.embed([RedactedDocument(id=doc_id, text=text)]))
    return list(vectors[0].values)
