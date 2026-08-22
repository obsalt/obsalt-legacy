"""Black-box: the six product capabilities over HTTP. No internals."""

from __future__ import annotations

from obsalt.domain.enums import AnalysisState
from obsalt.domain.models import AnalysisExecution, AnalysisResult

from tests.helpers import (
    EXAMPLE_FIXTURES,
    VAPI_FIXTURES,
    api_client,
    example_headers,
    example_state,
    vapi_headers,
    vapi_state,
)

RANGE = "start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z"


def _ingest_example():
    state = example_state()
    client = api_client(state)
    raw = (EXAMPLE_FIXTURES / "raw" / "call_ended.json").read_bytes()
    posted = client.post("/v1/ingest/example/ik", content=raw, headers=example_headers(raw))
    assert posted.status_code == 200
    listed = client.get(f"/v1/calls?{RANGE}", headers={"X-API-Key": "k"})
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert items
    return client, items[0]["id"]


def test_six_capability_endpoints_respond_for_an_ingested_call() -> None:
    client, call_id = _ingest_example()
    auth = {"X-API-Key": "k"}
    latency = client.get(f"/v1/latency?{RANGE}", headers=auth)
    hangups = client.get(f"/v1/hangups?{RANGE}", headers=auth)
    tools = client.get(f"/v1/tools?{RANGE}", headers=auth)
    quality = client.get(f"/v1/quality?{RANGE}", headers=auth)
    search = client.post(
        "/v1/search",
        headers=auth,
        json={"q": "refund", "start": "2020-01-01T00:00:00Z", "end": "2030-01-01T00:00:00Z"},
    )
    timeline = client.get(f"/v1/calls/{call_id}/timeline", headers=auth)
    detail = client.get(f"/v1/calls/{call_id}", headers=auth)
    assert latency.status_code == 200
    assert hangups.status_code == 200
    assert tools.status_code == 200
    assert quality.status_code == 200
    assert search.status_code == 200
    assert timeline.status_code == 200
    assert detail.status_code == 200
    body = timeline.json()
    assert body["draw_stage_waterfall"] is False
    assert body["stage_intervals"] == []
    assert body["unplaced_stage_chips"]
    assert "hallucinations" in quality.json()
    hits = search.json()["items"]
    assert any(item.get("call_id") == call_id for item in hits)


def test_vapi_timeline_http_never_invents_a_stage_waterfall() -> None:
    state = vapi_state()
    client = api_client(state)
    raw = (VAPI_FIXTURES / "raw" / "end_of_call.json").read_bytes()
    posted = client.post("/v1/ingest/vapi/ik", content=raw, headers=vapi_headers())
    assert posted.status_code == 200
    listed = client.get(f"/v1/calls?{RANGE}", headers={"X-API-Key": "k"})
    call_id = listed.json()["items"][0]["id"]
    timeline = client.get(f"/v1/calls/{call_id}/timeline", headers={"X-API-Key": "k"})
    body = timeline.json()
    assert body["draw_stage_waterfall"] is False
    assert body["stage_intervals"] == []
    assert body["unplaced_stage_chips"]
    assert body["timeline_fidelity"] == "turn_level"


def test_call_detail_ui_uses_fidelity_not_hardcoded_vapi_copy() -> None:
    state = vapi_state()
    client = api_client(state)
    raw = (VAPI_FIXTURES / "raw" / "end_of_call.json").read_bytes()
    client.post("/v1/ingest/vapi/ik", content=raw, headers=vapi_headers())
    listed = client.get(f"/v1/calls?{RANGE}", headers={"X-API-Key": "k"})
    call_id = listed.json()["items"][0]["id"]
    login = client.post("/v1/ui/login", data={"api_key": "k"}, follow_redirects=False)
    assert login.status_code == 303
    page = client.get(f"/v1/ui/calls/{call_id}")
    assert page.status_code == 200
    html = page.text
    assert "Vapi reports stage durations" not in html
    assert "No invented stage intervals" in html
    assert "turn bars with unplaced stage chips" in html or "Unplaced stage chips" in html


def test_quality_http_does_not_count_pending_hallucinations() -> None:
    state = example_state()
    client = api_client(state)
    raw = (EXAMPLE_FIXTURES / "raw" / "call_ended.json").read_bytes()
    client.post("/v1/ingest/example/ik", content=raw, headers=example_headers(raw))
    listed = client.get(f"/v1/calls?{RANGE}", headers={"X-API-Key": "k"})
    call_id = listed.json()["items"][0]["id"]
    detail = client.get(f"/v1/calls/{call_id}", headers={"X-API-Key": "k"})
    revision = detail.json()["revision"]
    state.sink.write_analysis(
        "acme",
        call_id,
        revision,
        [
            AnalysisResult(
                execution=AnalysisExecution(
                    call_id=call_id,
                    revision=revision,
                    analyzer_id="hallucination",
                    analyzer_version="1",
                    state=AnalysisState.PENDING,
                ),
                payload={"candidates": [{"kind": "price_claim", "needs_llm": True}], "selection": "pending"},
            )
        ],
    )
    quality = client.get(f"/v1/quality?{RANGE}", headers={"X-API-Key": "k"})
    assert quality.status_code == 200
    assert quality.json()["hallucinations"]["count"] == 0
