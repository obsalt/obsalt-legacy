"""Large tests: the six product capabilities over HTTP. No internals."""

from __future__ import annotations

from tests.helpers import (
    EXAMPLE_AGENT_ID,
    EXAMPLE_SOURCE_CALL_ID,
    EXAMPLE_STT_MS,
    EXAMPLE_USER_TEXT,
    RANGE_END,
    RANGE_QS,
    RANGE_START,
    VAPI_FIXTURES,
    api_client,
    assert_generation_labelled,
    assert_no_stage_waterfall,
    auth,
    example_state,
    first_call_id,
    ingest_example,
    vapi_headers,
    vapi_state,
)


def test_latency_uses_sample_chips_not_a_waterfall() -> None:
    client = api_client(example_state())
    ingest_example(client)
    latency = client.get(f"/v1/latency?{RANGE_QS}", headers=auth())
    assert latency.status_code == 200
    body = latency.json()
    assert_generation_labelled(body)
    stages = {item["stage"]: item for item in body["items"]}
    assert "stt" in stages
    assert stages["stt"]["p50"] == EXAMPLE_STT_MS
    assert stages["stt"]["p95"] == EXAMPLE_STT_MS
    assert body.get("aggregates_excluded") == 0
    timeline = client.get(f"/v1/calls/{first_call_id(client)}/timeline", headers=auth())
    assert timeline.status_code == 200
    view = timeline.json()
    assert_no_stage_waterfall(view)
    assert view["unplaced_stage_chips"]
    assert view["unplaced_stage_chips"][0]["value_ms"] == EXAMPLE_STT_MS
    assert view["timeline_fidelity"] == "turn_level"


def test_vapi_timeline_never_invents_stage_intervals() -> None:
    client = api_client(vapi_state())
    raw = (VAPI_FIXTURES / "raw" / "end_of_call.json").read_bytes()
    posted = client.post("/v1/ingest/vapi/ik", content=raw, headers=vapi_headers())
    assert posted.status_code == 200
    listed = client.get(f"/v1/calls?{RANGE_QS}", headers=auth())
    call_id = listed.json()["items"][0]["id"]
    view = client.get(f"/v1/calls/{call_id}/timeline", headers=auth()).json()
    assert_no_stage_waterfall(view)
    assert view["unplaced_stage_chips"]
    assert view["timeline_fidelity"] == "turn_level"
    paths = {chip.get("source_path") or "" for chip in view["unplaced_stage_chips"]}
    assert any("transcriberLatency" in path for path in paths)
    assert any("modelLatency" in path for path in paths)
    assert any("voiceLatency" in path for path in paths)


def test_hangup_cluster_uses_the_provider_agnostic_reason() -> None:
    client = api_client(example_state())
    ingest_example(client)
    hangups = client.get(f"/v1/hangups?{RANGE_QS}", headers=auth())
    assert hangups.status_code == 200
    body = hangups.json()
    assert_generation_labelled(body)
    reasons = {item["reason"]: item for item in body["items"]}
    assert "user_hangup" in reasons
    assert reasons["user_hangup"]["count"] == 1
    detail = client.get(f"/v1/calls/{first_call_id(client)}", headers=auth()).json()
    assert detail["hangup"]["reason"] == "user_hangup"
    assert detail["source_call_id"] == EXAMPLE_SOURCE_CALL_ID
    assert detail["agent_id"] == EXAMPLE_AGENT_ID


def test_tools_rollup_is_empty_when_the_source_reported_none() -> None:
    client = api_client(example_state())
    ingest_example(client)
    tools = client.get(f"/v1/tools?{RANGE_QS}", headers=auth())
    assert tools.status_code == 200
    body = tools.json()
    assert_generation_labelled(body)
    assert body["items"] == []
    assert body["invocation_count"] == 0


def test_search_finds_refunds_only_inside_the_requested_range() -> None:
    client = api_client(example_state())
    ingest_example(client)
    call_id = first_call_id(client)
    in_range = client.post(
        "/v1/search",
        headers=auth(),
        json={"q": "refund", "start": RANGE_START, "end": RANGE_END},
    )
    assert in_range.status_code == 200
    hits = in_range.json()["items"]
    assert any(item.get("call_id") == call_id for item in hits)
    too_early = client.post(
        "/v1/search",
        headers=auth(),
        json={"q": "refund", "start": "2019-01-01T00:00:00Z", "end": "2019-12-31T00:00:00Z"},
    )
    assert too_early.status_code == 200
    assert too_early.json()["items"] == []


def test_quality_does_not_count_calls_without_confirmed_hallucinations() -> None:
    client = api_client(example_state())
    ingest_example(client)
    quality = client.get(f"/v1/quality?{RANGE_QS}", headers=auth())
    assert quality.status_code == 200
    body = quality.json()
    assert_generation_labelled(body)
    assert body["hallucinations"]["count"] == 0
    assert body["flag_count"] == 0
    detail = client.get(f"/v1/calls/{first_call_id(client)}", headers=auth()).json()
    texts = [turn["text"] for turn in detail["turns"]]
    assert EXAMPLE_USER_TEXT in texts
    assert detail["decoder_version"]
    assert detail["coverage"]


def test_every_org_rubric_is_evaluated_on_analyze() -> None:
    client = api_client(example_state())
    ingest_example(client)
    call_id = first_call_id(client)
    first = client.post(
        "/v1/rubrics",
        headers=auth(),
        json={"name": "grounded", "description": "no invented facts"},
    )
    second = client.post(
        "/v1/rubrics",
        headers=auth(),
        json={"name": "polite", "description": "Was the agent polite?"},
    )
    assert first.status_code == 200
    assert second.status_code == 200
    analyzed = client.post(f"/v1/calls/{call_id}/analyze", headers=auth())
    assert analyzed.status_code == 200
    items = analyzed.json()["items"]
    assert len(items) == 2
    detail = client.get(f"/v1/calls/{call_id}", headers=auth()).json()
    evals = [
        row
        for row in detail["analysis"]
        if row.get("execution", {}).get("analyzer_id") in {"tier2", "eval", "rubric"}
        or "passed" in (row.get("payload") or {})
        and row.get("execution", {}).get("analyzer_id") not in {"hallucination", "flags"}
    ]
    assert len(evals) >= 2
