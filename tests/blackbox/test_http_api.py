"""Black-box HTTP API tests. The client only sees status codes and JSON."""

from __future__ import annotations

from fastapi.testclient import TestClient

from obsalt.api import create_app, create_test_app
from obsalt.config import Settings
from obsalt.crypto.primitives import hmac_hex
from tests.helpers import EXAMPLE_FIXTURES, signed_example_headers


def test_create_app_without_state_is_not_a_memory_backend() -> None:
    try:
        create_app(Settings(environment="dev"))
    except RuntimeError as exc:
        assert "production_state" in str(exc) or "create_test_app" in str(exc)
    else:
        raise AssertionError("create_app() must not silently start an in-memory backend")


def test_create_test_app_is_explicit() -> None:
    client = TestClient(create_test_app())
    assert client.get("/health").json()["status"] == "ok"


def test_openapi_describes_the_product() -> None:
    spec = create_test_app().openapi()
    description = (spec["info"].get("description") or "").lower()
    assert "x-api-key" in description
    assert "/v1/ui" in description
    assert spec["info"]["title"] == "obsalt"


def test_unknown_api_key_is_401_not_first_org(client: TestClient) -> None:
    res = client.get(
        "/v1/calls?start=2026-01-01T00:00:00Z&end=2026-12-31T00:00:00Z",
        headers={"X-API-Key": "nope"},
    )
    assert res.status_code == 401


def test_list_and_fleet_require_time_range(client: TestClient) -> None:
    headers = {"X-API-Key": "k"}
    assert client.get("/v1/calls", headers=headers).status_code == 400
    assert client.get("/v1/latency", headers=headers).status_code == 400
    assert client.get("/v1/hangups", headers=headers).status_code == 400
    res = client.get(
        "/v1/latency?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z",
        headers=headers,
    )
    assert res.status_code == 200
    assert res.json()["as_of_generation"] == "g1"


def test_ingest_list_detail_timeline_and_ui(client: TestClient, example_raw: bytes) -> None:
    res = client.post(
        "/v1/ingest/example/ik", content=example_raw, headers=signed_example_headers(example_raw)
    )
    assert res.status_code == 200
    listed = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z",
        headers={"X-API-Key": "k"},
    )
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert items
    call_id = items[0]["id"]
    detail = client.get(f"/v1/calls/{call_id}", headers={"X-API-Key": "k"})
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["decoder_version"]
    assert payload["coverage"]
    timeline = client.get(f"/v1/calls/{call_id}/timeline", headers={"X-API-Key": "k"})
    assert timeline.status_code == 200
    assert "draw_stage_waterfall" in timeline.json()
    ui = client.get("/v1/ui")
    assert ui.status_code == 200
    assert b"obsalt" in ui.content


def test_cross_tenant_read_is_404(client: TestClient, example_raw: bytes) -> None:
    client.post(
        "/v1/ingest/example/ik", content=example_raw, headers=signed_example_headers(example_raw)
    )
    listed = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z",
        headers={"X-API-Key": "k"},
    )
    call_id = listed.json()["items"][0]["id"]
    denied = client.get(f"/v1/calls/{call_id}", headers={"X-API-Key": "other"})
    assert denied.status_code == 404


def test_replay_promotes_a_new_revision(client: TestClient, example_raw: bytes) -> None:
    client.post(
        "/v1/ingest/example/ik", content=example_raw, headers=signed_example_headers(example_raw)
    )
    first = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z",
        headers={"X-API-Key": "k"},
    ).json()["items"][0]
    replayed = client.post("/v1/replay", json={}, headers={"X-API-Key": "k"})
    assert replayed.status_code == 200
    assert replayed.json()["replayed"] >= 1
    second = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z",
        headers={"X-API-Key": "k"},
    ).json()["items"][0]
    assert second["id"] == first["id"]
    assert second["revision"] != first["revision"]


def test_evidence_and_session_cookie(client: TestClient, example_raw: bytes) -> None:
    client.post(
        "/v1/ingest/example/ik", content=example_raw, headers=signed_example_headers(example_raw)
    )
    listed = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z",
        headers={"X-API-Key": "k"},
    )
    call = listed.json()["items"][0]
    detail = client.get(f"/v1/calls/{call['id']}", headers={"X-API-Key": "k"}).json()
    text_ref = detail["turns"][0]["text_ref"]
    ev = client.get(f"/v1/calls/{call['id']}/evidence/{text_ref}", headers={"X-API-Key": "k"})
    assert ev.status_code == 200
    login = client.post("/v1/ui/login", data={"api_key": "k"}, follow_redirects=False)
    assert login.status_code == 303
    cookie = login.headers.get("set-cookie", "")
    assert "obsalt_session=" in cookie
    assert "HttpOnly" in cookie


def test_csrf_required_for_ui_analyze(client: TestClient, example_raw: bytes) -> None:
    client.post(
        "/v1/ingest/example/ik", content=example_raw, headers=signed_example_headers(example_raw)
    )
    listed = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z",
        headers={"X-API-Key": "k"},
    )
    call_id = listed.json()["items"][0]["id"]
    login = client.post("/v1/ui/login", data={"api_key": "k"}, follow_redirects=False)
    assert login.status_code == 303
    denied = client.post(
        f"/v1/ui/calls/{call_id}/analyze", data={"csrf": "nope"}, follow_redirects=False
    )
    assert denied.status_code == 403


def test_ingest_unknown_plugin_is_404(client: TestClient, example_raw: bytes) -> None:
    res = client.post(
        "/v1/ingest/not-a-plugin/ik",
        content=example_raw,
        headers=signed_example_headers(example_raw),
    )
    assert res.status_code == 404


def test_signed_example_fixture_still_exists() -> None:
    assert (EXAMPLE_FIXTURES / "raw" / "call_ended.json").is_file()
    raw = (EXAMPLE_FIXTURES / "raw" / "call_ended.json").read_bytes()
    digest = hmac_hex("s", raw)
    assert digest == hmac_hex("s", raw)
    assert digest != hmac_hex("wrong", raw)
    assert len(digest) == 64
