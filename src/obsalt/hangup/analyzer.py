from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from obsalt.domain.enums import HangupReason
from obsalt.domain.models import CanonicalCall
from obsalt.hangup.taxonomy import customer_loss_score
from obsalt.search.embeddings import Embedder, HashingEmbedder, cosine


@dataclass
class HangupCluster:
    key: str
    reason: str
    party: str
    last_utterance_theme: str
    count: int
    avg_loss_score: float
    example_call_ids: list[str]
    lost_customer_call_id: str | None


def _cluster_key(reason: str, party: str, theme: str) -> str:
    return f"{reason}|{party}|{theme}"


def _theme_for(text: str, embedder: Embedder, centroids: list[tuple[str, list[float]]]) -> str:
    cleaned = " ".join(text.lower().split())
    if not cleaned:
        return "no_last_utterance"
    vector = embedder.embed(cleaned)
    best_name = "misc"
    best_sim = 0.42
    for name, centroid in centroids:
        sim = cosine(vector, centroid)
        if sim > best_sim:
            best_sim = sim
            best_name = name
    if best_name != "misc":
        return best_name
    # Greedy: start a new theme from the first 6 tokens when nothing matches.
    tokens = [t for t in cleaned.split() if len(t) > 2][:6]
    return " ".join(tokens) if tokens else "misc"


_SEED_THEMES = {
    "refund_billing": "refund money back charge billing invoice price",
    "speak_to_human": "speak to a human real person representative manager supervisor",
    "cancel": "cancel nevermind stop don't want",
    "wrong_person": "wrong number not me who is this",
    "confusion": "what do you mean I don't understand confused",
    "wait_hold": "still there hello waiting hold on",
}


class HangupAnalyzer:
    def __init__(self, embedder: Embedder | None = None) -> None:
        self.embedder = embedder or HashingEmbedder()
        self._centroids = [(name, self.embedder.embed(text)) for name, text in _SEED_THEMES.items()]

    def analyze_call(self, call: CanonicalCall) -> CanonicalCall:
        if call.hangup is None:
            return call
        score, reasons = customer_loss_score(call)
        call.hangup.loss_score = score
        call.hangup.loss_reasons = reasons
        return call

    def cluster(self, calls: list[CanonicalCall]) -> list[HangupCluster]:
        buckets: dict[str, list[CanonicalCall]] = defaultdict(list)
        themes: dict[str, str] = {}
        for call in calls:
            hangup = call.hangup
            if hangup is None:
                continue
            theme = _theme_for(hangup.last_user_text or "", self.embedder, self._centroids)
            key = _cluster_key(hangup.reason.value, hangup.party.value, theme)
            buckets[key].append(call)
            themes[key] = theme

        clusters: list[HangupCluster] = []
        for key, members in buckets.items():
            reason, party, _ = key.split("|", 2)
            scores = [m.hangup.loss_score if m.hangup else 0.0 for m in members]
            ranked = sorted(members, key=lambda c: (c.hangup.loss_score if c.hangup else 0.0), reverse=True)
            lost = next(
                (
                    c
                    for c in ranked
                    if c.hangup
                    and c.hangup.reason == HangupReason.USER_HANGUP
                    and c.hangup.loss_score >= 0.5
                ),
                ranked[0] if ranked else None,
            )
            clusters.append(
                HangupCluster(
                    key=key,
                    reason=reason,
                    party=party,
                    last_utterance_theme=themes[key],
                    count=len(members),
                    avg_loss_score=round(sum(scores) / len(scores), 3) if scores else 0.0,
                    example_call_ids=[c.id for c in ranked[:5]],
                    lost_customer_call_id=lost.id if lost else None,
                )
            )
        clusters.sort(key=lambda c: (c.avg_loss_score, c.count), reverse=True)
        return clusters
