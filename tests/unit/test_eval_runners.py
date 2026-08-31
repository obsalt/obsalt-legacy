"""Per-org eval runners. Env is bootstrap; English stays not_judged until enabled."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obsalt.analysis.judge import OpenAICompatibleJudge
from obsalt.analysis.runners import (
    DEFAULT_ENABLED_SAMPLE_RATE,
    MemoryEvalRunnerStore,
    expensive_judge_if_distinct,
    get_policy,
    llm_evals_on,
    ping_runner,
    resolve_judge,
    runner_from_body,
    validate_policy,
)
from obsalt.config import Settings
from obsalt.domain.enums import EvalSlot
from obsalt.domain.models import EvalPolicy, EvalRunner
from obsalt.egress import EgressDenied
from obsalt.runtime import in_memory_state


def test_english_stays_none_until_enabled() -> None:
    state = in_memory_state()
    judge = resolve_judge(state, "dev")
    assert judge is None
    store = state.eval_runner_store
    store.upsert(
        EvalRunner(
            id="r1",
            org_id="dev",
            slot=EvalSlot.CHEAP,
            base_url="https://api.openai.com/v1",
            model="gpt-4.1-mini",
            api_key="sk-test",
        )
    )
    assert llm_evals_on(state, "dev") is False
    assert resolve_judge(state, "dev") is None
    store.set_policy(
        validate_policy(
            EvalPolicy(org_id="dev", monthly_budget_usd=5.0, llm_evals_enabled=True),
            store.list("dev"),
        )
    )
    judge = resolve_judge(state, "dev")
    assert isinstance(judge, OpenAICompatibleJudge)
    assert judge.model == "gpt-4.1-mini"


def test_enable_requires_runner_and_cap() -> None:
    with pytest.raises(ValueError, match="cap"):
        validate_policy(EvalPolicy(org_id="dev", monthly_budget_usd=0, llm_evals_enabled=True), [])
    runner = EvalRunner(
        id="r1",
        org_id="dev",
        slot=EvalSlot.CHEAP,
        base_url="https://api.openai.com/v1",
        model="m",
        api_key="k",
    )
    with pytest.raises(ValueError, match="runner"):
        validate_policy(EvalPolicy(org_id="dev", monthly_budget_usd=1, llm_evals_enabled=True), [])
    policy = validate_policy(
        EvalPolicy(
            org_id="dev",
            monthly_budget_usd=1,
            baseline_sample_rate=0,
            llm_evals_enabled=True,
        ),
        [runner],
    )
    assert policy.baseline_sample_rate == DEFAULT_ENABLED_SAMPLE_RATE


def test_public_dict_omits_api_key() -> None:
    runner = EvalRunner(
        id="r1",
        org_id="dev",
        slot=EvalSlot.CHEAP,
        base_url="https://api.openai.com/v1",
        model="m",
        api_key="sk-secret",
    )
    public = runner.public_dict()
    assert "api_key" not in public
    assert public["has_api_key"] is True


def test_runner_from_body_rejects_internal_url() -> None:
    with pytest.raises(EgressDenied):
        runner_from_body(
            "dev",
            {
                "slot": "cheap",
                "base_url": "http://127.0.0.1:9",
                "model": "m",
                "api_key": "k",
            },
        )


def test_get_policy_falls_back_to_env() -> None:
    settings = Settings(
        llm_monthly_budget_usd=3.5,
        baseline_sample_rate=0.1,
        groundedness_enabled=True,
        groundedness_sample_rate=0.02,
    )
    state = in_memory_state(settings)
    policy = get_policy(state, "dev")
    assert policy.monthly_budget_usd == 3.5
    assert policy.llm_evals_enabled is False
    assert policy.groundedness_enabled is True
    assert policy.groundedness_sample_rate == 0.02


def test_ping_runner_posts_chat_completions() -> None:
    runner = EvalRunner(
        id="r1",
        org_id="dev",
        slot=EvalSlot.CHEAP,
        base_url="https://api.openai.com/v1",
        model="gpt-4.1-mini",
        api_key="sk-test",
    )
    response = MagicMock()
    response.status_code = 200
    response.raise_for_status = MagicMock()
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    with patch("obsalt.analysis.runners.httpx.AsyncClient", return_value=client):
        out = asyncio.run(ping_runner(runner))
    assert out["ok"] is True
    client.post.assert_awaited()
    args, kwargs = client.post.await_args
    assert args[0].endswith("/chat/completions")
    assert kwargs["headers"]["Authorization"] == "Bearer sk-test"


def test_expensive_judge_requires_distinct_enabled_runner() -> None:
    state = in_memory_state()
    store = state.eval_runner_store
    cheap = EvalRunner(
        id="c",
        org_id="dev",
        slot=EvalSlot.CHEAP,
        base_url="https://api.openai.com/v1",
        model="mini",
        api_key="k1",
    )
    store.upsert(cheap)
    store.set_policy(
        validate_policy(
            EvalPolicy(org_id="dev", monthly_budget_usd=5.0, llm_evals_enabled=True),
            store.list("dev"),
        )
    )
    assert expensive_judge_if_distinct(state, "dev") is None
    store.upsert(
        EvalRunner(
            id="e",
            org_id="dev",
            slot=EvalSlot.EXPENSIVE,
            base_url="https://api.openai.com/v1",
            model="strong",
            api_key="k2",
        )
    )
    judge = expensive_judge_if_distinct(state, "dev")
    assert isinstance(judge, OpenAICompatibleJudge)
    assert judge.model == "strong"


def test_memory_store_upserts_same_slot() -> None:
    store = MemoryEvalRunnerStore()
    first = store.upsert(
        EvalRunner(
            id="a",
            org_id="dev",
            slot=EvalSlot.CHEAP,
            base_url="https://api.openai.com/v1",
            model="one",
            api_key="k1",
        )
    )
    second = store.upsert(
        EvalRunner(
            id="b",
            org_id="dev",
            slot=EvalSlot.CHEAP,
            base_url="https://api.openai.com/v1",
            model="two",
            api_key="k2",
        )
    )
    assert second.id == first.id
    assert store.get_slot("dev", EvalSlot.CHEAP).model == "two"
    assert len(store.list("dev")) == 1
