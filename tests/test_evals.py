from __future__ import annotations

from obsalt.domain.enums import HallucinationKind, HangupParty, HangupReason, LatencyComponent, Provider, Speaker
from obsalt.domain.models import CanonicalCall, EvalResult, HallucinationFlag, Hangup, LatencySample, Rubric, Turn
from obsalt.evals.judges import HeuristicJudge, default_rubrics
from obsalt.pipeline import IngestPipeline
from obsalt.store import MemoryStore
from obsalt.util import call_id_for
from tests.conftest import load_fixture


def _call() -> CanonicalCall:
    return CanonicalCall(
        id=call_id_for("org", "vapi", "e1"),
        org_id="org",
        provider=Provider.VAPI,
        provider_call_id="e1",
        agent_id="support",
        hangup=Hangup(
            reason=HangupReason.USER_HANGUP,
            party=HangupParty.USER,
            provider_reason="customer-ended-call",
            last_user_text="This is useless",
            loss_score=0.8,
        ),
        hallucinations=[
            HallucinationFlag(
                kind=HallucinationKind.PRICE_CLAIM,
                span_text="$48.50",
                turn_index=1,
                confidence=0.9,
                rationale="ungrounded",
            )
        ],
        latency_samples=[LatencySample(component=LatencyComponent.TTFA, duration_ms=1600)],
        turns=[Turn(index=0, speaker=Speaker.USER, text="This is useless")],
    )


def test_heuristic_judge_reads_plain_english_rubrics() -> None:
    judge = HeuristicJudge()
    call = _call()
    grounded = Rubric(id="g", org_id="org", name="g", description="Flag hallucinations and invented prices.", threshold=0.7)
    latency = Rubric(id="l", org_id="org", name="l", description="Stay fast. Latency over 1500ms fails.", threshold=0.7)
    kept = Rubric(id="k", org_id="org", name="k", description="Do not lose a frustrated customer.", threshold=0.7)
    assert judge.evaluate(call, grounded).passed is False
    assert judge.evaluate(call, latency).passed is False
    assert judge.evaluate(call, kept).passed is False


def test_pipeline_runs_default_evals_on_ingest() -> None:
    store = MemoryStore()
    pipeline = IngestPipeline(store=store)
    result = pipeline.ingest(Provider.VAPI, load_fixture("vapi_end_of_call.json"), org_id="org")
    call = store.get_call("org", result.call_id)
    assert call is not None
    assert call.evals
    names = {e.rubric_name for e in call.evals}
    assert "Grounded claims" in names
    grounded = next(e for e in call.evals if e.rubric_name == "Grounded claims")
    assert grounded.passed is False


def test_custom_rubric_via_store() -> None:
    assert default_rubrics("org")[0].description
    result = HeuristicJudge().evaluate(
        _call(),
        Rubric(id="x", org_id="org", name="x", description="Be polite.", threshold=0.9),
    )
    assert isinstance(result, EvalResult)
