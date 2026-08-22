"""§4.2 promotion protocol. No cross-database transaction is pretended.

1. Write a complete immutable candidate to ClickHouse and verify visibility.
2. CAS the Postgres active-revision pointer. On conflict, rebase and retry.
3. Call-detail reads the pointer, then the exact revision.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Promotion:
    org_id: str
    call_id: str
    expected_revision: int | None
    candidate_revision: int
    fact_frontier: frozenset[str]


@dataclass(frozen=True, slots=True)
class PromotionResult:
    promoted: bool
    active_revision: int
    rebased: bool = False
    blocked: bool = False
    reason: str | None = None


def cas_pointer(current: int | None, expected: int | None, candidate: int) -> PromotionResult:
    if current is None and expected is None:
        return PromotionResult(promoted=True, active_revision=candidate)
    if current == expected:
        return PromotionResult(promoted=True, active_revision=candidate)
    return PromotionResult(
        promoted=False,
        active_revision=current if current is not None else 0,
        rebased=True,
        reason="active revision moved; rebase candidate onto the new frontier",
    )
