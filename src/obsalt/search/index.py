from __future__ import annotations

from dataclasses import dataclass

from obsalt.search.embeddings import Embedder, HashingEmbedder, cosine, tokenize


@dataclass
class SearchHit:
    call_id: str
    score: float
    snippet: str


class VectorIndex:
    def __init__(self, embedder: Embedder | None = None) -> None:
        self.embedder = embedder or HashingEmbedder()
        self._vectors: dict[str, list[float]] = {}
        self._snippets: dict[str, str] = {}

    def upsert(self, call_id: str, text: str) -> None:
        self._vectors[call_id] = self.embedder.embed(text)
        snippet = " ".join(text.split())
        self._snippets[call_id] = snippet[:280]

    def delete(self, call_id: str) -> None:
        self._vectors.pop(call_id, None)
        self._snippets.pop(call_id, None)

    def search(self, query: str, limit: int = 10, allowed_ids: set[str] | None = None) -> list[SearchHit]:
        q = self.embedder.embed(query)
        q_tokens = set(tokenize(query))
        hits: list[SearchHit] = []
        for call_id, vector in self._vectors.items():
            if allowed_ids is not None and call_id not in allowed_ids:
                continue
            semantic = cosine(q, vector)
            snippet = self._snippets.get(call_id, "")
            s_tokens = set(tokenize(snippet))
            if q_tokens and s_tokens:
                lexical = len(q_tokens & s_tokens) / len(q_tokens | s_tokens)
            else:
                lexical = 0.0
            score = 0.7 * semantic + 0.3 * lexical
            hits.append(SearchHit(call_id=call_id, score=round(score, 4), snippet=snippet))
        hits.sort(key=lambda h: h.score, reverse=True)
        return [h for h in hits if h.score > 0][:limit]
