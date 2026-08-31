"""Per-org eval runners. Env OBSALT_JUDGE_* is bootstrap when no row exists."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from obsalt.analysis.judge import OpenAICompatibleJudge, judge_from_settings
from obsalt.domain.enums import EvalRunnerKind, EvalSlot
from obsalt.domain.models import EvalPolicy, EvalRunner
from obsalt.egress import validate_destination
from obsalt.util import new_id, utcnow

DEFAULT_ENABLED_SAMPLE_RATE = 0.05


@dataclass
class MemoryEvalRunnerStore:
    runners: dict[str, EvalRunner] = field(default_factory=dict)
    policies: dict[str, EvalPolicy] = field(default_factory=dict)

    def list(self, org_id: str) -> list[EvalRunner]:
        return [item for item in self.runners.values() if item.org_id == org_id]

    def get(self, org_id: str, runner_id: str) -> EvalRunner | None:
        item = self.runners.get(runner_id)
        if item is None or item.org_id != org_id:
            return None
        return item

    def get_slot(self, org_id: str, slot: EvalSlot | str) -> EvalRunner | None:
        wanted = EvalSlot(slot)
        for item in self.list(org_id):
            if item.slot is wanted:
                return item
        return None

    def upsert(self, runner: EvalRunner) -> EvalRunner:
        existing = self.get_slot(runner.org_id, runner.slot)
        if existing is not None:
            runner = runner.model_copy(
                update={"id": existing.id, "created_at": existing.created_at}
            )
        self.runners[runner.id] = runner
        return runner

    def delete(self, org_id: str, runner_id: str) -> bool:
        item = self.get(org_id, runner_id)
        if item is None:
            return False
        del self.runners[runner_id]
        return True

    def get_policy(self, org_id: str) -> EvalPolicy | None:
        return self.policies.get(org_id)

    def set_policy(self, policy: EvalPolicy) -> EvalPolicy:
        self.policies[policy.org_id] = policy
        return policy


def runner_from_body(
    org_id: str, body: dict[str, Any], *, existing: EvalRunner | None = None
) -> EvalRunner:
    slot = EvalSlot(
        str(body.get("slot") or (existing.slot.value if existing else EvalSlot.CHEAP.value))
    )
    kind = EvalRunnerKind(
        str(
            body.get("kind")
            or (existing.kind.value if existing else EvalRunnerKind.OPENAI_COMPATIBLE.value)
        )
    )
    if kind is not EvalRunnerKind.OPENAI_COMPATIBLE:
        raise ValueError("kind must be openai_compatible")
    api_key = str(body.get("api_key") or "")
    if existing is not None and not api_key:
        api_key = existing.api_key
    if not api_key:
        raise ValueError("api_key is required")
    base_url = str(body.get("base_url") or (existing.base_url if existing else "")).rstrip("/")
    if not base_url:
        raise ValueError("base_url is required")
    model = str(body.get("model") or (existing.model if existing else "") or "gpt-4.1-mini")
    allow = body.get("allow_http_localhost")
    if allow is None:
        allow_http = bool(existing.allow_http_localhost) if existing else False
    else:
        allow_http = str(allow).lower() in {"1", "true", "on", "yes"}
    validate_destination(f"{base_url}/chat/completions", allow_http_localhost=allow_http)
    return EvalRunner(
        id=existing.id if existing else new_id(),
        org_id=org_id,
        slot=slot,
        kind=kind,
        base_url=base_url,
        model=model,
        api_key=api_key,
        allow_http_localhost=allow_http,
        created_at=existing.created_at if existing else utcnow(),
    )


def validate_policy(policy: EvalPolicy, runners: list[EvalRunner]) -> EvalPolicy:
    from obsalt.analysis.pack import normalize_pack

    pack = list(normalize_pack(policy.pack))
    policy = policy.model_copy(update={"pack": pack})
    if policy.monthly_budget_usd < 0:
        raise ValueError("monthly_budget_usd must be >= 0")
    rate = float(policy.baseline_sample_rate)
    if rate < 0 or rate > 1:
        raise ValueError("baseline_sample_rate must be between 0 and 1")
    grounded = float(policy.groundedness_sample_rate)
    if grounded < 0 or grounded > 1:
        raise ValueError("groundedness_sample_rate must be between 0 and 1")
    if policy.llm_evals_enabled:
        if policy.monthly_budget_usd <= 0:
            raise ValueError("set a monthly cap greater than 0 before enabling LLM evals")
        if not runners:
            raise ValueError("add a runner before enabling LLM evals")
        if rate == 0:
            policy = policy.model_copy(update={"baseline_sample_rate": DEFAULT_ENABLED_SAMPLE_RATE})
    return policy


def store_of(state: Any) -> MemoryEvalRunnerStore | Any | None:
    return state.eval_runner_store


def list_runners(state: Any, org_id: str) -> list[EvalRunner]:
    store = store_of(state)
    if store is None:
        return []
    return list(store.list(org_id))


def get_policy(state: Any, org_id: str) -> EvalPolicy:
    store = store_of(state)
    stored = store.get_policy(org_id) if store is not None else None
    if stored is not None:
        return stored
    settings = getattr(state, "settings", None)
    return EvalPolicy(
        org_id=org_id,
        monthly_budget_usd=float(getattr(settings, "llm_monthly_budget_usd", 0.0) or 0.0),
        baseline_sample_rate=float(getattr(settings, "baseline_sample_rate", 0.0) or 0.0),
        llm_evals_enabled=False,
        groundedness_enabled=bool(getattr(settings, "groundedness_enabled", False)),
        groundedness_sample_rate=float(getattr(settings, "groundedness_sample_rate", 0.0) or 0.0),
    )


def llm_evals_on(state: Any, org_id: str) -> bool:
    policy = get_policy(state, org_id)
    return bool(
        policy.llm_evals_enabled and policy.monthly_budget_usd > 0 and list_runners(state, org_id)
    )


def expensive_judge_if_distinct(state: Any, org_id: str) -> Any | None:
    """Second judge only when an expensive runner exists and differs from cheap."""
    if not llm_evals_on(state, org_id):
        return None
    store = store_of(state)
    if store is None:
        return None
    expensive = store.get_slot(org_id, EvalSlot.EXPENSIVE)
    if expensive is None:
        return None
    cheap = store.get_slot(org_id, EvalSlot.CHEAP)
    if cheap is not None and (
        cheap.base_url == expensive.base_url
        and cheap.model == expensive.model
        and cheap.api_key == expensive.api_key
    ):
        return None
    return resolve_judge(state, org_id, slot=EvalSlot.EXPENSIVE)


def resolve_judge(state: Any, org_id: str, *, slot: EvalSlot | str = EvalSlot.CHEAP) -> Any:
    """Org runner if LLM evals are enabled; else env bootstrap; else None."""
    if llm_evals_on(state, org_id):
        store = store_of(state)
        runner = store.get_slot(org_id, slot) if store is not None else None
        if runner is None and store is not None:
            runner = store.get_slot(org_id, EvalSlot.CHEAP) or store.get_slot(
                org_id, EvalSlot.EXPENSIVE
            )
        if runner is not None:
            return OpenAICompatibleJudge(
                base_url=runner.base_url,
                api_key=runner.api_key,
                model=runner.model,
                allow_http_localhost=runner.allow_http_localhost,
            )
    settings = getattr(state, "settings", None)
    return judge_from_settings(settings)


def org_eval_budget(state: Any, org_id: str) -> float:
    return float(get_policy(state, org_id).monthly_budget_usd)


def org_eval_sample_rate(state: Any, org_id: str) -> float:
    return float(get_policy(state, org_id).baseline_sample_rate)


def org_groundedness_enabled(state: Any, org_id: str) -> bool:
    return bool(get_policy(state, org_id).groundedness_enabled)


def org_groundedness_sample_rate(state: Any, org_id: str) -> float:
    return float(get_policy(state, org_id).groundedness_sample_rate)


def enabled_pack(state: Any, org_id: str) -> tuple[str, ...]:
    from obsalt.analysis.pack import normalize_pack

    return normalize_pack(get_policy(state, org_id).pack)


async def ping_runner(runner: EvalRunner) -> dict[str, Any]:
    url = f"{runner.base_url.rstrip('/')}/chat/completions"
    validate_destination(url, allow_http_localhost=runner.allow_http_localhost)
    body = {
        "model": runner.model,
        "temperature": 0,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
        response = await client.post(
            url,
            json=body,
            headers={
                "Authorization": f"Bearer {runner.api_key}",
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
    return {"ok": True, "status": response.status_code, "model": runner.model}


def enable_blocked_reason(state: Any, org_id: str) -> str | None:
    runners = list_runners(state, org_id)
    policy = get_policy(state, org_id)
    if not runners:
        return "Add a cheap or expensive runner."
    if policy.monthly_budget_usd <= 0:
        return "Set a monthly cap greater than $0."
    return None
