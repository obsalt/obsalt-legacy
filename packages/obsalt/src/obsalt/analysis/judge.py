"""LLM judges. Cheap software verdicts live in ``detect_claims``, not here.

There is no heuristic stand-in. Without a runner, callers pass ``judge=None``
and leave English rows ``not_judged``. Historical ``heuristic/1`` payloads stay
unconfirmed via ``is_heuristic_result``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx

from obsalt.domain.enums import JudgeVerdict
from obsalt.egress import EgressDenied, validate_destination
from obsalt.plugin.types import JudgeRequest, JudgeResult

LLM_PROMPT_VERSION = "judge/1"


def is_heuristic_result(payload: Mapping[str, Any] | None, execution: Any | None = None) -> bool:
    """True when a stored row came from the removed regex stand-in. Never confirmed."""
    for raw in (
        (payload or {}).get("model"),
        getattr(execution, "judge_version", None),
    ):
        if str(raw or "").startswith("heuristic"):
            return True
    for bucket in ((payload or {}).get("claims"), (payload or {}).get("candidates")):
        for item in bucket or []:
            if isinstance(item, dict) and str(item.get("model") or "").startswith("heuristic"):
                return True
    return False


class OpenAICompatibleJudge:
    """Any OpenAI-compatible chat/completions endpoint, including a local model."""

    name = "openai_compatible"
    version = "openai-compatible/1"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str = "claude-or-compatible",
        allow_http_localhost: bool = False,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.allow_http_localhost = allow_http_localhost
        self.timeout = timeout

    async def judge(self, request: JudgeRequest) -> JudgeResult:
        url = f"{self.base_url}/chat/completions"
        validate_destination(url, allow_http_localhost=self.allow_http_localhost)
        body = {
            "model": request.model or self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Judge the call against the rubric. Return JSON with keys "
                        "verdict (pass, fail, maybe, not_applicable, evidence_missing), "
                        "score (0-1 float), passed (bool, true only when verdict is pass), "
                        "rationale (string), quotes (array of evidence spans). "
                        "Use maybe when unsure. Missing evidence is evidence_missing, never pass."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Rubric v{request.rubric_version}: {request.rubric_text}\n\n"
                        f"Transcript:\n{request.transcript}\n\n"
                        f"Grounding:\n" + "\n".join(request.grounding)
                    ),
                },
            ],
        }
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
            response = await client.post(
                url,
                json=body,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
            response.raise_for_status()
            payload = response.json()
        content = _message_content(payload)
        parsed = _parse_structured(content)
        return _result(
            _parse_verdict(parsed),
            score=float(parsed.get("score") or 0.0),
            rationale=str(parsed.get("rationale") or content),
            quotes=[str(item) for item in parsed.get("quotes") or []],
            model=request.model or self.model,
            prompt_version=LLM_PROMPT_VERSION,
            cost_usd=_usage_cost_usd(payload, parsed),
        )


def judge_from_settings(
    settings: Any | None = None,
    *,
    judge_url: str | None = None,
    judge_api_key: str | None = None,
    judge_model: str | None = None,
) -> OpenAICompatibleJudge | None:
    url = judge_url or getattr(settings, "judge_base_url", None)
    key = judge_api_key or getattr(settings, "judge_api_key", None)
    model = judge_model or getattr(settings, "judge_model", None) or "gpt-4.1-mini"
    if url and key:
        try:
            return OpenAICompatibleJudge(base_url=str(url), api_key=str(key), model=str(model))
        except EgressDenied:
            return None
    return None


def _result(
    verdict: JudgeVerdict,
    *,
    score: float,
    rationale: str,
    quotes: list[str] | None = None,
    model: str = "",
    prompt_version: str,
    cost_usd: float | None = None,
) -> JudgeResult:
    if verdict is JudgeVerdict.PASS:
        passed: bool | None = True
    elif verdict is JudgeVerdict.FAIL:
        passed = False
    else:
        passed = None
    return JudgeResult(
        score=score,
        passed=passed,
        verdict=verdict,
        rationale=rationale,
        quotes=list(quotes or []),
        model=model,
        prompt_version=prompt_version,
        cost_usd=cost_usd,
    )


def _parse_verdict(parsed: dict[str, Any]) -> JudgeVerdict:
    raw = str(parsed.get("verdict") or "").strip().lower()
    if raw in {item.value for item in JudgeVerdict}:
        return JudgeVerdict(raw)
    if parsed.get("passed") is True:
        return JudgeVerdict.PASS
    if parsed.get("passed") is False:
        return JudgeVerdict.FAIL
    return JudgeVerdict.NOT_JUDGED


def _usage_cost_usd(payload: dict[str, Any], parsed: dict[str, Any]) -> float | None:
    """Use a cost the judge or gateway actually reported. Do not invent a token price."""
    raw_usage = payload.get("usage")
    usage = raw_usage if isinstance(raw_usage, dict) else {}
    raw = usage.get("cost") or usage.get("total_cost") or parsed.get("cost_usd")
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _message_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return str(message.get("content") or "")


def _parse_structured(content: str) -> dict[str, Any]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return {"rationale": content, "quotes": []}
    return data if isinstance(data, dict) else {"rationale": content}
