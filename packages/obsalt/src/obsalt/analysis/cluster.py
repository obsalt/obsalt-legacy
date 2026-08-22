"""Scheduled-style hangup clustering.

Group the provided (already-active) calls by hangup.reason and hangup.party.
Optional LocalEmbedder vectors of last user text split a reason/party group
into semantic subclusters. Deterministic: sorted call ids, first-member centroid.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from obsalt.domain.enums import HangupParty, HangupReason, Speaker
from obsalt.domain.models import CallRevision
from obsalt.plugin.types import RedactedDocument
from obsalt.search.hybrid import LocalEmbedder


def cluster_hangups(
    calls: Sequence[CallRevision],
    as_of_generation: str = "",
    *,
    embedder: LocalEmbedder | None = None,
    similarity_threshold: float = 0.3,
) -> dict[str, Any]:
    """Return clusters with drill-through call ids. One generation per response."""
    grouped: dict[tuple[str, str], list[CallRevision]] = defaultdict(list)
    for call in sorted(calls, key=lambda item: item.call_id):
        reason, party = _reason_party(call)
        grouped[(reason, party)].append(call)

    clusters: list[dict[str, Any]] = []
    for (reason, party), members in sorted(grouped.items()):
        subclusters = (
            _embed_subclusters(members, embedder, similarity_threshold)
            if embedder is not None
            else [members]
        )
        for index, subset in enumerate(subclusters):
            ranked = sorted(subset, key=lambda call: (-_loss(call), call.call_id))
            cluster_id = f"{reason}:{party}" if embedder is None else f"{reason}:{party}:{index:02d}"
            top = ranked[0]
            clusters.append(
                {
                    "id": cluster_id,
                    "reason": reason,
                    "party": party,
                    "size": len(ranked),
                    "call_ids": [call.call_id for call in ranked],
                    "top_call_id": top.call_id,
                    "max_loss_score": _loss(top),
                    "loss_reasons": list(top.hangup.loss_reasons) if top.hangup else [],
                    "last_user_text": _last_user_text(top),
                }
            )

    clusters.sort(key=lambda row: (-row["size"], row["reason"], row["party"], row["id"]))
    return {
        "as_of_generation": as_of_generation,
        "clusters": clusters,
        "call_count": len(calls),
        "note": "scheduled clustering of the active-revision set; not re-embedded per pageview",
    }


def _reason_party(call: CallRevision) -> tuple[str, str]:
    if call.hangup is None:
        return HangupReason.UNKNOWN.value, HangupParty.UNKNOWN.value
    return call.hangup.reason.value, call.hangup.party.value


def _loss(call: CallRevision) -> float:
    if call.hangup is None:
        return 0.0
    return float(call.hangup.loss_score)


def _last_user_text(call: CallRevision) -> str:
    for turn in reversed(call.turns):
        if turn.speaker is Speaker.USER:
            return turn.text
    return ""


def _embed_subclusters(
    members: Sequence[CallRevision],
    embedder: LocalEmbedder,
    similarity_threshold: float,
) -> list[list[CallRevision]]:
    texts = [_last_user_text(call) for call in members]
    vectors = _embed_texts(embedder, texts)
    empty: list[CallRevision] = []
    buckets: list[tuple[list[float], list[CallRevision]]] = []
    for call, text, vector in zip(members, texts, vectors, strict=True):
        if not text.strip():
            empty.append(call)
            continue
        placed = False
        for centroid, bucket in buckets:
            if _dot(centroid, vector) >= similarity_threshold:
                bucket.append(call)
                placed = True
                break
        if not placed:
            buckets.append((vector, [call]))
    result = [bucket for _centroid, bucket in buckets]
    if empty:
        result.append(empty)
    return result or [list(members)]


def _embed_texts(embedder: LocalEmbedder, texts: Sequence[str]) -> list[list[float]]:
    docs = [RedactedDocument(id=str(index), text=text) for index, text in enumerate(texts)]
    vectors = asyncio.run(embedder.embed(docs))
    return [list(item.values) for item in vectors]


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    return float(sum(a * b for a, b in zip(left, right, strict=True)))
