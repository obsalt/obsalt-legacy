"""Local air-gapped embedder. Character n-gram hashing, 384 dimensions.

This is a real embedding (feature hashing), not a document content-hash
stand-in. An ONNX model can replace it via the Embedder plugin capability.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence

from obsalt.plugin.protocol import RedactedDocument, Vector

DIM = 384
EMBEDDER_VERSION = "ngram/1"


class NgramEmbedder:
    name = "ngram"
    version = EMBEDDER_VERSION
    dim = DIM

    def embed_sync(self, documents: Sequence[RedactedDocument]) -> list[Vector]:
        return [Vector(document_id=doc.document_id, values=ngram_vector(doc.text)) for doc in documents]

    async def embed(self, documents: Sequence[RedactedDocument]) -> Sequence[Vector]:
        return self.embed_sync(documents)


def ngram_vector(text: str, *, dim: int = DIM) -> list[float]:
    acc = [0.0] * dim
    lowered = text.lower()
    for n in (2, 3, 4):
        if len(lowered) < n:
            continue
        for i in range(len(lowered) - n + 1):
            gram = lowered[i : i + n]
            digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
            acc[int.from_bytes(digest, "big") % dim] += 1.0
    norm = math.sqrt(sum(value * value for value in acc)) or 1.0
    return [value / norm for value in acc]


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    return float(sum(a * b for a, b in zip(left, right, strict=False)))
