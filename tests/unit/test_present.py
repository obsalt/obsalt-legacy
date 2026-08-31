"""Small tests: console presentation labels facts; it does not invent them."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from obsalt.analysis.hangup import ENDING_NOT_REPORTED, ENDING_UNROOTED
from obsalt.assemble.timeline import timeline_view
from obsalt.domain.enums import (
    CallStatus,
    EvidenceKind,
    GroundingKind,
    HangupParty,
    HangupReason,
    MeasurementPlacement,
    Metric,
    Provenance,
    Speaker,
    Stage,
    TimelineFidelity,
    ToolStatus,
)
from obsalt.domain.models import (
    CallRevision,
    EvidenceRef,
    GroundingRef,
    Hangup,
    StageMeasurement,
    ToolInvocation,
    Turn,
)
from obsalt.ui.present import (
    agent_label,
    empty_calls,
    format_cost,
    groundedness_parts,
    hangup_label,
    hangup_label_for,
    present_call_detail,
    present_call_row,
    present_eval,
    present_flags,
    present_fleet,
    present_grounding,
    present_hangups,
    present_health,
    present_latency,
    present_quality,
    present_quality_card,
    present_quality_card_axes,
    present_recording,
    present_timeline,
    present_tool,
    speaker_label,
    stage_label,
)


def _call(**overrides: object) -> CallRevision:
    t0 = datetime(2026, 8, 22, 12, 0, 1, tzinfo=UTC)
    base = dict(
        org_id="o",
        call_id="call-refund-1",
        revision="r1",
        source="example",
        source_call_id="ex-1",
        agent_id="support",
        started_at=t0,
        ended_at=t0 + timedelta(seconds=12),
        duration_ms=12_000,
        timeline_fidelity=TimelineFidelity.TURN_LEVEL,
        hangup=Hangup(
            reason=HangupReason.USER_HANGUP,
            party=HangupParty.USER,
            loss_score=0.7,
            loss_reasons=["user_hangup", "negative_last_utterance"],
        ),
        turns=[
            Turn(index=0, speaker=Speaker.USER, text="I want a refund.", started_at=t0),
            Turn(
                index=1,
                speaker=Speaker.AGENT,
                text="I can help with that.",
                started_at=t0 + timedelta(seconds=2),
            ),
        ],
        stage_measurements=[
            StageMeasurement(
                fact_id="f1",
                stage=Stage.STT,
                metric=Metric.DURATION,
                value_ms=180,
                placement=MeasurementPlacement.UNPLACED,
                provenance=Provenance.PROVIDER_REPORTED,
            )
        ],
    )
    base.update(overrides)
    return CallRevision(**base)


def test_empty_calls_splits_unsigned_from_unconnected() -> None:
    unsigned = empty_calls(signed_in=False)
    assert unsigned["title"] == "Sign in to see live calls"
    unconnected = empty_calls(signed_in=True, has_connections=False, can_connect=True)
    assert unconnected["title"] == "Connect an agent"
    assert unconnected["cta_connect"]
    stuck = empty_calls(signed_in=True, has_connections=True, worker_stuck=True)
    assert "worker" in stuck["body"].lower() or "worker" in stuck["title"].lower()


def test_present_health_flags_a_dlq() -> None:
    live = present_health(
        plugins=["vapi"],
        insecure_defaults=False,
        dlq_depth=2,
        outbox_depth=3,
        environment="prod",
    )
    assert live["show"]
    assert "Decode is failing" in live["summary"]
    stale = present_health(
        plugins=["vapi"],
        insecure_defaults=False,
        dlq_depth=130,
        outbox_depth=0,
        environment="prod",
    )
    assert "earlier decode failures" in stale["summary"]
    assert "caught up" in stale["summary"]
    quiet = present_health(plugins=["vapi"], insecure_defaults=False, environment="prod")
    assert quiet["ok"]


def test_present_eval_strips_pack_prefix_and_labels_na() -> None:
    view = present_eval(
        {
            "analyzer_id": "pack:handoff",
            "passed": None,
            "verdict": "not_applicable",
            "state": "completed",
            "rationale": "no handoff on this call",
        }
    )
    assert view["label"] == "handoff"
    assert view["passed_label"] == "not applicable"


def test_hangup_label_is_the_product_sentence() -> None:
    assert hangup_label("user_hangup") == "Caller hung up"
    assert hangup_label(None) == "Ending not reported"
    assert hangup_label(ENDING_NOT_REPORTED) == "Ending not reported"
    assert hangup_label(ENDING_UNROOTED) == "Still assembling"
    assert hangup_label("unknown", provider_code="weird-code") == "Unmapped ending (weird-code)"
    assert hangup_label("unknown") == "Unmapped ending"
    assert hangup_label("error_unknown", provider_code="vapifault") == (
        "Unclassified platform error (vapifault)"
    )
    assert agent_label("unknown") == ""
    assert agent_label("support") == "support"
    assert speaker_label(None) == "Speaker not reported"
    assert stage_label(None) == "Stage not reported"


def test_missing_hangup_is_ending_not_reported_not_unknown() -> None:
    row = present_call_row(_call(hangup=None, agent_id="unknown"))
    assert row["hangup"] == "Ending not reported"
    assert row["hangup_reason"] == ENDING_NOT_REPORTED
    assert row["agent_id"] == ""
    unrooted = present_call_row(_call(hangup=None, rooted=False, status=CallStatus.UNROOTED))
    assert unrooted["hangup"] == "Still assembling"
    mapped = _call(
        hangup=Hangup(
            reason=HangupReason.UNKNOWN,
            party=HangupParty.UNKNOWN,
            provider_code="mystery-code",
        )
    )
    assert hangup_label_for(mapped) == "Unmapped ending (mystery-code)"


def test_pending_flags_are_warn_not_hidden() -> None:
    items = present_flags(
        [{"kind": "price_claim", "span_text": "$12", "needs_llm": True, "pending": True}]
    )
    assert items[0]["pending"] is True
    assert items[0]["tone"] == "warn"
    assert "not yet judged" in items[0]["label"]
    confirmed = present_flags(
        [{"kind": "price_claim", "verdict": "contradicted", "model": "detector/2"}]
    )
    assert confirmed[0]["tone"] == "loss"
    assert confirmed[0]["pending"] is False
    llm = present_flags(
        [{"kind": "price_claim", "verdict": "unsupported", "model": "gpt-4.1-mini"}]
    )
    assert llm[0]["tone"] == "warn"
    assert llm[0]["pending"] is True
    heuristic = present_flags(
        [
            {
                "kind": "price_claim",
                "verdict": "contradicted",
                "model": "heuristic/1",
                "span_text": "$48.50",
            }
        ]
    )
    assert heuristic[0]["pending"] is True
    assert heuristic[0]["tone"] == "warn"
    assert "heuristic, not confirmed" in heuristic[0]["label"]
    missing = present_flags([{"kind": "price_claim", "verdict": "evidence_missing"}])
    assert missing[0]["pending"] is True
    assert missing[0]["tone"] == "warn"
    assert "evidence missing" in missing[0]["label"]


def test_call_row_leads_with_last_words_and_hangup() -> None:
    row = present_call_row(_call())
    assert row["hangup"] == "Caller hung up"
    assert row["last_user"] == "I want a refund."
    assert row["call_id"] == "call-refund-1"
    assert "refund" in row["headline"]
    assert "Speech-to-text" in row["latency"]
    assert "180" in row["latency"]


def test_timeline_does_not_draw_waterfall_from_unplaced_durations() -> None:
    call = _call()
    view = present_timeline(call, timeline_view(call))
    assert view["draw_stage_waterfall"] is False
    assert view["stage_intervals"] == []
    assert view["unplaced_stage_chips"]
    assert view["unplaced_stage_chips"][0]["value_text"] == "180 ms"
    assert view["duration_rows"][0]["value_text"] == "180 ms"
    assert view["duration_rows"][0]["bar_pct"] > 0
    assert view["has_turn_bars"] is False


def test_timeline_waterfall_geometry_uses_real_clocks_only() -> None:
    t0 = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    call = _call(
        timeline_fidelity=TimelineFidelity.STAGE_LEVEL,
        stage_measurements=[
            StageMeasurement(
                fact_id="stt",
                stage=Stage.STT,
                metric=Metric.DURATION,
                value_ms=200,
                placement=MeasurementPlacement.INTERVAL,
                started_at=t0,
                ended_at=t0 + timedelta(milliseconds=200),
                provenance=Provenance.PROVIDER_REPORTED,
            ),
            StageMeasurement(
                fact_id="llm",
                stage=Stage.LLM,
                metric=Metric.DURATION,
                value_ms=800,
                placement=MeasurementPlacement.INTERVAL,
                started_at=t0 + timedelta(milliseconds=200),
                ended_at=t0 + timedelta(milliseconds=1000),
                provenance=Provenance.PROVIDER_REPORTED,
            ),
        ],
    )
    view = present_timeline(call, timeline_view(call))
    assert view["draw_stage_waterfall"] is True
    assert view["stage_intervals"][0]["left_pct"] == 0.0
    assert view["stage_intervals"][1]["width_pct"] > view["stage_intervals"][0]["width_pct"]


def test_timeline_turn_bars_use_clocks_not_unplaced_durations() -> None:
    t0 = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    call = _call(
        turns=[
            Turn(
                index=0,
                speaker=Speaker.USER,
                text="I want a refund.",
                started_at=t0,
                ended_at=t0 + timedelta(milliseconds=400),
            ),
            Turn(
                index=1,
                speaker=Speaker.AGENT,
                text="I can help with that.",
                started_at=t0 + timedelta(milliseconds=400),
                ended_at=t0 + timedelta(milliseconds=1400),
            ),
        ]
    )
    view = present_timeline(call, timeline_view(call))
    assert view["has_turn_bars"] is True
    assert view["turns"][0]["speaker_label"] == "Caller"
    assert view["turns"][1]["bar_pct"] > view["turns"][0]["bar_pct"]
    assert view["draw_stage_waterfall"] is False


def test_fleet_questions_point_at_the_six_capabilities() -> None:
    fleet = present_fleet([_call()])
    questions = [item["question"] for item in fleet["questions"]]
    assert "Why did we lose callers?" in questions
    assert "Where did time go?" in questions
    assert "Did the agent invent facts?" in questions
    assert fleet["top_hangup"] == "Caller hung up"
    hangup = next(item for item in fleet["questions"] if item["question"].startswith("Why"))
    assert "Caller hung up" in hangup["value"]
    assert "1 of 1" in hangup["value"]
    search = next(item for item in fleet["questions"] if item["kind"] == "door")
    assert search["value"] == ""
    invent = next(item for item in fleet["questions"] if "invent" in item["question"])
    assert invent["tone"] == "warn"
    tools = next(item for item in fleet["questions"] if "tools" in item["question"])
    assert tools["value"] == "No tools in this range"
    assert tools["tone"] == "warn"


def test_fleet_tools_tile_is_ok_when_invocations_exist_and_none_failed() -> None:
    fleet = present_fleet(
        [_call()],
        quality={"hallucinations": {"scanned": 3, "count": 0}},
        tools={"invocation_count": 2, "by_tool": {"lookup": {"failures": 0}}},
    )
    invent = next(item for item in fleet["questions"] if "invent" in item["question"])
    assert invent["tone"] == "ok"
    tools = next(item for item in fleet["questions"] if "tools" in item["question"])
    assert tools["value"] == "0"
    assert tools["tone"] == "ok"


def test_hangup_shares_are_reason_bars_not_essays() -> None:
    presented = present_hangups(
        {
            "clusters": [
                {
                    "reason": "user_hangup",
                    "size": 8,
                    "top_call_id": "call-refund-1",
                    "call_ids": ["call-refund-1"],
                    "last_speaker": "user",
                    "last_user_text": "I want a refund.",
                }
            ]
        },
        10,
    )
    assert presented["shares"][0]["label"] == "Caller hung up"
    assert presented["shares"][0]["bar_pct"] >= 4
    assert presented["clusters"][0]["top_short_id"] == "refund-1"
    assert presented["clusters"][0]["last_speaker_label"] == "Caller"


def test_latency_leads_with_aggregates_when_samples_are_empty() -> None:
    call = _call(stage_measurements=[])
    view = present_latency(
        {
            "sample_percentiles": {},
            "provider_aggregates": [
                {
                    "call_id": "call-refund-1",
                    "stage": "e2e",
                    "statistic": "p50",
                    "value_ms": 900,
                }
            ],
        },
        [call],
    )
    assert view["has_samples"] is False
    assert view["aggregates_primary"] is True
    assert view["provider_aggregates"][0]["value_text"] == "900 ms"


def test_hangup_cluster_joins_in_range_call_rows() -> None:
    call = _call()
    presented = present_hangups(
        {
            "clusters": [
                {
                    "reason": "user_hangup",
                    "size": 1,
                    "top_call_id": call.call_id,
                    "call_ids": [call.call_id],
                    "last_user_text": "I want a refund.",
                }
            ]
        },
        1,
        [call],
    )
    row = presented["clusters"][0]["calls"][0]
    assert row["last_user"] == "I want a refund."
    assert row["hangup"] == "Caller hung up"


def test_quality_review_queue_carries_last_words() -> None:
    call = _call()
    view = present_quality(
        {
            "evals": {"eligible": 1, "completed": 0, "passed": 0, "failed": 0},
            "hallucinations": {},
            "review_queue": [{"call_id": call.call_id, "kind": "price_claim", "state": "pending"}],
        },
        0.0,
        0.0,
        [call],
    )
    assert view["review_queue"][0]["last_user"] == "I want a refund."
    assert view["review_queue"][0]["hangup"] == "Caller hung up"


def test_quality_card_axes_are_detector_dims_only() -> None:
    axes = present_quality_card_axes(
        {
            "dimensions": {
                "faithfulness": {"status": "fail", "reason": "ungrounded price"},
                "abandonment": {"status": "pass", "reason": "completed"},
            }
        }
    )
    assert axes["accuracy"][0]["name"] == "faithfulness"
    assert axes["accuracy"][0]["tone"] == "loss"
    assert axes["experience"] == []
    detail = present_call_detail(_call(), timeline_view(_call()), [], [], quality_card=None)
    assert detail["quality_axes"]["accuracy"] == []


def test_call_detail_keeps_call_identity_when_turns_exist() -> None:
    detail = present_call_detail(_call(), timeline_view(_call()), [], [])
    assert detail["call_id"] == "call-refund-1"
    assert "Caller hung up" in detail["headline"]
    assert "refund" in detail["headline"]
    assert detail["hangup"] == "Caller hung up"
    assert detail["when"]
    assert detail["duration"] == "12s"
    assert len(detail["turns"]) == 2
    assert detail["turns"][0]["text"] == "I want a refund."


def test_shadow_pack_evals_are_kept_off_the_scorecard() -> None:
    detail = present_call_detail(
        _call(),
        timeline_view(_call()),
        [],
        [
            {
                "analyzer_id": "pack:conciseness",
                "passed": None,
                "verdict": "not_judged",
                "shadow": True,
                "rationale": "not_judged",
            }
        ],
    )
    assert detail["evals"] == []
    assert len(detail["evals_shadow"]) == 1
    assert detail["evals_shadow"][0]["label"] == "conciseness"


def test_not_judged_accuracy_dims_stay_on_historical_cards() -> None:
    card = {
        "dimensions": {
            "faithfulness": {"status": "not_judged", "reason": "no factual claims to check"},
            "tool_integrity": {"status": "not_judged", "reason": "no tool claims to check"},
        }
    }
    detail = present_call_detail(_call(), timeline_view(_call()), [], [], quality_card=card)
    assert detail["quality_historical"] is True
    axes = detail["quality_axes"]["accuracy"]
    assert [row["name"] for row in axes] == ["faithfulness", "tool_integrity"]
    assert all(row["status"] == "not_judged" for row in axes)
    assert all(row["tone"] != "ok" for row in axes)
    assert axes[0]["status_label"] == "not judged"


def test_groundedness_parts_skip_out_of_range() -> None:
    text = "The total is $48.50."
    parts = groundedness_parts(
        text,
        [{"start": 13, "end": 19}, {"start": 0, "end": 99}],
        title="lettucedect",
    )
    marked = [part for part in parts if part["mark"]]
    assert marked == [{"text": "$48.50", "mark": True, "title": "lettucedect"}]
    detail = present_call_detail(
        _call(),
        timeline_view(_call()),
        [],
        [],
        groundedness={
            "model": "KRLabsOrg/lettucedect-v2-mmbert-base",
            "spans": [{"turn_index": 1, "start": 0, "end": 4, "text": "I can"}],
        },
    )
    agent = next(turn for turn in detail["turns"] if turn["speaker"] == "agent")
    assert any(part["mark"] for part in agent["html_parts"])
    assert detail["groundedness_span_count"] == 1


def test_v3_card_shows_quoted_evidence_not_faithfulness() -> None:
    flags = [
        {
            "kind": "price_claim",
            "verdict": "contradicted",
            "model": "detector/2",
            "agent_span": "$48.50",
            "span_text": "I've processed a refund totaling $48.50.",
            "evidence_spans": [
                {"source": "tool", "tool_name": "lookup_order", "text": '{"error": "not_found"}'}
            ],
        }
    ]
    detail = present_call_detail(
        _call(),
        timeline_view(_call()),
        flags,
        [],
        quality_card={"schema": "obsalt.quality_card/3", "flag_kinds": ["price_claim"]},
    )
    assert detail["quality_historical"] is False
    assert detail["quality_axes"]["accuracy"] == []
    assert detail["evidence_confirmed"]
    assert detail["evidence_confirmed"][0]["agent_span"] == "$48.50"
    assert "lookup_order" in detail["evidence_confirmed"][0]["evidence"]
    assert "not_found" in detail["evidence_confirmed"][0]["evidence"]


def test_vacuous_historical_pass_is_not_painted_green() -> None:
    rows = present_quality_card(
        {
            "dimensions": {
                "faithfulness": {"status": "pass", "reason": "no factual claims to check"},
                "tool_integrity": {"status": "pass", "reason": "no tools on this call"},
            }
        }
    )
    by_name = {row["name"]: row for row in rows}
    assert by_name["faithfulness"]["status"] == "not_judged"
    assert by_name["faithfulness"]["tone"] == "warn"
    assert by_name["tool_integrity"]["status"] == "not_judged"
    assert by_name["tool_integrity"]["tone"] == "warn"
    fail = present_quality_card(
        {"dimensions": {"faithfulness": {"status": "fail", "reason": "ungrounded price"}}}
    )
    assert fail[0]["tone"] == "loss"


def test_latency_marks_the_slower_agent() -> None:
    view = present_latency(
        {
            "sample_percentiles": {},
            "by_agent": {
                "support": {
                    "e2e": {"p50": 200, "p95": 400, "n": 3, "metric": "duration"},
                    "transport": {"p50": 10, "p95": 18, "n": 3, "metric": "duration"},
                },
                "sales": {"e2e": {"p50": 500, "p95": 900, "n": 2, "metric": "duration"}},
            },
        },
        [],
    )
    slower = {(row["agent_id"], row["stage"]): row["slower"] for row in view["by_agent"]}
    assert slower[("sales", "e2e")] is True
    assert slower[("support", "e2e")] is False
    assert slower[("support", "transport")] is False


def test_join_view_surfaces_source_facts() -> None:
    call = _call(
        cost=0.18,
        evidence=[
            EvidenceRef(
                kind=EvidenceKind.RECORDING,
                uri="https://example.com/vapi/refund.wav",
                provenance=Provenance.PROVIDER_REPORTED,
                source_path="message.artifact.recordingUrl",
            )
        ],
        grounding=[
            GroundingRef(
                kind=GroundingKind.SYSTEM_PROMPT,
                content="Never invent order numbers.",
                content_ref="p1",
                provenance=Provenance.PROVIDER_REPORTED,
            )
        ],
        tools=[
            ToolInvocation(
                id="t1",
                name="lookup_order",
                status=ToolStatus.SUCCESS,
                result='{"error": "not_found"}',
            )
        ],
        turns=[
            Turn(index=0, speaker=Speaker.USER, text="I want a refund.", confidence=0.92),
            Turn(
                index=1,
                speaker=Speaker.AGENT,
                text="I've processed a refund for order ORD-99999 totaling $48.50.",
            ),
        ],
    )
    flags = [
        {
            "kind": "fabricated_id",
            "verdict": "contradicted",
            "model": "detector/1",
            "span_text": "I've processed a refund for order ORD-99999 totaling $48.50.",
            "turn_index": 1,
        }
    ]
    detail = present_call_detail(call, timeline_view(call), flags, [])
    assert detail["cost"] == "$0.18"
    assert detail["recording"] is not None
    assert detail["recording"]["uri"].startswith("https://")
    assert present_grounding(call)[0]["label"] == "Prompt"
    tool = present_tool(call.tools[0])
    assert tool["status"] == "failed"
    assert "not_found" in tool["result"]
    assert detail["turns"][0]["confidence_text"] == "92%"
    assert detail["turns"][1]["flagged"]
    assert present_call_row(call)["cost"] == "$0.18"
    assert format_cost(None) == ""
    assert (
        present_recording(
            _call(
                evidence=[
                    EvidenceRef(
                        kind=EvidenceKind.RECORDING,
                        uri="javascript:alert(1)",
                        provenance=Provenance.PROVIDER_REPORTED,
                    )
                ]
            )
        )
        is None
    )
