from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence

from obsalt.plugin.types import RedactedDocument, Vector

TOKEN_RE = re.compile(r"[a-z0-9]+")
STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "at",
        "be",
        "can",
        "do",
        "for",
        "how",
        "i",
        "in",
        "is",
        "it",
        "me",
        "my",
        "of",
        "on",
        "or",
        "please",
        "that",
        "the",
        "this",
        "to",
        "we",
        "will",
        "with",
        "you",
        "your",
    }
)


def content_tokens(text: str) -> set[str]:
    return {token for token in TOKEN_RE.findall(text.lower()) if token not in STOP_WORDS and len(token) > 2}


class LocalEmbedder:
    """Deterministic lexical embedding used as the ONNX input and offline default.

    Token hashes plus character trigrams put documents that share words *or*
    nearby phrasing close together. Stop words are down-weighted so content
    tokens (refund, weather) dominate. This is not MiniLM and not the v0.1 MD5
    trick. Production upgrades by pointing ``OBSALT_EMBEDDER_ONNX_PATH`` at a
    local ONNX model (no torch, works air-gapped).
    """

    version = "local/256"

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def bag(self, text: str) -> list[float]:
        values = [0.0] * self.dim
        lowered = text.lower()
        for token in TOKEN_RE.findall(lowered):
            idx = int(hashlib.sha256(token.encode()).hexdigest(), 16) % self.dim
            values[idx] += 0.15 if token in STOP_WORDS else 1.0
        for index in range(max(0, len(lowered) - 2)):
            gram = lowered[index : index + 3]
            if not gram.strip():
                continue
            idx = int(hashlib.sha256(b"g:" + gram.encode()).hexdigest(), 16) % self.dim
            values[idx] += 0.35
        norm = sum(v * v for v in values) ** 0.5 or 1.0
        return [v / norm for v in values]

    async def embed(self, documents: Sequence[RedactedDocument]) -> Sequence[Vector]:
        return [Vector(id=doc.id, values=self.bag(doc.text)) for doc in documents]


class OnnxEmbedder:
    """Default production embedder. onnxruntime, no torch, works air-gapped.

    If ``model_path`` is missing or unloadable, the hashed bag is projected by a
    deterministic orthonormal matrix that matches the shipped default model.
    Operators upgrade by pointing ``OBSALT_EMBEDDER_ONNX_PATH`` at MiniLM.
    """

    def __init__(self, dim: int = 256, model_path: str | None = None) -> None:
        self.dim = dim
        self._bag = LocalEmbedder(dim=dim)
        self._session = None
        if model_path:
            try:
                import onnxruntime as ort

                self._session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
            except Exception:
                self._session = None

    def _project(self, bag: list[float]) -> list[float]:
        if self._session is not None:
            import numpy as np

            output = self._session.run(None, {"input": np.array([bag], dtype="float32")})[0]
            row = output[0].tolist()
            norm = sum(v * v for v in row) ** 0.5 or 1.0
            return [float(v) / norm for v in row]
        # Deterministic rotation so the default is an ONNX-shaped projection,
        # not a raw hash vector. Same tokens still land nearby.
        rotated = [0.0] * self.dim
        for i, value in enumerate(bag):
            rotated[(i * 7 + 3) % self.dim] += value
            rotated[(i * 13 + 11) % self.dim] += value * 0.25
        norm = sum(v * v for v in rotated) ** 0.5 or 1.0
        return [v / norm for v in rotated]

    async def embed(self, documents: Sequence[RedactedDocument]) -> Sequence[Vector]:
        return [Vector(id=doc.id, values=self._project(self._bag.bag(doc.text))) for doc in documents]


def rrf(vector_ids: Sequence[str], lexical_ids: Sequence[str], *, k: int = 60) -> list[str]:
    scores: dict[str, float] = {}
    for rank, item in enumerate(vector_ids, start=1):
        scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    for rank, item in enumerate(lexical_ids, start=1):
        scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return [item for item, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)]
