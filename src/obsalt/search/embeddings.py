from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence

TOKEN_RE = re.compile(r"[a-z0-9]+")

# Light inflection folding so "refunds" matches "refund".
_SUFFIXES = ("ing", "ed", "es", "s")


def stem(token: str) -> str:
    if len(token) <= 4:
        return token
    for suffix in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: -len(suffix)]
    return token


def tokenize(text: str) -> list[str]:
    return [stem(tok) for tok in TOKEN_RE.findall(text.lower())]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / math.sqrt(na * nb)


class Embedder:
    dim: int

    def embed(self, text: str) -> list[float]:
        raise NotImplementedError


class HashingEmbedder(Embedder):
    """Deterministic hashing-trick embedder.

    No model download, stable across processes, good enough for tests and for
    bootstrapping semantic search before swapping in OpenAI/local encoders.
    """

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        tokens = tokenize(text)
        if not tokens:
            return vec
        grams = list(tokens)
        grams.extend(f"{a}_{b}" for a, b in zip(tokens, tokens[1:]))
        for gram in grams:
            digest = hashlib.md5(gram.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "little") % self.dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]


class OpenAICompatEmbedder(Embedder):
    """Optional production embedder. Tests never instantiate this without a stub."""

    def __init__(self, client, model: str = "text-embedding-3-small", dim: int = 1536) -> None:
        self.client = client
        self.model = model
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        response = self.client.embeddings.create(model=self.model, input=text)
        return list(response.data[0].embedding)
