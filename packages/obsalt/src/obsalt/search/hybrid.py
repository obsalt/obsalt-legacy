from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence

from obsalt.plugin.types import RedactedDocument, Vector

TOKEN_RE = re.compile(r"[a-z0-9]+")


class LocalEmbedder:
    """Deterministic bag-of-tokens embedding for air-gapped defaults.

    A production install should swap this for an ONNX MiniLM or a hosted embedder.
    This is not the v0.1 MD5 trick: documents that share tokens land nearby.
    """

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    async def embed(self, documents: Sequence[RedactedDocument]) -> Sequence[Vector]:
        out: list[Vector] = []
        for doc in documents:
            values = [0.0] * self.dim
            tokens = TOKEN_RE.findall(doc.text.lower())
            for token in tokens:
                idx = int(hashlib.sha256(token.encode()).hexdigest(), 16) % self.dim
                values[idx] += 1.0
            norm = sum(v * v for v in values) ** 0.5 or 1.0
            out.append(Vector(id=doc.id, values=[v / norm for v in values]))
        return out


def rrf(vector_ids: Sequence[str], lexical_ids: Sequence[str], *, k: int = 60) -> list[str]:
    scores: dict[str, float] = {}
    for rank, item in enumerate(vector_ids, start=1):
        scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    for rank, item in enumerate(lexical_ids, start=1):
        scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return [item for item, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)]
