"""Large tests: public HTTP contract. Status codes and JSON only."""

from __future__ import annotations

from fastapi.testclient import TestClient

from obsalt.api import create_app, create_test_app
from obsalt.config import Settings
from tests.helpers import (
    RANGE_QS,
    api_client,
    auth,
    example_headers,
    example_raw,
    example_state,
    first_call_id,
    ingest_example,
    list_calls,
)


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


def test_health_is_public() -> None:
    client = api_client(example_state())
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert "version" in body


def test_reads_require_an_api_key() -> None:
    client = api_client(example_state())
    listed = client.get(f"/v1/calls?{RANGE_QS}")
    assert listed.status_code == 401


def test_unknown_api_key_is_401_not_first_org() -> None:
    client = api_client(example_state())
    listed = client.get(f"/v1/calls?{RANGE_QS}", headers=auth("nope"))
    assert listed.status_code == 401


def test_collection_lists_require_a_time_range() -> None:
    client = api_client(example_state())
    headers = auth()
    assert client.get("/v1/calls", headers=headers).status_code == 400
    assert client.get("/v1/latency", headers=headers).status_code == 400
    assert client.get("/v1/hangups", headers=headers).status_code == 400
    assert client.get("/v1/tools", headers=headers).status_code == 400
    assert client.get("/v1/quality", headers=headers).status_code == 400
    missing = client.post("/v1/search", headers=headers, json={"q": "refund"})
    assert missing.status_code == 400


def test_fleet_responses_carry_one_generation() -> None:
    client = api_client(example_state())
    latency = client.get(f"/v1/latency?{RANGE_QS}", headers=auth())
    assert latency.status_code == 200
    assert latency.json()["as_of_generation"] == "g1"


def test_cross_tenant_call_is_404_not_403() -> None:
    client = api_client(example_state())
    ingest_example(client)
    call_id = first_call_id(client)
    denied = client.get(f"/v1/calls/{call_id}", headers=auth("other"))
    assert denied.status_code == 404
    foreign = list_calls(client, key="other")
    assert foreign["items"] == []


def test_ingest_unknown_plugin_is_404() -> None:
    client = api_client(example_state())
    raw = example_raw()
    res = client.post("/v1/ingest/not-a-plugin/ik", content=raw, headers=example_headers(raw))
    assert res.status_code == 404


def test_csrf_required_for_ui_mutation() -> None:
    client = api_client(example_state())
    ingest_example(client)
    call_id = first_call_id(client)
    login = client.post("/v1/ui/login", data={"api_key": "k"}, follow_redirects=False)
    assert login.status_code == 303
    denied = client.post(
        f"/v1/ui/calls/{call_id}/analyze", data={"csrf": "nope"}, follow_redirects=False
    )
    assert denied.status_code == 403


def test_plugins_endpoint_requires_an_api_key() -> None:
    client = api_client(example_state())
    assert client.get("/v1/plugins").status_code == 401
    listed = client.get("/v1/plugins", headers=auth())
    assert listed.status_code == 200
    assert "example" in {item["name"] for item in listed.json()["items"]}
