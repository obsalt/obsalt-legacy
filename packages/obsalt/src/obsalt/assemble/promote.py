"""Postgres compare-and-swap of the active-revision pointer (§4.2)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from obsalt.domain.models import CallRevision


@dataclass
class PromotionResult:
    promoted: bool
    active_revision: str
    conflict: bool = False
    detail: str = ""


class RevisionPointerStore:
    """CAS store for (org_id, call_id) → active revision.

    Production uses Postgres. Tests inject an in-memory implementation of this
    protocol; that is not a second product storage backend.
    """

    def compare_and_swap(
        self,
        org_id: str,
        call_id: str,
        expected: str | None,
        candidate: str,
        *,
        fact_frontier: frozenset[str],
    ) -> bool:
        raise NotImplementedError

    def get(self, org_id: str, call_id: str) -> str | None:
        raise NotImplementedError

    def list_org(self, org_id: str) -> list[tuple[str, str]]:
        raise NotImplementedError

    def delete(self, org_id: str, call_id: str) -> None:
        raise NotImplementedError

    def frontier(self, org_id: str, call_id: str) -> frozenset[str]:
        return frozenset()


class MemoryPointerStore(RevisionPointerStore):
    def __init__(self) -> None:
        self._ptrs: dict[tuple[str, str], str] = {}
        self._frontiers: dict[tuple[str, str], frozenset[str]] = {}

    def compare_and_swap(
        self,
        org_id: str,
        call_id: str,
        expected: str | None,
        candidate: str,
        *,
        fact_frontier: frozenset[str],
    ) -> bool:
        key = (org_id, call_id)
        current = self._ptrs.get(key)
        if current != expected:
            return False
        self._ptrs[key] = candidate
        self._frontiers[key] = fact_frontier
        return True

    def get(self, org_id: str, call_id: str) -> str | None:
        return self._ptrs.get((org_id, call_id))

    def list_org(self, org_id: str) -> list[tuple[str, str]]:
        return [
            (call_id, revision) for (oid, call_id), revision in self._ptrs.items() if oid == org_id
        ]

    def delete(self, org_id: str, call_id: str) -> None:
        self._ptrs.pop((org_id, call_id), None)
        self._frontiers.pop((org_id, call_id), None)

    def frontier(self, org_id: str, call_id: str) -> frozenset[str]:
        return self._frontiers.get((org_id, call_id), frozenset())


def promote(
    store: RevisionPointerStore,
    candidate: CallRevision,
    *,
    expected: str | None,
    fact_frontier: frozenset[str],
    rebase: Callable[[CallRevision, str], CallRevision] | None = None,
    max_attempts: int = 8,
) -> PromotionResult:
    """CAS the pointer. On conflict, rebase onto the new active revision and retry.

    A candidate is not marked assembled until an active revision covers its fact
    frontier, so a concurrent winner cannot strand accepted facts.
    """
    attempt = candidate
    base = expected
    for _ in range(max_attempts):
        if attempt.conflicts:
            return PromotionResult(
                promoted=False,
                active_revision=store.get(attempt.org_id, attempt.call_id) or "",
                conflict=True,
                detail="fact conflicts block automatic promotion",
            )
        ok = store.compare_and_swap(
            attempt.org_id,
            attempt.call_id,
            base,
            attempt.revision,
            fact_frontier=fact_frontier,
        )
        if ok:
            return PromotionResult(promoted=True, active_revision=attempt.revision)
        current = store.get(attempt.org_id, attempt.call_id)
        if current is None:
            base = None
            continue
        if rebase is None:
            return PromotionResult(
                promoted=False,
                active_revision=current,
                conflict=True,
                detail="CAS lost and no rebase provided",
            )
        attempt = rebase(attempt, current)
        base = current
    return PromotionResult(
        promoted=False,
        active_revision=store.get(candidate.org_id, candidate.call_id) or "",
        conflict=True,
        detail="CAS retries exhausted",
    )
