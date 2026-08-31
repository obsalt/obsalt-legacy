"""Large tests: console, sessions, keys, and rubrics. HTTP only."""

from __future__ import annotations

from fastapi.testclient import TestClient

from obsalt.api import create_test_app
from obsalt.config import Settings
from obsalt.domain.enums import EnvelopeState
from obsalt.plugin.types import RawEnvelope
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
    assert 'class="waterfall"' not in html
    assert "not placed on the call" in html
    assert "Evaluate this call" in html
    assert f"/v1/ui/calls/{call_id}/analyze" in html
    headline = html.split("<h1>", 1)[1].split("</h1>", 1)[0].strip()
    assert headline
    assert "Caller hung up" in html
    assert "ORD-99999" in html
    assert "$48.50" in html
    assert "not_found" in html
    assert "Faithfulness" not in html
    assert "Said vs done" not in html
    assert "Missing judge output is never a pass" in html
    assert "$0.18" in html
    assert "not_found" in html
    assert "Open source recording" in html
    assert "Grounding" in html
    assert "Caller" in html
    assert "0 user" not in html
    assert "decoder_version" not in html.split("<h1>", 1)[0]


def test_evaluate_does_not_invent_accuracy_dims() -> None:
    client = api_client(example_state())
    ingest_example(client)
    call_id = first_call_id(client)
    login = client.post("/v1/ui/login", data={"api_key": "k"}, follow_redirects=False)
    assert login.status_code == 303
    csrf = client.cookies.get("obsalt_csrf")
    assert csrf
    before = client.get(f"/v1/ui/calls/{call_id}")
    assert before.status_code == 200
    assert "Faithfulness" not in before.text
    assert "Said vs done" not in before.text
    assert "pill ok" not in before.text
    assert "No checkable numeric, id, or bound tool-success claim" in before.text
    analyzed = client.post(
        f"/v1/ui/calls/{call_id}/analyze",
        data={"csrf": csrf},
        follow_redirects=False,
    )
    assert analyzed.status_code == 303
    after = client.get(f"/v1/ui/calls/{call_id}")
    assert after.status_code == 200
    assert "Faithfulness" not in after.text
    assert "Said vs done" not in after.text
    assert "pill ok" not in after.text
    assert "No checkable numeric, id, or bound tool-success claim" in after.text


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
    assert res.json()["item"]["call_id"] == "missing-is-ok"
    assert "revision" in res.json()["item"]


def test_key_rotation_keeps_overlap() -> None:
    settings = Settings(environment="test")
    state = in_memory_state(settings)
    client = TestClient(create_test_app(settings, state))
    rotated = client.post("/v1/keys/rotate", headers=auth("dev-key"), json={"overlap_seconds": 60})
    assert rotated.status_code == 200
    new_key = rotated.json()["key"]
    assert client.get("/v1/plugins", headers=auth("dev-key")).status_code == 200
    assert client.get("/v1/plugins", headers=auth(new_key)).status_code == 200


def test_quality_page_leads_with_coverage_not_a_fake_pass_rate() -> None:
    client = api_client(vapi_state())
    raw = (VAPI_FIXTURES / "raw" / "end_of_call.json").read_bytes()
    client.post("/v1/ingest/vapi/ik", content=raw, headers=vapi_headers())
    login = client.post("/v1/ui/login", data={"api_key": "k"}, follow_redirects=False)
    assert login.status_code == 303
    page = client.get(f"/v1/ui/quality?{RANGE_QS}")
    assert page.status_code == 200
    html = page.text
    assert "Calls in range" in html
    assert "Evidence missing" in html
    assert "Confirmed flag" in html
    assert "Accuracy" not in html
    assert "Experience" not in html
    assert "Faithfulness" not in html
    assert "last words were negative" not in html


def test_console_collection_pages_default_last_seven_days() -> None:
    client = api_client(example_state())
    ingest_example(client)
    call_id = first_call_id(client)
    login = client.post("/v1/ui/login", data={"api_key": "k"}, follow_redirects=False)
    assert login.status_code == 303
    for path in ("/v1/ui", "/v1/ui/latency", "/v1/ui/hangups", "/v1/ui/quality"):
        bare = client.get(path)
        assert bare.status_code == 200
        assert b"start and end are required" not in bare.content
        found = client.get(f"{path}?{RANGE_QS}")
        assert found.status_code == 200
        if path == "/v1/ui":
            assert call_id.encode() in found.content


def test_console_review_records_disagreement() -> None:
    client = api_client(example_state())
    ingest_example(client)
    login = client.post("/v1/ui/login", data={"api_key": "k"}, follow_redirects=False)
    assert login.status_code == 303
    csrf = client.cookies.get("obsalt_csrf")
    assert csrf
    posted = client.post(
        "/v1/ui/quality/review",
        data={"csrf": csrf, "call_id": first_call_id(client), "agree": "0"},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    assert "/v1/ui/quality" in posted.headers.get("location", "")


def test_console_search_requires_a_range_and_finds_refunds() -> None:
    client = api_client(example_state())
    ingest_example(client)
    call_id = first_call_id(client)
    login = client.post("/v1/ui/login", data={"api_key": "k"}, follow_redirects=False)
    assert login.status_code == 303
    missing = client.get("/v1/ui/search?q=refund")
    assert missing.status_code == 200
    assert b"start and end are required" not in missing.content
    found = client.get(f"/v1/ui/search?q=refund&{RANGE_QS}")
    assert found.status_code == 200
    assert call_id.encode() in found.content
    too_early = client.get(
        "/v1/ui/search?q=refund&start=2019-01-01T00:00:00Z&end=2019-12-31T00:00:00Z"
    )
    assert too_early.status_code == 200
    assert call_id.encode() not in too_early.content


def test_console_call_list_shows_why_the_caller_left() -> None:
    client = api_client(example_state())
    ingest_example(client)
    login = client.post("/v1/ui/login", data={"api_key": "k"}, follow_redirects=False)
    assert login.status_code == 303
    page = client.get(f"/v1/ui?{RANGE_QS}")
    assert page.status_code == 200
    html = page.text
    assert "I want a refund." in html
    assert "Caller hung up" in html
    assert "Why did we lose callers?" in html
    assert "Where did time go?" in html
    assert " of 1 lost" not in html
    assert "last words were negative" not in html


def test_console_unsigned_home_explains_the_product() -> None:
    client = api_client(example_state())
    ingest_example(client)
    page = client.get("/v1/ui")
    assert page.status_code == 200
    assert b"Sign in to see live calls" in page.content
    assert b"ex-1" not in page.content


def test_console_search_shows_matching_utterance() -> None:
    client = api_client(example_state())
    ingest_example(client)
    login = client.post("/v1/ui/login", data={"api_key": "k"}, follow_redirects=False)
    assert login.status_code == 303
    page = client.get(f"/v1/ui/search?q=refund&{RANGE_QS}")
    assert page.status_code == 200
    assert "I want a refund." in page.text
    assert first_call_id(client) in page.text


def test_settings_keeps_range_query_on_nav() -> None:
    client = api_client(example_state())
    login = client.post("/v1/ui/login", data={"api_key": "k"}, follow_redirects=False)
    assert login.status_code == 303
    page = client.get("/v1/ui/settings?preset=30d")
    assert page.status_code == 200
    assert 'href="/v1/ui?preset=30d"' in page.text


def test_console_filter_bar_uses_date_pickers_and_source_select() -> None:
    client = api_client(example_state())
    ingest_example(client)
    login = client.post("/v1/ui/login", data={"api_key": "k"}, follow_redirects=False)
    assert login.status_code == 303
    for path in ("/v1/ui", "/v1/ui/latency", "/v1/ui/hangups", "/v1/ui/quality", "/v1/ui/search"):
        page = client.get(f"{path}?{RANGE_QS}")
        assert page.status_code == 200
        html = page.text
        assert 'type="date"' in html
        assert 'name="source"' in html
        assert "<select" in html
        assert 'name="outcome"' not in html
        assert "Last 7 days" in html
        assert "Start (UTC)" in html
    calls = client.get(f"/v1/ui?{RANGE_QS}")
    assert "Caller hung up" in calls.text
    assert "Not classified" not in calls.text
    assert 'name="hangup_reason"' in calls.text
    quality = client.get(f"/v1/ui/quality?{RANGE_QS}")
    assert "Grounding candidates" in quality.text
    assert "Not scored" not in quality.text
    preset = client.get("/v1/ui?preset=7d")
    assert preset.status_code == 200
    assert b"start and end are required" not in preset.content


def test_console_search_accepts_a_date_only_range() -> None:
    client = api_client(example_state())
    ingest_example(client)
    call_id = first_call_id(client)
    login = client.post("/v1/ui/login", data={"api_key": "k"}, follow_redirects=False)
    assert login.status_code == 303
    found = client.get("/v1/ui/search?q=refund&start=2020-01-01&end=2030-01-01")
    assert found.status_code == 200
    assert call_id.encode() in found.content
    assert 'type="search"' in found.text


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


def _login(client, key: str = "k") -> str:
    login = client.post("/v1/ui/login", data={"api_key": key}, follow_redirects=False)
    assert login.status_code == 303
    csrf = client.cookies.get("obsalt_csrf")
    assert csrf
    return csrf


def test_console_shows_role_and_signs_out() -> None:
    client = api_client(example_state())
    csrf = _login(client)
    home = client.get("/v1/ui")
    assert home.status_code == 200
    assert "Owner" in home.text
    assert "Sign out" in home.text
    logged_out = client.post("/v1/ui/logout", data={"csrf": csrf}, follow_redirects=False)
    assert logged_out.status_code == 303
    assert "obsalt_session=" in (logged_out.headers.get("set-cookie") or "")


def test_connection_create_shows_ingest_url_once() -> None:
    client = api_client(vapi_state())
    csrf = _login(client)
    created = client.post(
        "/v1/ui/connections",
        data={
            "csrf": csrf,
            "provider": "vapi",
            "secret_legacy_secret": "ui-secret",
            "setting_auth_mode": "legacy_secret",
        },
    )
    assert created.status_code == 200
    assert "/v1/ingest/vapi/" in created.text
    assert "Copy the ingest URL now" in created.text
    settings = client.get("/v1/ui/settings")
    assert settings.status_code == 200
    assert "ui-secret" not in settings.text
    assert "/v1/ingest/vapi/" not in settings.text or "Copy the ingest URL now" not in settings.text
    assert "Create a hosted connection" in settings.text
    assert "vapi" in settings.text


def test_console_replay_this_call_promotes_a_new_revision() -> None:
    client = api_client(example_state())
    ingest_example(client)
    call_id = first_call_id(client)
    first = client.get(f"/v1/calls/{call_id}", headers=auth()).json()
    csrf = _login(client)
    missing = client.post(
        f"/v1/ui/calls/{call_id}/delete",
        data={"csrf": csrf, "confirm": "nope"},
        follow_redirects=False,
    )
    assert missing.status_code == 400
    replayed = client.post(
        f"/v1/ui/calls/{call_id}/replay",
        data={"csrf": csrf},
        follow_redirects=False,
    )
    assert replayed.status_code == 303
    second = client.get(f"/v1/calls/{call_id}", headers=auth()).json()
    assert second["revision"] != first["revision"]


def test_console_export_is_an_attachment() -> None:
    client = api_client(example_state())
    ingest_example(client)
    csrf = _login(client)
    exported = client.post("/v1/ui/export", data={"csrf": csrf})
    assert exported.status_code == 200
    assert "attachment" in exported.headers.get("content-disposition", "")
    assert "obsalt-calls.jsonl" in exported.headers.get("content-disposition", "")
    assert first_call_id(client) in exported.text


def test_console_seed_refuses_production() -> None:
    settings = Settings(environment="production", session_secret="change-me-session")
    state = in_memory_state(settings)
    client = TestClient(create_test_app(settings, state))
    login = client.post("/v1/ui/login", data={"api_key": "dev-key"}, follow_redirects=False)
    assert login.status_code == 303
    csrf = client.cookies.get("obsalt_csrf")
    denied = client.post("/v1/ui/seed", data={"csrf": csrf})
    assert denied.status_code == 403


def test_console_seed_queues_sample_calls() -> None:
    settings = Settings(environment="test", trace_grace_seconds=0)
    state = in_memory_state(settings)
    client = TestClient(create_test_app(settings, state))
    login = client.post("/v1/ui/login", data={"api_key": "dev-key"}, follow_redirects=False)
    assert login.status_code == 303
    csrf = client.cookies.get("obsalt_csrf")
    seeded = client.post("/v1/ui/seed", data={"csrf": csrf}, follow_redirects=False)
    assert seeded.status_code == 303
    listed = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z",
        headers=auth("dev-key"),
    )
    assert listed.status_code == 200
    assert listed.json()["items"]


def test_console_settings_create_rubric() -> None:
    client = api_client(example_state())
    csrf = _login(client)
    created = client.post(
        "/v1/ui/rubrics",
        data={
            "csrf": csrf,
            "name": "empathy",
            "description": "acknowledge first",
            "threshold": "0.7",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    page = client.get("/v1/ui/settings")
    assert "empathy" in page.text
    assert "acknowledge first" in page.text


def test_console_settings_create_opening_disclosure_predicate() -> None:
    client = api_client(example_state())
    csrf = _login(client)
    created = client.post(
        "/v1/ui/rubrics",
        data={
            "csrf": csrf,
            "kind": "predicate",
            "name": "recording-disclosure",
            "description": "opening disclosure",
            "combinator": "all",
            "phrase_in_opening": "call may be recorded",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    state = client.app.state.obsalt
    rubric = next(r for r in state.rubrics.values() if r.name == "recording-disclosure")
    assert rubric.is_predicate()
    assert rubric.spec == {"all": [{"phrase_in_opening": "call may be recorded"}]}


def test_console_clears_org_dlq() -> None:
    client = api_client(example_state())
    csrf = _login(client)
    state = client.app.state.obsalt
    state.inbox.by_id["e-dlq"] = RawEnvelope(
        envelope_id="e-dlq",
        org_id="acme",
        provider="example",
        connection_id="c1",
        object_key="org/acme/raw/example/d/ab",
        delivery_key="d",
        content_sha256="ab",
        state=EnvelopeState.FAILED,
    )
    state.inbox.dlq.append(
        {"envelope_id": "e-dlq", "org_id": "acme", "error": "fact conflicts block promotion"}
    )
    missing = client.post("/v1/ui/dlq/purge", data={"csrf": csrf}, follow_redirects=False)
    assert missing.status_code == 400
    cleared = client.post(
        "/v1/ui/dlq/purge",
        data={"csrf": csrf, "confirm": "DELETE"},
        follow_redirects=False,
    )
    assert cleared.status_code == 303
    assert "dlq_cleared" in cleared.headers.get("location", "")
    assert state.inbox.dlq == []


def test_console_filter_bar_includes_eval_and_search_hangup() -> None:
    client = api_client(example_state())
    ingest_example(client)
    _login(client)
    calls = client.get(f"/v1/ui?{RANGE_QS}")
    assert 'name="eval_result"' in calls.text
    assert 'name="latency_ms"' in calls.text
    search = client.get(f"/v1/ui/search?q=refund&{RANGE_QS}")
    assert 'name="hangup_reason"' in search.text
