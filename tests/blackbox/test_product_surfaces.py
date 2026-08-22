"""Black-box product surfaces: filters, rubrics, keys, UI tenancy, review."""

from __future__ import annotations

from fastapi.testclient import TestClient

from obsalt.api import create_app
from obsalt.config import Settings
from obsalt.domain.enums import MeasurementPlacement, Metric, Provenance, Stage
from obsalt.domain.events import CallObserved, StageObserved
from obsalt.privacy.caller import DEFAULT_PEPPER, caller_token
from obsalt.runtime import in_memory_state
from obsalt.worker.process import process_normalized_events
from tests.helpers import example_raw, example_state, fidelity_declaration, signed_example_headers


def test_call_list_filters_and_retention_api() -> None:
    state = example_state()
    process_normalized_events(
        [
            CallObserved(source_call_id="c1", agent_id="support"),
            StageObserved(
                stage=Stage.E2E,
                metric=Metric.DURATION,
                value_ms=900,
                placement=MeasurementPlacement.UNPLACED,
                provenance=Provenance.PROVIDER_REPORTED,
                source_path="e2e",
            ),
        ],
        org_id="acme",
        source="example",
        source_call_id="c1",
        envelope_id="e1",
        declaration=fidelity_declaration(),
        pointers=state.pointers,
        sink=state.sink,
        decoder_version="t/1",
    )
    client = TestClient(create_app(Settings(environment="test"), state))
    listed = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z&agent_id=support&latency_ms=100",
        headers={"X-API-Key": "k"},
    )
    assert listed.status_code == 200
    assert listed.json()["items"]
    empty = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z&agent_id=other",
        headers={"X-API-Key": "k"},
    )
    assert empty.json()["items"] == []
    horizon = client.get("/v1/retention", headers={"X-API-Key": "k"})
    assert horizon.status_code == 200
    assert horizon.json()["raw_retention_days"] == 30


def test_plugins_endpoint_requires_api_key() -> None:
    client = TestClient(create_app(Settings(environment="test"), example_state()))
    assert client.get("/v1/plugins").status_code == 401
    assert client.get("/v1/plugins", headers={"X-API-Key": "k"}).status_code == 200


def test_rubric_put_increments_version_on_same_id() -> None:
    client = TestClient(create_app(Settings(environment="test"), example_state()))
    created = client.post(
        "/v1/rubrics",
        headers={"X-API-Key": "k"},
        json={"name": "grounded", "description": "no invented facts"},
    )
    assert created.status_code == 200
    rubric_id = created.json()["id"]
    updated = client.put(
        f"/v1/rubrics/{rubric_id}",
        headers={"X-API-Key": "k"},
        json={"name": "grounded-stricter", "description": "stricter"},
    )
    assert updated.status_code == 200
    assert updated.json()["id"] == rubric_id
    assert updated.json()["version"] == 2


def test_review_and_caller_token() -> None:
    client = TestClient(create_app(Settings(environment="test"), example_state()))
    res = client.post(
        "/v1/quality/review",
        headers={"X-API-Key": "k"},
        json={"call_id": "missing-is-ok", "agree": False},
    )
    assert res.status_code == 200
    assert res.json()["status"] == "recorded"
    token = caller_token("acme", "+1555", DEFAULT_PEPPER)
    assert len(token) == 64
    assert token == caller_token("acme", "+1555", DEFAULT_PEPPER)


def test_ui_does_not_collapse_to_first_org_without_session() -> None:
    state = example_state()
    client = TestClient(create_app(Settings(environment="test"), state))
    raw = example_raw()
    client.post("/v1/ingest/example/ik", content=raw, headers=signed_example_headers(raw))
    page = client.get("/v1/ui")
    assert page.status_code == 200
    assert b"ex-1" not in page.content


def test_key_rotation_keeps_overlap() -> None:
    settings = Settings(environment="test")
    state = in_memory_state(settings)
    client = TestClient(create_app(settings, state))
    rotated = client.post(
        "/v1/keys/rotate", headers={"X-API-Key": "dev-key"}, json={"overlap_seconds": 60}
    )
    assert rotated.status_code == 200
    new_key = rotated.json()["key"]
    listed = client.get("/v1/plugins", headers={"X-API-Key": "dev-key"})
    assert listed.status_code == 200
    listed_new = client.get("/v1/plugins", headers={"X-API-Key": new_key})
    assert listed_new.status_code == 200
