"""Hybrid lexical + vector retrieval with reciprocal-rank fusion.

One Postgres search projection. Embeddings are of redacted content.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SearchHit:
    call_id: str
    revision: int
    score: float
    snippet: str
    source: str


def reciprocal_rank_fusion(
    ranked_lists: list[list[SearchHit]],
    *,
    k: int = 60,
) -> list[SearchHit]:
    scores: dict[str, float] = {}
    best: dict[str, SearchHit] = {}
    for hits in ranked_lists:
        for rank, hit in enumerate(hits, start=1):
            scores[hit.call_id] = scores.get(hit.call_id, 0.0) + 1.0 / (k + rank)
            prev = best.get(hit.call_id)
            if prev is None or len(hit.snippet) > len(prev.snippet):
                best[hit.call_id] = hit
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    out: list[SearchHit] = []
    for call_id, score in ordered:
        hit = best[call_id]
        out.append(
            SearchHit(
                call_id=call_id,
                revision=hit.revision,
                score=round(score, 6),
                snippet=hit.snippet,
                source="rrf",
            )
        )
    return out


def lexical_rank(query: str, documents: list[tuple[str, int, str]]) -> list[SearchHit]:
    tokens = {t.lower() for t in query.split() if t}
    hits: list[SearchHit] = []
    for call_id, revision, text in documents:
        hay = text.lower()
        score = sum(1.0 for token in tokens if token in hay)
        if score:
            hits.append(SearchHit(call_id, revision, score, text[:280], "lexical"))
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits
