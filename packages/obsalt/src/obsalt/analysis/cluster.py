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


class MemoryHangupClusterStore:
    """Materialized hangup clusters. Refreshed on promotion, not per pageview."""

    def __init__(self) -> None:
        self.by_org: dict[str, dict[str, Any]] = {}

    def refresh(self, org_id: str, calls: Sequence[CallRevision], generation: str) -> dict[str, Any]:
        payload = cluster_hangups(calls, generation)
        self.by_org[org_id] = payload
        return payload

    def get(self, org_id: str, generation: str | None = None) -> dict[str, Any] | None:
        payload = self.by_org.get(org_id)
        if payload is None:
            return None
        if generation is not None and payload.get("as_of_generation") != generation:
            return None
        return payload


class ClickHouseHangupClusterStore(MemoryHangupClusterStore):
    """Persist scheduled cluster generations. Pageviews read the table, not re-embed."""

    def __init__(self, client: Any) -> None:
        super().__init__()
        self._client = client

    def refresh(self, org_id: str, calls: Sequence[CallRevision], generation: str) -> dict[str, Any]:
        payload = super().refresh(org_id, calls, generation)
        try:
            import json

            from obsalt.store.clickhouse import _ch_dt
            from obsalt.util import utcnow

            rows = [
                [
                    org_id,
                    generation,
                    row["id"],
                    row["reason"],
                    row["party"],
                    int(row["size"]),
                    row["top_call_id"],
                    json.dumps(row),
                    _ch_dt(utcnow()),
                ]
                for row in payload.get("clusters") or []
            ]
            if rows:
                self._client.insert(
                    "hangup_clusters",
                    rows,
                    column_names=[
                        "org_id",
                        "generation",
                        "cluster_id",
                        "reason",
                        "party",
                        "size",
                        "top_call_id",
                        "payload",
                        "created_at",
                    ],
                )
        except Exception:
            pass
        return payload

    def get(self, org_id: str, generation: str | None = None) -> dict[str, Any] | None:
        cached = super().get(org_id, generation)
        if cached is not None:
            return cached
        try:
            import json

            result = self._client.query(
                """
                SELECT generation, cluster_id, reason, party, size, top_call_id, payload
                FROM hangup_clusters
                WHERE org_id = {org:String}
                  AND ({gen:String} = '' OR generation = {gen:String})
                ORDER BY created_at DESC
                """,
                parameters={"org": org_id, "gen": generation or ""},
            )
        except Exception:
            return None
        if not getattr(result, "result_rows", None):
            return None
        clusters = []
        served_gen = generation or ""
        for gen, cluster_id, reason, party, size, top_call_id, payload in result.result_rows:
            served_gen = served_gen or str(gen)
            if generation and str(gen) != generation:
                continue
            parsed = {}
            if payload:
                if isinstance(payload, bytes):
                    payload = payload.decode("utf-8")
                try:
                    parsed = json.loads(payload) if isinstance(payload, str) else {}
                except json.JSONDecodeError:
                    parsed = {}
            clusters.append(
                parsed
                or {
                    "id": cluster_id,
                    "reason": reason,
                    "party": party,
                    "size": size,
                    "top_call_id": top_call_id,
                    "call_ids": [],
                }
            )
        if not clusters:
            return None
        payload = {
            "as_of_generation": served_gen,
            "clusters": clusters,
            "call_count": sum(int(row.get("size") or 0) for row in clusters),
            "note": "scheduled clustering of the active-revision set; not re-embedded per pageview",
        }
        self.by_org[org_id] = payload
        return payload


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
