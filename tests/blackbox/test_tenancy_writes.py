"""Tenant identity comes only from authenticated credentials (T10)."""

from __future__ import annotations

from fastapi.testclient import TestClient
from obsalt.runtime import AppState
from tests.helpers import signed_example_headers


def test_ingest_writes_only_the_authenticated_org(
    client: TestClient, example_raw: bytes
) -> None:
    res = client.post(
        "/v1/ingest/example/ik",
        content=example_raw,
        headers=signed_example_headers(example_raw),
    )
    assert res.status_code == 200
    acme = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z",
        headers={"X-API-Key": "k"},
    )
    other = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z",
        headers={"X-API-Key": "other"},
    )
    assert acme.status_code == 200
    assert acme.json()["items"]
    assert other.status_code == 200
    assert other.json()["items"] == []


def test_unknown_ingest_key_does_not_persist(
    client: TestClient, memory_state: AppState, example_raw: bytes
) -> None:
    res = client.post(
        "/v1/ingest/example/nope",
        content=example_raw,
        headers=signed_example_headers(example_raw),
    )
    assert res.status_code == 404
    assert memory_state.objects.blobs == {}
    assert memory_state.inbox.by_id == {}


def test_cross_tenant_delete_cannot_remove_other_org_call(
    client: TestClient, example_raw: bytes
) -> None:
    client.post(
        "/v1/ingest/example/ik",
        content=example_raw,
        headers=signed_example_headers(example_raw),
    )
    listed = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z",
        headers={"X-API-Key": "k"},
    )
    call_id = listed.json()["items"][0]["id"]
    other = client.post(
        "/v1/privacy/deletion-requests",
        json={"call_id": call_id},
        headers={"X-API-Key": "other"},
    )
    assert other.status_code == 200
    assert call_id not in (other.json().get("deleted_calls") or [])
    still = client.get(f"/v1/calls/{call_id}", headers={"X-API-Key": "k"})
    assert still.status_code == 200
    assert still.json()["call_id"] == call_id
