"""Medium tests: roles and scopes. Owner vs admin cannot be inferred from scopes alone."""

from __future__ import annotations

from fastapi.testclient import TestClient

from obsalt.api import create_test_app
from obsalt.config import Settings
from obsalt.domain.enums import KeyScope, Role
from obsalt.runtime import in_memory_state
from obsalt.security.sessions import ROLE_SCOPES, read_session, sign_session


def _client() -> tuple[TestClient, object]:
    settings = Settings(environment="test")
    state = in_memory_state(settings)
    return TestClient(create_test_app(settings, state)), state


def test_reviewer_cannot_rotate_keys_or_replay() -> None:
    client, state = _client()
    state.keys["reviewer"] = ("dev", frozenset({KeyScope.READ}))
    headers = {"X-API-Key": "reviewer"}
    assert client.post("/v1/keys/rotate", headers=headers, json={}).status_code == 403
    assert client.post("/v1/replay", headers=headers, json={}).status_code == 403
    listed = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z",
        headers=headers,
    )
    assert listed.status_code == 200


def test_analyst_can_analyze_admin_cannot_rotate() -> None:
    client, state = _client()
    state.keys["analyst"] = ("dev", frozenset({KeyScope.READ, KeyScope.ANALYZE}))
    state.keys["admin"] = ("dev", ROLE_SCOPES[Role.ADMIN])
    missing = client.post("/v1/calls/no-such-call/analyze", headers={"X-API-Key": "analyst"})
    assert missing.status_code == 404
    state.keys["reviewer"] = ("dev", frozenset({KeyScope.READ}))
    denied = client.post("/v1/calls/no-such-call/analyze", headers={"X-API-Key": "reviewer"})
    assert denied.status_code == 403
    rotated = client.post("/v1/keys/rotate", headers={"X-API-Key": "admin"}, json={})
    assert rotated.status_code == 403
    owner = client.post(
        "/v1/keys/rotate", headers={"X-API-Key": "dev-key"}, json={"overlap_seconds": 30}
    )
    assert owner.status_code == 200


def test_reviewer_session_cannot_replay() -> None:
    client, state = _client()
    token = sign_session("dev", state.settings.session_secret, role=Role.REVIEWER)
    info = read_session(token, state.settings.session_secret)
    assert info is not None
    res = client.post(
        "/v1/ui/replay",
        data={"csrf": info.csrf},
        cookies={"obsalt_session": token},
    )
    assert res.status_code == 403
    analyst = sign_session("dev", state.settings.session_secret, role=Role.ANALYST)
    settings = client.get("/v1/ui/settings", cookies={"obsalt_session": analyst})
    assert settings.status_code == 200
    csrf = info.csrf
    assert (
        client.post(
            "/v1/ui/connections",
            data={"csrf": csrf, "provider": "vapi"},
            cookies={"obsalt_session": token},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/v1/ui/calls/missing/delete",
            data={"csrf": csrf, "confirm": "missing"},
            cookies={"obsalt_session": token},
        ).status_code
        == 403
    )
    analyst_info = read_session(analyst, state.settings.session_secret)
    assert analyst_info is not None
    assert (
        client.post(
            "/v1/ui/keys/rotate",
            data={"csrf": analyst_info.csrf, "current_key": "dev-key"},
            cookies={"obsalt_session": analyst},
        ).status_code
        == 403
    )
