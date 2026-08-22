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


def vector_rank(
    query_vec: list[float],
    documents: list[tuple[str, int, str, list[float]]],
) -> list[SearchHit]:
    from obsalt.search.embedder import cosine

    hits: list[SearchHit] = []
    for call_id, revision, text, vec in documents:
        score = cosine(query_vec, vec)
        if score > 0:
            hits.append(SearchHit(call_id, revision, score, text[:280], "vector"))
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits


def hybrid_search(
    query: str,
    documents: list[tuple[str, int, str]],
) -> list[SearchHit]:
    from obsalt.plugin.protocol import RedactedDocument
    from obsalt.search.embedder import NgramEmbedder, ngram_vector

    lexical = lexical_rank(query, documents)
    embedder = NgramEmbedder()
    query_vec = ngram_vector(query)
    catalog = [
        (
            call_id,
            revision,
            text,
            embedder.embed_sync([RedactedDocument(document_id=call_id, text=text)])[0].values,
        )
        for call_id, revision, text in documents
    ]
    vector = vector_rank(query_vec, catalog)
    return reciprocal_rank_fusion([lexical, vector])
