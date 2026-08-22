"""Pluggable judges. Heuristic is the offline default; LLM is OpenAI-compatible."""

from __future__ import annotations

import json
from typing import Any

import httpx

from obsalt.egress import EgressDenied, validate_destination
from obsalt.plugin.types import JudgeRequest, JudgeResult

HEURISTIC_VERSION = "heuristic/1"
LLM_PROMPT_VERSION = "judge/1"


class HeuristicJudge:
    """Offline default so evaluate-on-click works without a bill."""

    name = "heuristic"
    version = HEURISTIC_VERSION

    async def judge(self, request: JudgeRequest) -> JudgeResult:
        text = request.rubric_text.lower()
        transcript = request.transcript.lower()
        grounding = "\n".join(request.grounding).lower()
        score = 1.0
        quotes: list[str] = []
        if "hallucin" in text or "invent" in text:
            if any(token in transcript for token in ("ord-", "$")):
                if not any(token in grounding for token in ("ord-", "$")):
                    score -= 0.5
                    quotes.append("ungrounded identifier or price")
        contradicted = ("error" in grounding or "not_found" in grounding) and (
            "refund" in transcript or "processed" in transcript
        )
        if contradicted:
            score = min(score, 0.2)
            quotes.append("tool/knowledge corpus contradicts the agent claim")
        passed = score >= 0.7
        return JudgeResult(
            score=score,
            passed=passed,
            rationale="heuristic",
            quotes=quotes,
            model=HEURISTIC_VERSION,
            prompt_version="heuristic/1",
        )


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
                        "score (0-1 float), passed (bool), rationale (string), "
                        "quotes (array of evidence spans)."
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
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
        content = _message_content(payload)
        parsed = _parse_structured(content)
        return JudgeResult(
            score=float(parsed.get("score") or 0.0),
            passed=bool(parsed.get("passed")),
            rationale=str(parsed.get("rationale") or content),
            quotes=[str(item) for item in parsed.get("quotes") or []],
            model=request.model or self.model,
            prompt_version=LLM_PROMPT_VERSION,
        )


def judge_from_settings(
    settings: Any | None = None,
    *,
    judge_url: str | None = None,
    judge_api_key: str | None = None,
    judge_model: str | None = None,
) -> HeuristicJudge | OpenAICompatibleJudge:
    url = judge_url or getattr(settings, "judge_base_url", None)
    key = judge_api_key or getattr(settings, "judge_api_key", None)
    model = judge_model or getattr(settings, "judge_model", None) or "gpt-4.1-mini"
    if url and key:
        try:
            return OpenAICompatibleJudge(base_url=str(url), api_key=str(key), model=str(model))
        except EgressDenied:
            return HeuristicJudge()
    return HeuristicJudge()


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
        return {"score": 0.0, "passed": False, "rationale": content, "quotes": []}
    return data if isinstance(data, dict) else {"rationale": content}
