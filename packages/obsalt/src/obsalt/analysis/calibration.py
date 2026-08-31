"""Calibration keys and thresholds. Uncalibrated judges stay shadow (§9.6)."""

from __future__ import annotations

from collections.abc import Mapping

DEFAULT_AGREEMENT_THRESHOLD = 0.9


def calibration_key(org_id: str, rubric_id: str, version: int | str) -> str:
    return f"{org_id}:{rubric_id}:{version}"


def is_calibrated(
    store: Mapping[str, float] | None,
    org_id: str,
    rubric_id: str,
    version: int | str,
    *,
    threshold: float = DEFAULT_AGREEMENT_THRESHOLD,
) -> bool:
    if not store:
        return False
    agreement = store.get(calibration_key(org_id, rubric_id, version))
    return agreement is not None and float(agreement) >= threshold
