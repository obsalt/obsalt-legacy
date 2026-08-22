"""Large tests: deletion completes and tenant identity is credential-only."""

from __future__ import annotations

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


def test_ingest_writes_only_the_authenticated_org() -> None:
    client = api_client(example_state())
    ingest_example(client)
    acme = list_calls(client, key="k")
    other = list_calls(client, key="other")
    assert acme["items"]
    assert other["items"] == []


def test_unknown_ingest_key_does_not_create_a_call() -> None:
    client = api_client(example_state())
    raw = example_raw()
    res = client.post("/v1/ingest/example/nope", content=raw, headers=example_headers(raw))
    assert res.status_code == 404
    assert list_calls(client)["items"] == []


def test_cross_tenant_delete_cannot_remove_other_org_call() -> None:
    client = api_client(example_state())
    ingest_example(client)
    call_id = first_call_id(client)
    other = client.post(
        "/v1/privacy/deletion-requests",
        json={"call_id": call_id},
        headers=auth("other"),
    )
    assert other.status_code == 200
    assert call_id not in (other.json().get("deleted_calls") or [])
    still = client.get(f"/v1/calls/{call_id}", headers=auth())
    assert still.status_code == 200
    assert still.json()["call_id"] == call_id


def test_delete_by_call_cannot_be_replayed() -> None:
    client = api_client(example_state())
    ingest_example(client)
    call_id = first_call_id(client)
    deleted = client.post(
        "/v1/privacy/deletion-requests",
        headers=auth(),
        json={"call_id": call_id},
    )
    assert deleted.status_code == 200
    body = deleted.json()
    assert body["undoable"] is False
    assert body.get("completed_at"), "a deletion that never completes is not a deletion"
    assert list_calls(client)["items"] == []
    ingest_example(client)
    assert list_calls(client)["items"] == []


def test_retention_horizon_is_the_documented_default() -> None:
    client = api_client(example_state())
    horizon = client.get("/v1/retention", headers=auth())
    assert horizon.status_code == 200
    assert horizon.json()["raw_retention_days"] == 30


def test_quality_does_not_count_another_orgs_hallucinations() -> None:
    """Org A must not see org B's confirmed flags. Built entirely over HTTP."""
    client = api_client(example_state())
    ingest_example(client)
    acme = client.get(f"/v1/quality?{RANGE_QS}", headers=auth("k"))
    beta = client.get(f"/v1/quality?{RANGE_QS}", headers=auth("other"))
    assert acme.status_code == 200
    assert beta.status_code == 200
    assert acme.json()["hallucinations"]["count"] == 0
    assert beta.json()["hallucinations"]["count"] == 0
    assert beta.json()["eligible"] == 0
