"""Local embedder is lexical, not the v0.1 MD5 trick, and clusters similar phrases."""

from __future__ import annotations

import asyncio

from obsalt.analysis.cluster import cluster_hangups
from obsalt.domain.enums import HangupParty, HangupReason, Speaker
from obsalt.domain.models import CallRevision, Hangup, Turn
from obsalt.plugin.types import RedactedDocument
from obsalt.search.hybrid import LocalEmbedder, rrf


def test_shared_tokens_are_nearer_than_unrelated_text() -> None:
    embedder = LocalEmbedder(dim=256)
    refund = embedder.bag("customer asking about refunds for order 100")
    refund_near = embedder.bag("I need a refund on my order")
    weather = embedder.bag("hello how is the weather today")

    def dot(left: list[float], right: list[float]) -> float:
        return sum(a * b for a, b in zip(left, right, strict=True))

    assert dot(refund, refund_near) > dot(refund, weather)


def test_async_embed_preserves_document_ids() -> None:
    embedder = LocalEmbedder(dim=32)
    vectors = asyncio.run(
        embedder.embed(
            [
                RedactedDocument(id="a", text="refund please"),
                RedactedDocument(id="b", text="weather today"),
            ]
        )
    )
    assert [item.id for item in vectors] == ["a", "b"]
    assert all(abs(sum(v * v for v in item.values) - 1.0) < 1e-6 for item in vectors)


def test_rrf_promotes_agreement() -> None:
    assert rrf(["a", "b"], ["b", "c"])[0] == "b"


def test_hangup_clusters_split_by_closing_utterance() -> None:
    refund = CallRevision(
        org_id="o",
        call_id="refund",
        revision="r1",
        source="vapi",
        source_call_id="a",
        hangup=Hangup(reason=HangupReason.USER_HANGUP, party=HangupParty.USER),
        turns=[Turn(index=0, speaker=Speaker.USER, text="I want a refund now")],
    )
    refund2 = CallRevision(
        org_id="o",
        call_id="refund-2",
        revision="r2",
        source="vapi",
        source_call_id="b",
        hangup=Hangup(reason=HangupReason.USER_HANGUP, party=HangupParty.USER),
        turns=[Turn(index=0, speaker=Speaker.USER, text="please process my refund")],
    )
    weather = CallRevision(
        org_id="o",
        call_id="weather",
        revision="r3",
        source="vapi",
        source_call_id="c",
        hangup=Hangup(reason=HangupReason.USER_HANGUP, party=HangupParty.USER),
        turns=[Turn(index=0, speaker=Speaker.USER, text="what is the weather tomorrow")],
    )
    payload = cluster_hangups([refund, refund2, weather], "g1", embedder=LocalEmbedder(), similarity_threshold=0.45)
    assert payload["as_of_generation"] == "g1"
    assert payload["call_count"] == 3
    assert all(":" in row["id"] for row in payload["clusters"])
    refund_cluster = next(row for row in payload["clusters"] if "refund" in row["call_ids"])
    assert "refund-2" in refund_cluster["call_ids"]
    assert "weather" not in refund_cluster["call_ids"]
