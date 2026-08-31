"""HTTP: eval runners and policy. Admin writes; keys never listed."""

from __future__ import annotations

from obsalt.domain.enums import KeyScope, Role
from tests.helpers import api_client, auth, example_state, first_call_id, ingest_example


def test_eval_runner_create_lists_without_secret() -> None:
    client = api_client(example_state())
    created = client.post(
        "/v1/eval-runners",
        headers=auth(),
        json={
            "slot": "cheap",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4.1-mini",
            "api_key": "sk-never-list-me",
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["has_api_key"] is True
    assert "api_key" not in body
    listed = client.get("/v1/eval-runners", headers=auth())
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert len(items) == 1
    assert items[0]["model"] == "gpt-4.1-mini"
    assert "api_key" not in items[0]
    assert listed.json()["policy"]["llm_evals_enabled"] is False
    assert listed.json()["policy"]["pack"] == ["accuracy", "tool_use", "conciseness", "safety"]


def test_eval_policy_rejects_unknown_pack_judge() -> None:
    client = api_client(example_state())
    client.post(
        "/v1/eval-runners",
        headers=auth(),
        json={
            "slot": "cheap",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4.1-mini",
            "api_key": "sk-test",
        },
    )
    bad = client.put(
        "/v1/eval-policy",
        headers=auth(),
        json={"monthly_budget_usd": 1, "llm_evals_enabled": True, "pack": ["not-a-judge"]},
    )
    assert bad.status_code == 400
    ok = client.put(
        "/v1/eval-policy",
        headers=auth(),
        json={
            "monthly_budget_usd": 1,
            "llm_evals_enabled": True,
            "pack": ["accuracy", "handoff"],
        },
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["pack"] == ["accuracy", "handoff"]


def test_eval_policy_groundedness_round_trip() -> None:
    client = api_client(example_state())
    bad = client.put(
        "/v1/eval-policy",
        headers=auth(),
        json={"groundedness_sample_rate": 2},
    )
    assert bad.status_code == 400
    ok = client.put(
        "/v1/eval-policy",
        headers=auth(),
        json={"groundedness_enabled": True, "groundedness_sample_rate": 0.01},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["groundedness_enabled"] is True
    assert ok.json()["groundedness_sample_rate"] == 0.01
    listed = client.get("/v1/eval-runners", headers=auth())
    assert listed.json()["policy"]["groundedness_enabled"] is True


def test_enable_without_cap_is_400() -> None:
    client = api_client(example_state())
    client.post(
        "/v1/eval-runners",
        headers=auth(),
        json={
            "slot": "cheap",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4.1-mini",
            "api_key": "sk-test",
        },
    )
    blocked = client.put(
        "/v1/eval-policy",
        headers=auth(),
        json={"llm_evals_enabled": True, "monthly_budget_usd": 0},
    )
    assert blocked.status_code == 400
    enabled = client.put(
        "/v1/eval-policy",
        headers=auth(),
        json={"llm_evals_enabled": True, "monthly_budget_usd": 12},
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["llm_evals_enabled"] is True
    assert enabled.json()["baseline_sample_rate"] == 0.05


def test_analyst_cannot_write_eval_runners() -> None:
    state = example_state()
    state.keys["analyst"] = ("acme", frozenset({KeyScope.READ, KeyScope.ANALYZE}))
    state.key_roles["analyst"] = Role.ANALYST
    client = api_client(state)
    denied = client.post(
        "/v1/eval-runners",
        headers=auth("analyst"),
        json={
            "slot": "cheap",
            "base_url": "https://api.openai.com/v1",
            "model": "m",
            "api_key": "k",
        },
    )
    assert denied.status_code == 403
    listed = client.get("/v1/eval-runners", headers=auth("analyst"))
    assert listed.status_code == 200


def test_console_settings_creates_eval_runner() -> None:
    from obsalt.domain.enums import Role
    from obsalt.security.sessions import read_session, sign_session

    state = example_state()
    client = api_client(state)
    token = sign_session("dev", state.settings.session_secret, role=Role.OWNER)
    info = read_session(token, state.settings.session_secret)
    assert info is not None
    created = client.post(
        "/v1/ui/eval-runners",
        data={
            "csrf": info.csrf,
            "slot": "cheap",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4.1-mini",
            "api_key": "sk-ui",
        },
        cookies={"obsalt_session": token},
        follow_redirects=False,
    )
    assert created.status_code == 303
    settings = client.get("/v1/ui/settings", cookies={"obsalt_session": token})
    assert settings.status_code == 200
    assert "gpt-4.1-mini" in settings.text
    assert "sk-ui" not in settings.text
    assert "Evals" in settings.text
    assert "LiveKit pack" in settings.text
    assert "accuracy" in settings.text
    assert "New predicate" in settings.text


def test_predicate_rubric_evaluates_without_llm() -> None:
    client = api_client(example_state())
    ingest_example(client)
    call_id = first_call_id(client)
    created = client.post(
        "/v1/rubrics",
        headers=auth(),
        json={
            "name": "disclosure",
            "kind": "predicate",
            "spec": {"all": [{"phrase_in_agent": "hello"}]},
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["kind"] == "predicate"
    analyzed = client.post(f"/v1/calls/{call_id}/analyze", headers=auth())
    assert analyzed.status_code == 200
    rows = [
        item
        for item in analyzed.json()["items"]
        if item.get("execution", {}).get("analyzer_id", "").startswith("rubric:")
    ]
    assert rows
    payload = rows[0]["payload"]
    assert payload.get("shadow") is False
    assert payload.get("verdict") in {"pass", "fail", "evidence_missing"}
