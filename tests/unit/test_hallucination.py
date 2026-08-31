"""Small tests: deterministic claim pre-filter. Independent of the judge."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from obsalt.analysis.hallucination import (
    DETECTOR_VERSION,
    detect_claims,
    extract_candidate_claims,
    grounding_corpus,
    tool_effectively_failed,
)
from obsalt.analysis.quality_card import compose_quality_card
from obsalt.analysis.rollups import build_quality_rollup
from obsalt.analysis.tool_values import tool_scalar_values
from obsalt.assemble.promote import MemoryPointerStore
from obsalt.domain.enums import GroundingKind, HangupReason, Provenance, Speaker, ToolStatus
from obsalt.domain.models import (
    AnalysisExecution,
    AnalysisResult,
    CallRevision,
    GroundingRef,
    Hangup,
    ToolInvocation,
    Turn,
)
from obsalt.plugin.types import RawEnvelope
from obsalt.ui.present import present_quality_card
from obsalt.util import utcnow
from obsalt.worker.process import MemoryRevisionSink, process_normalized_events
from obsalt_vapi.plugin import VapiPlugin


def _call(**kwargs) -> CallRevision:
    base = dict(
        org_id="acme",
        call_id="c1",
        revision="r1",
        source="example",
        source_call_id="s1",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        hangup=Hangup(reason=HangupReason.COMPLETED),
        turns=[],
        tools=[],
        grounding=[],
    )
    base.update(kwargs)
    return CallRevision(**base)


def test_ungrounded_price_and_id_are_candidates() -> None:
    call = _call(
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="I refunded $48.50 for order ORD-99999.")]
    )
    claims = extract_candidate_claims(call)
    kinds = {item["kind"] for item in claims}
    assert "price_claim" in kinds
    assert "fabricated_id" in kinds
    assert all(item.get("evidence_need") for item in claims)
    assert all(item.get("agent_span") for item in claims)


def test_extract_does_not_prefilter_on_grounding_blob() -> None:
    call = _call(
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="Order ORD-100 is $12.00")],
        grounding=[
            GroundingRef(
                kind=GroundingKind.KNOWLEDGE,
                content="Order ORD-100 is $12.00",
                content_ref="g1",
                provenance=Provenance.PROVIDER_REPORTED,
            )
        ],
    )
    kinds = {item["kind"] for item in extract_candidate_claims(call)}
    assert "price_claim" in kinds
    assert "fabricated_id" in kinds
    detected = detect_claims(call)
    assert detected
    assert all(item["verdict"] == "grounded" for item in detected)


def test_commitment_without_a_tool_is_extracted() -> None:
    call = _call(turns=[Turn(index=0, speaker=Speaker.AGENT, text="I've booked that for Tuesday.")])
    claims = extract_candidate_claims(call)
    assert any(item["kind"] == "commitment" for item in claims)
    detected = detect_claims(call)
    assert any(
        item["kind"] == "commitment" and item["verdict"] == "evidence_missing" for item in detected
    )


def test_failed_tool_plus_success_claim_is_phantom_on_detect() -> None:
    call = _call(
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="I've booked that for Tuesday.")],
        tools=[ToolInvocation(id="t1", name="book_appointment", status=ToolStatus.ERROR)],
    )
    kinds = {item["kind"] for item in extract_candidate_claims(call)}
    assert "phantom_tool_success" not in kinds
    assert "commitment" in kinds
    detected = detect_claims(call)
    phantom = next(item for item in detected if item["kind"] == "phantom_tool_success")
    assert phantom["verdict"] == "contradicted"
    assert phantom["binding"]["rule"] == "unique_on_call"


def test_successful_bound_tool_is_not_a_phantom() -> None:
    call = _call(
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="I've booked that for Tuesday.")],
        tools=[ToolInvocation(id="t1", name="book_appointment", status=ToolStatus.SUCCESS)],
    )
    assert any(item["kind"] == "commitment" for item in extract_candidate_claims(call))
    assert detect_claims(call) == []


def test_looked_up_is_not_a_success_claim() -> None:
    call = _call(
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="I've looked up your order.")],
        tools=[ToolInvocation(id="t1", name="lookup_order", status=ToolStatus.ERROR)],
    )
    assert extract_candidate_claims(call) == []
    assert detect_claims(call) == []


def test_grounding_corpus_includes_tool_errors() -> None:
    call = _call(
        turns=[Turn(index=0, speaker=Speaker.USER, text="refund please")],
        grounding=[
            GroundingRef(
                kind=GroundingKind.SYSTEM_PROMPT,
                content="Be honest",
                content_ref="p1",
                provenance=Provenance.PROVIDER_REPORTED,
            )
        ],
        tools=[
            ToolInvocation(id="t1", name="lookup_order", status=ToolStatus.ERROR, error="not_found")
        ],
    )
    corpus = grounding_corpus(call)
    assert "refund please" in corpus
    assert "Be honest" in corpus
    assert any("not_found" in item and "lookup_order" in item for item in corpus)
    assert extract_candidate_claims(call) == []


def test_empty_grounding_is_evidence_missing_not_a_hallucination() -> None:
    call = _call(
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="I refunded $48.50 for order ORD-99999.")]
    )
    detected = detect_claims(call)
    assert detected
    assert all(item["verdict"] == "evidence_missing" for item in detected)
    assert all(item["needs_llm"] is False for item in detected)


def test_prompt_only_grounding_is_evidence_missing() -> None:
    call = _call(
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="The total is $48.50.")],
        grounding=[
            GroundingRef(
                kind=GroundingKind.SYSTEM_PROMPT,
                content="Always quote $48.50",
                content_ref="p1",
                provenance=Provenance.PROVIDER_REPORTED,
            )
        ],
    )
    detected = detect_claims(call)
    assert detected
    assert all(item["verdict"] == "evidence_missing" for item in detected)


def test_success_status_with_not_found_body_is_a_failed_tool() -> None:
    tool = ToolInvocation(
        id="t1",
        name="lookup_order",
        status=ToolStatus.SUCCESS,
        result='{"error": "not_found"}',
    )
    assert tool_effectively_failed(tool) is True
    call = _call(
        turns=[
            Turn(
                index=0,
                speaker=Speaker.AGENT,
                text="I've processed a refund for order ORD-99999 totaling $48.50.",
            )
        ],
        tools=[tool],
    )
    detected = detect_claims(call)
    kinds = {item["kind"] for item in detected}
    assert "phantom_tool_success" in kinds
    phantom = next(item for item in detected if item["kind"] == "phantom_tool_success")
    assert phantom["verdict"] == "contradicted"
    assert phantom["model"] == DETECTOR_VERSION
    assert phantom["needs_llm"] is False
    prices = [item for item in detected if item["kind"] == "price_claim"]
    assert prices
    assert all(item["verdict"] == "contradicted" for item in prices)
    assert all(item["severity"] == "critical" for item in prices)
    assert phantom["severity"] == "high"


def test_spoken_fifty_vs_count_is_not_grounded() -> None:
    call = _call(
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="The total is $50.00.")],
        tools=[
            ToolInvocation(
                id="t1",
                name="list_orders",
                status=ToolStatus.SUCCESS,
                result='{"count": 50}',
            )
        ],
    )
    prices = [item for item in detect_claims(call) if item["kind"] == "price_claim"]
    assert prices
    assert all(item["verdict"] == "contradicted" for item in prices)


def test_integer_without_currency_is_not_extracted() -> None:
    call = _call(turns=[Turn(index=0, speaker=Speaker.AGENT, text="I can refund 50 today.")])
    assert extract_candidate_claims(call) == []


def test_decimal_near_total_is_extracted() -> None:
    call = _call(turns=[Turn(index=0, speaker=Speaker.AGENT, text="The total is 48.5.")])
    claims = extract_candidate_claims(call)
    assert any(item["agent_span"] == "48.5" for item in claims)


def test_48_does_not_match_148() -> None:
    call = _call(
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="That will be $48.00.")],
        tools=[
            ToolInvocation(
                id="t1",
                name="quote",
                status=ToolStatus.SUCCESS,
                result='{"total": 148.00}',
            )
        ],
    )
    prices = [item for item in detect_claims(call) if item["kind"] == "price_claim"]
    assert prices
    assert all(item["verdict"] == "contradicted" for item in prices)


def test_id_100_does_not_match_ord_100() -> None:
    call = _call(
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="Your order ORD-100 is ready.")],
        tools=[
            ToolInvocation(
                id="t1",
                name="list",
                status=ToolStatus.SUCCESS,
                result='{"count": 100}',
            )
        ],
    )
    ids = [item for item in detect_claims(call) if item["kind"] == "fabricated_id"]
    assert ids
    assert all(item["verdict"] == "contradicted" for item in ids)


def test_48_5_grounds_against_money_like_total() -> None:
    call = _call(
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="The total is 48.5.")],
        tools=[
            ToolInvocation(
                id="t1",
                name="quote",
                status=ToolStatus.SUCCESS,
                result='{"total": 48.50}',
            )
        ],
    )
    prices = [item for item in detect_claims(call) if item["kind"] == "price_claim"]
    assert prices
    assert all(item["verdict"] == "grounded" for item in prices)


def test_spoken_money_vs_not_found_is_contradicted() -> None:
    call = _call(
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="That's $48.50.")],
        tools=[
            ToolInvocation(
                id="t1",
                name="lookup_order",
                status=ToolStatus.SUCCESS,
                result='{"error": "not_found"}',
            )
        ],
    )
    prices = [item for item in detect_claims(call) if item["kind"] == "price_claim"]
    assert prices
    assert all(item["verdict"] == "contradicted" for item in prices)


def test_count_scalar_is_not_money_like() -> None:
    tool = ToolInvocation(id="t1", name="list", status=ToolStatus.SUCCESS, result='{"count": 50}')
    scalars = tool_scalar_values(tool)
    assert any(scalar.path.endswith("count") and not scalar.money_like for scalar in scalars)


def test_honest_not_found_is_not_a_phantom() -> None:
    call = _call(
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="I could not find that order.")],
        tools=[
            ToolInvocation(
                id="t1",
                name="lookup_order",
                status=ToolStatus.SUCCESS,
                result='{"error": "not_found"}',
            )
        ],
    )
    assert detect_claims(call) == []
    card = compose_quality_card(call)
    assert card["flag_kinds"] == []
    assert card["critical_failure"] is False
    assert present_quality_card(card) == []


def test_three_unindexed_tools_are_candidates_not_confirmed() -> None:
    call = _call(
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="I've processed a refund.")],
        tools=[
            ToolInvocation(id="t1", name="a", status=ToolStatus.ERROR),
            ToolInvocation(id="t2", name="b", status=ToolStatus.ERROR),
            ToolInvocation(id="t3", name="c", status=ToolStatus.ERROR),
        ],
    )
    detected = detect_claims(call)
    assert len(detected) == 1
    assert detected[0]["kind"] == "commitment"
    assert detected[0]["verdict"] == "needs_review"
    assert detected[0]["needs_llm"] is False


def test_empty_grounding_severity_is_needs_review() -> None:
    call = _call(
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="I refunded $48.50 for order ORD-99999.")]
    )
    detected = detect_claims(call)
    assert detected
    assert all(item["severity"] == "needs_review" for item in detected)


def test_mixed_confirmed_price_and_unbound_booking() -> None:
    from obsalt.analysis.hallucination import detector_payload
    from obsalt.domain.enums import AnalysisState

    call = _call(
        turns=[
            Turn(index=0, speaker=Speaker.AGENT, text="That's $48.50. I've booked Tuesday."),
        ],
        tools=[
            ToolInvocation(
                id="t1",
                name="lookup_order",
                status=ToolStatus.SUCCESS,
                result='{"error": "not_found"}',
            ),
            ToolInvocation(id="t2", name="calendar", status=ToolStatus.ERROR),
            ToolInvocation(id="t3", name="notes", status=ToolStatus.SUCCESS),
        ],
    )
    detected = detect_claims(call)
    prices = [item for item in detected if item["kind"] == "price_claim"]
    bookings = [item for item in detected if item["kind"] == "commitment"]
    assert prices and all(item["verdict"] == "contradicted" for item in prices)
    assert bookings and all(item["verdict"] == "needs_review" for item in bookings)
    payload = detector_payload(detected)
    assert payload["claims"]
    assert payload["candidates"]
    result = AnalysisResult(
        execution=AnalysisExecution(
            call_id=call.call_id,
            revision=call.revision,
            analyzer_id="hallucination",
            analyzer_version="2",
            state=AnalysisState.COMPLETED,
            judge_version=DETECTOR_VERSION,
        ),
        payload=payload,
    )
    roll = build_quality_rollup([call], [result], "g1")
    assert roll["hallucinations"]["count"] == 1
    assert roll["hallucinations"]["candidate_count"] == 1
    from obsalt.api import _ui_flags_and_evals

    flags, _evals, _card = _ui_flags_and_evals([result])
    kinds = {item.get("kind") for item in flags}
    assert "price_claim" in kinds
    assert "commitment" in kinds
    pending = next(item for item in flags if item.get("kind") == "commitment")
    assert pending.get("pending") is True


VAPI_FIXTURES = (
    Path(__file__).resolve().parents[2]
    / "packages"
    / "obsalt-vapi"
    / "src"
    / "obsalt_vapi"
    / "fixtures"
)


def test_vapi_end_of_call_fixture_confirms_price_id_and_phantom() -> None:
    plugin = VapiPlugin()
    body = json.loads((VAPI_FIXTURES / "raw" / "end_of_call.json").read_text())
    env = RawEnvelope(
        envelope_id="env-0",
        org_id="acme",
        provider="vapi",
        connection_id="c1",
        object_key="k",
        delivery_key="d",
        content_sha256="x",
        body=json.dumps(body).encode(),
        received_at=utcnow(),
    )
    events = list(plugin.decode(env))
    pointers = MemoryPointerStore()
    sink = MemoryRevisionSink()
    last = process_normalized_events(
        events,
        org_id="acme",
        source="vapi",
        source_call_id=body["message"]["call"]["id"],
        envelope_id="env-0",
        declaration=plugin.fidelity,
        pointers=pointers,
        sink=sink,
        decoder_version=plugin.decoder_version,
    )
    rev = sink.get("acme", last.call_id, pointers.get("acme", last.call_id))
    assert rev is not None
    detected = detect_claims(rev)
    kinds = {item["kind"] for item in detected}
    assert "price_claim" in kinds
    assert "fabricated_id" in kinds
    assert "phantom_tool_success" in kinds
    price = next(item for item in detected if item["kind"] == "price_claim")
    ident = next(item for item in detected if item["kind"] == "fabricated_id")
    phantom = next(item for item in detected if item["kind"] == "phantom_tool_success")
    assert price["verdict"] == "contradicted"
    assert ident["verdict"] == "contradicted"
    assert phantom["verdict"] == "contradicted"
    assert price["agent_span"] == "$48.50"
    assert ident["agent_span"] == "ORD-99999"
    assert phantom["binding"]["tool_id"]
    card = compose_quality_card(rev, claims=detected)
    assert card["critical_failure"] is True


def _tool(**kwargs) -> ToolInvocation:
    base = dict(id="t1", name="lookup_order", status=ToolStatus.SUCCESS)
    base.update(kwargs)
    return ToolInvocation(**base)


def test_honest_failure_report_is_dropped() -> None:
    call = _call(
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="I can't find your order.")],
        tools=[_tool(turn_index=0, result='{"error": "not_found"}')],
    )
    kinds = {item["kind"] for item in detect_claims(call)}
    assert "phantom_tool_failure" not in kinds


def test_denied_successful_tool_is_contradicted() -> None:
    call = _call(
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="I'm sorry, I can't find your order.")],
        tools=[
            _tool(
                turn_index=0,
                result='{"order_id": "ORD-1234", "status": "shipped"}',
            )
        ],
    )
    phantom = next(item for item in detect_claims(call) if item["kind"] == "phantom_tool_failure")
    assert phantom["verdict"] == "contradicted"
    assert phantom["severity"] == "high"
    assert phantom["model"] == DETECTOR_VERSION
    assert phantom["binding"]["tool_id"]


def test_failure_claim_without_a_bound_tool_is_needs_review() -> None:
    call = _call(
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="The system is down right now.")],
        tools=[_tool(result='{"status": "ok"}')],
    )
    item = next(item for item in detect_claims(call) if item["kind"] == "phantom_tool_failure")
    assert item["verdict"] == "needs_review"


def test_args_mismatch_placeholder_contradicts() -> None:
    call = _call(
        turns=[
            Turn(index=0, speaker=Speaker.USER, text="my order is ORD-1234"),
            Turn(index=1, speaker=Speaker.AGENT, text="Let me look that up."),
        ],
        tools=[_tool(turn_index=1, args={"order_id": "unknown"}, result="{}")],
    )
    mismatch = next(item for item in detect_claims(call) if item["kind"] == "args_mismatch")
    assert mismatch["verdict"] == "contradicted"
    assert mismatch["severity"] == "high"
    assert mismatch["evidence_spans"][0]["tool_name"] == "lookup_order"


def test_args_mismatch_requires_a_stated_id() -> None:
    call = _call(
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="Let me look that up.")],
        tools=[_tool(turn_index=0, args={"order_id": "unknown"}, result="{}")],
    )
    assert not [item for item in detect_claims(call) if item["kind"] == "args_mismatch"]


def test_args_mismatch_ignores_ambiguous_and_digitless_ids() -> None:
    ambiguous = _call(
        turns=[
            Turn(index=0, speaker=Speaker.USER, text="my order is ORD-1234"),
            Turn(index=1, speaker=Speaker.USER, text="and my booking is BK-99001"),
            Turn(index=2, speaker=Speaker.AGENT, text="Looking."),
        ],
        tools=[_tool(turn_index=2, args={"order_id": "unknown"}, result="{}")],
    )
    prose = _call(
        turns=[
            Turn(index=0, speaker=Speaker.USER, text="the case is closed"),
            Turn(index=1, speaker=Speaker.AGENT, text="Understood."),
        ],
        tools=[_tool(name="close_case", turn_index=1, args={"case_id": "unknown"}, result="{}")],
    )
    for call in (ambiguous, prose):
        assert not [item for item in detect_claims(call) if item["kind"] == "args_mismatch"]


def test_private_knowledge_echo_of_caller_data_is_grounded() -> None:
    call = _call(
        turns=[
            Turn(index=0, speaker=Speaker.USER, text="my email is john.doe@acme.com"),
            Turn(index=1, speaker=Speaker.AGENT, text="Confirming john.doe@acme.com."),
        ],
    )
    item = next(item for item in detect_claims(call) if item["kind"] == "private_knowledge")
    assert item["verdict"] == "grounded"
    assert item["severity"] == "low"


def test_private_knowledge_misquote_is_contradicted() -> None:
    call = _call(
        turns=[
            Turn(index=0, speaker=Speaker.USER, text="my email is john.doe@acme.com"),
            Turn(
                index=1, speaker=Speaker.AGENT, text="Your email john.doe1@acme.com is confirmed."
            ),
        ],
    )
    item = next(item for item in detect_claims(call) if item["kind"] == "private_knowledge")
    assert item["verdict"] == "contradicted"
    assert item["severity"] == "critical"


def test_private_knowledge_without_source_stays_needs_review() -> None:
    call = _call(
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="I see jane.smith@other.org on file.")]
    )
    item = next(item for item in detect_claims(call) if item["kind"] == "private_knowledge")
    assert item["verdict"] == "needs_review"
    assert item["severity"] == "needs_review"
    assert item["needs_llm"] is False


def test_phone_echo_grounds_and_near_miss_contradicts() -> None:
    echo = _call(
        turns=[
            Turn(index=0, speaker=Speaker.USER, text="call me at 555 201 8890"),
            Turn(index=1, speaker=Speaker.AGENT, text="I will call 555 201 8890."),
        ],
    )
    grounded = next(item for item in detect_claims(echo) if item["kind"] == "private_knowledge")
    assert grounded["verdict"] == "grounded"
    wrong = _call(
        turns=[
            Turn(index=0, speaker=Speaker.USER, text="call me at 555 201 8890"),
            Turn(index=1, speaker=Speaker.AGENT, text="I will call 555 201 8891."),
        ],
    )
    near = next(item for item in detect_claims(wrong) if item["kind"] == "private_knowledge")
    assert near["verdict"] == "contradicted"


def test_grounding_tool_date_grounds_date_and_time() -> None:
    call = _call(
        started_at=datetime(2026, 8, 26, 1, 8, 17, tzinfo=UTC),
        turns=[
            Turn(
                index=0,
                speaker=Speaker.AGENT,
                text="Your appointment is on September 3 at 3pm.",
                started_at=datetime(2026, 8, 26, 1, 8, 30, tzinfo=UTC),
            )
        ],
        tools=[_tool(name="get_slots", result='{"appointment": "2026-09-03T15:00:00Z"}')],
    )
    claims = [item for item in detect_claims(call) if item["kind"] == "date_time_claim"]
    assert {item["verdict"] for item in claims} == {"grounded"}


def test_contradicted_date_against_tool_result() -> None:
    call = _call(
        started_at=datetime(2026, 8, 26, 1, 8, 17, tzinfo=UTC),
        turns=[
            Turn(
                index=0,
                speaker=Speaker.AGENT,
                text="Your appointment is on September 3 at 3pm.",
                started_at=datetime(2026, 8, 26, 1, 8, 30, tzinfo=UTC),
            )
        ],
        tools=[_tool(name="get_slots", result='{"scheduled_for": "2026-09-04T15:00:00Z"}')],
    )
    claims = [item for item in detect_claims(call) if item["kind"] == "date_time_claim"]
    assert {item["verdict"] for item in claims} == {"contradicted"}


def test_policy_time_without_date_evidence_is_never_contradicted() -> None:
    call = _call(
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="We close at 6pm every day.")],
        tools=[_tool(name="get_slots", result='{"scheduled_for": "2026-09-04T15:00:00Z"}')],
    )
    claims = [item for item in detect_claims(call) if item["kind"] == "date_time_claim"]
    assert claims and claims[0]["verdict"] == "evidence_missing"


def test_count_claim_grounds_against_array_length() -> None:
    call = _call(
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="I see 3 items in your cart.")],
        tools=[_tool(name="get_cart", result='{"items": [1, 2, 3]}')],
    )
    item = next(item for item in detect_claims(call) if item["kind"] == "count_claim")
    assert item["verdict"] == "grounded"
    assert item["evidence_spans"][0]["path"] == "$.items.length"


def test_count_claim_contradicts_against_tool_count() -> None:
    call = _call(
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="I see 3 items in your cart.")],
        tools=[_tool(name="get_cart", result='{"item_count": 7}')],
    )
    item = next(item for item in detect_claims(call) if item["kind"] == "count_claim")
    assert item["verdict"] == "contradicted"


def test_parse_datetime_span_variants() -> None:
    from obsalt.analysis.tool_values import parse_datetime_span

    anchor = datetime(2026, 8, 26, 1, 8, 17, tzinfo=UTC)
    assert parse_datetime_span("August 30", anchor=anchor) == datetime(2026, 8, 30, tzinfo=UTC)
    assert parse_datetime_span("3pm", anchor=anchor) == datetime(2026, 8, 26, 15, 0, tzinfo=UTC)
    assert parse_datetime_span("at 3:30pm on Aug 30", anchor=anchor) == datetime(
        2026, 8, 30, 15, 30, tzinfo=UTC
    )
    assert parse_datetime_span("on 30th August 2027", anchor=anchor) == datetime(
        2027, 8, 30, tzinfo=UTC
    )
    assert parse_datetime_span("tomorrow", anchor=anchor) is None
