"""Local encoder extra. No network; torch is not required."""

from __future__ import annotations

from datetime import UTC, datetime

from obsalt.analysis.groundedness import (
    DEFAULT_MODEL_ID,
    maybe_run_groundedness,
    model_allowed,
    run_groundedness,
)
from obsalt.analysis.runners import (
    get_policy,
    org_groundedness_enabled,
    org_groundedness_sample_rate,
    validate_policy,
)
from obsalt.config import Settings
from obsalt.domain.enums import HangupReason, Speaker
from obsalt.domain.models import CallRevision, EvalPolicy, Hangup, Turn
from obsalt.runtime import in_memory_state


def _call(**kwargs) -> CallRevision:
    base = dict(
        org_id="acme",
        call_id="c-ground",
        revision="r1",
        source="example",
        source_call_id="s1",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        hangup=Hangup(reason=HangupReason.COMPLETED),
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="The total is $48.50.")],
    )
    base.update(kwargs)
    return CallRevision(**base)


def test_model_allowlist() -> None:
    assert model_allowed(DEFAULT_MODEL_ID) is True
    assert model_allowed("KRLabsOrg/lettucedect-v2-mmbert-base") is True
    assert model_allowed("vectara/hallucination_evaluation_model") is False
    assert model_allowed("/models/lettucedect-v2-mmbert-base") is True


def test_missing_extra_is_not_judged() -> None:
    result = run_groundedness(_call(), settings=Settings())
    assert result.execution.analyzer_id == "groundedness"
    assert result.execution.analyzer_version == "1"
    assert result.payload["status"] == "not_judged"
    assert result.payload["spans"] == []
    assert "confirmed" not in result.payload


def test_mocked_predict_emits_spans() -> None:
    def predict(_context: str, answer: str) -> list[dict]:
        start = answer.find("$48.50")
        return [{"start": start, "end": start + 6, "confidence": 0.9}]

    result = run_groundedness(_call(), settings=Settings(), predict_turn=predict)
    assert result.payload["status"] == "completed"
    assert result.payload["spans"][0]["text"] == "$48.50"
    assert result.payload["spans"][0]["turn_index"] == 0


def test_out_of_range_span_is_dropped() -> None:
    def predict(_context: str, answer: str) -> list[dict]:
        return [{"start": 0, "end": len(answer) + 9, "confidence": 0.9}]

    result = run_groundedness(_call(), settings=Settings(), predict_turn=predict)
    assert result.payload["spans"] == []


def test_disallowed_model_is_not_judged() -> None:
    settings = Settings(groundedness_model="evil/not-allowlisted")
    result = run_groundedness(_call(), settings=settings, predict_turn=lambda *_: [])
    assert result.payload["status"] == "not_judged"


def test_sample_rate_zero_skips_row() -> None:
    settings = Settings(groundedness_enabled=True, groundedness_sample_rate=0)
    state = in_memory_state(settings)
    assert org_groundedness_enabled(state, "dev") is True
    assert org_groundedness_sample_rate(state, "dev") == 0
    assert maybe_run_groundedness(_call(), state) is None


def test_get_policy_falls_back_to_groundedness_env() -> None:
    settings = Settings(groundedness_enabled=True, groundedness_sample_rate=0.01)
    state = in_memory_state(settings)
    policy = get_policy(state, "dev")
    assert policy.groundedness_enabled is True
    assert policy.groundedness_sample_rate == 0.01


def test_groundedness_sample_rate_out_of_range() -> None:
    import pytest

    with pytest.raises(ValueError, match="groundedness_sample_rate"):
        validate_policy(
            EvalPolicy(org_id="dev", groundedness_sample_rate=1.5),
            [],
        )
