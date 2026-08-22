"""Large tests: console, sessions, keys, and rubrics. HTTP only."""

from __future__ import annotations

from fastapi.testclient import TestClient

from obsalt.api import create_test_app
from obsalt.config import Settings
from obsalt.runtime import in_memory_state
from tests.helpers import (
    RANGE_QS,
    VAPI_FIXTURES,
    api_client,
    auth,
    example_state,
    first_call_id,
    ingest_example,
    vapi_headers,
    vapi_state,
)


def test_login_sets_httponly_session_cookie() -> None:
    client = api_client(example_state())
    res = client.post("/v1/ui/login", data={"api_key": "k"}, follow_redirects=False)
    assert res.status_code == 303
    cookie = res.headers.get("set-cookie", "")
    assert "obsalt_session=" in cookie
    assert "HttpOnly" in cookie


def test_ui_does_not_collapse_to_first_org_without_session() -> None:
    client = api_client(example_state())
    ingest_example(client)
    page = client.get("/v1/ui")
    assert page.status_code == 200
    assert b"ex-1" not in page.content
    assert b"obsalt" in page.content


def test_call_detail_ui_uses_fidelity_not_hardcoded_vapi_copy() -> None:
    client = api_client(vapi_state())
    raw = (VAPI_FIXTURES / "raw" / "end_of_call.json").read_bytes()
    client.post("/v1/ingest/vapi/ik", content=raw, headers=vapi_headers())
    listed = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z",
        headers=auth(),
    )
    call_id = listed.json()["items"][0]["id"]
    login = client.post("/v1/ui/login", data={"api_key": "k"}, follow_redirects=False)
    assert login.status_code == 303
    page = client.get(f"/v1/ui/calls/{call_id}")
    assert page.status_code == 200
    html = page.text
    assert "Vapi reports stage durations" not in html
    assert "No invented stage intervals" in html
    assert "turn bars with unplaced stage chips" in html or "Unplaced stage chips" in html


def test_evidence_is_fetchable_by_ref() -> None:
    client = api_client(example_state())
    ingest_example(client)
    call_id = first_call_id(client)
    detail = client.get(f"/v1/calls/{call_id}", headers=auth()).json()
    text_ref = detail["turns"][0]["text_ref"]
    assert text_ref
    ev = client.get(f"/v1/calls/{call_id}/evidence/{text_ref}", headers=auth())
    assert ev.status_code == 200
    assert b"refund" in ev.content


def test_replay_promotes_a_new_revision() -> None:
    client = api_client(example_state())
    ingest_example(client)
    first = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z",
        headers=auth(),
    ).json()["items"][0]
    replayed = client.post("/v1/replay", json={}, headers=auth())
    assert replayed.status_code == 200
    assert replayed.json()["replayed"] >= 1
    second = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z",
        headers=auth(),
    ).json()["items"][0]
    assert second["id"] == first["id"]
    assert second["revision"] != first["revision"]


def test_rubric_put_increments_version_on_same_id() -> None:
    client = api_client(example_state())
    created = client.post(
        "/v1/rubrics",
        headers=auth(),
        json={"name": "grounded", "description": "no invented facts"},
    )
    assert created.status_code == 200
    rubric_id = created.json()["id"]
    updated = client.put(
        f"/v1/rubrics/{rubric_id}",
        headers=auth(),
        json={"name": "grounded-stricter", "description": "stricter"},
    )
    assert updated.status_code == 200
    assert updated.json()["id"] == rubric_id
    assert updated.json()["version"] == 2


def test_review_records_disagreement() -> None:
    client = api_client(example_state())
    res = client.post(
        "/v1/quality/review",
        headers=auth(),
        json={"call_id": "missing-is-ok", "agree": False},
    )
    assert res.status_code == 200
    assert res.json()["status"] == "recorded"


def test_key_rotation_keeps_overlap() -> None:
    settings = Settings(environment="test")
    state = in_memory_state(settings)
    client = TestClient(create_test_app(settings, state))
    rotated = client.post("/v1/keys/rotate", headers=auth("dev-key"), json={"overlap_seconds": 60})
    assert rotated.status_code == 200
    new_key = rotated.json()["key"]
    assert client.get("/v1/plugins", headers=auth("dev-key")).status_code == 200
    assert client.get("/v1/plugins", headers=auth(new_key)).status_code == 200


def test_console_search_requires_a_range_and_finds_refunds() -> None:
    client = api_client(example_state())
    ingest_example(client)
    call_id = first_call_id(client)
    login = client.post("/v1/ui/login", data={"api_key": "k"}, follow_redirects=False)
    assert login.status_code == 303
    missing = client.get("/v1/ui/search?q=refund")
    assert missing.status_code == 200
    assert b"start and end are required" in missing.content
    found = client.get(f"/v1/ui/search?q=refund&{RANGE_QS}")
    assert found.status_code == 200
    assert call_id.encode() in found.content
    too_early = client.get(
        "/v1/ui/search?q=refund&start=2019-01-01T00:00:00Z&end=2019-12-31T00:00:00Z"
    )
    assert too_early.status_code == 200
    assert call_id.encode() not in too_early.content


def test_call_list_filters_by_agent() -> None:
    client = api_client(example_state())
    ingest_example(client)
    listed = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z&agent_id=support",
        headers=auth(),
    )
    assert listed.status_code == 200
    assert listed.json()["items"]
    empty = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z&agent_id=other",
        headers=auth(),
    )
    assert empty.json()["items"] == []
