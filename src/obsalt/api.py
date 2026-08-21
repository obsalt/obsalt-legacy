from __future__ import annotations

import json

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from obsalt._version import __version__
from obsalt.config import Settings
from obsalt.domain.enums import Provider, parse_provider
from obsalt.evals.judges import new_rubric
from obsalt.pipeline import IngestPipeline
from obsalt.security import header_map, verify_bland, verify_retell, verify_vapi
from obsalt.store import MemoryStore, Store
from obsalt.tracing.setup import setup_tracing


class RubricIn(BaseModel):
    name: str
    description: str
    threshold: float = 0.7


class SearchIn(BaseModel):
    query: str
    limit: int = Field(default=10, ge=1, le=100)


def _org_from_key(settings: Settings, authorization: str | None, x_api_key: str | None) -> str:
    keys = settings.parsed_api_keys()
    token = x_api_key
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if token and token in keys:
        return keys[token]
    if settings.require_auth:
        raise HTTPException(status_code=401, detail="invalid api key")
    if token and keys:
        raise HTTPException(status_code=401, detail="invalid api key")
    return next(iter(keys.values()), "demo")


def create_app(
    *,
    store: Store | None = None,
    pipeline: IngestPipeline | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    settings = settings or Settings()
    store = store or MemoryStore()
    if settings.otlp_endpoint:
        _, _, otel_metrics = setup_tracing(
            otlp_endpoint=settings.otlp_endpoint,
            batch=True,
            service_name=settings.service_name,
            environment=settings.environment,
        )
        pipeline = pipeline or IngestPipeline(
            store=store, metrics=otel_metrics, environment=settings.environment
        )
    else:
        pipeline = pipeline or IngestPipeline(store=store, environment=settings.environment)

    app = FastAPI(
        title="obsalt",
        version=__version__,
        description=(
            "Ingest voice-agent calls. Path A: Vapi / Retell / Bland webhooks. "
            "Path B: native snapshots from VoiceCall. "
            "Look up evidence over HTTP. Traces export over OpenTelemetry. "
            "This API is not a dashboard."
        ),
        openapi_tags=[
            {"name": "health", "description": "Liveness and operator-safe config."},
            {"name": "ingest", "description": "Provider webhooks and native snapshots."},
            {"name": "calls", "description": "Evidence lookup, search, and rollups."},
            {"name": "evals", "description": "Rubrics scored against stored calls."},
        ],
    )
    app.state.settings = settings
    app.state.store = store
    app.state.pipeline = pipeline

    def tenant(
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> str:
        return _org_from_key(settings, authorization, x_api_key)

    @app.get("/health", tags=["health"])
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "service": "obsalt",
            "version": __version__,
            "otlp_configured": bool(settings.otlp_endpoint),
            "require_auth": settings.require_auth,
            "environment": settings.environment,
            "store": "memory",
        }

    async def _ingest(provider: Provider, request: Request, org_id: str):
        body = await request.body()
        headers = header_map(request.headers)
        if provider == Provider.VAPI and not verify_vapi(headers, settings.vapi_secret):
            raise HTTPException(status_code=401, detail="invalid vapi signature")
        if provider == Provider.RETELL and not verify_retell(headers, body, settings.retell_secret):
            raise HTTPException(status_code=401, detail="invalid retell signature")
        if provider == Provider.BLAND and not verify_bland(headers, settings.bland_secret):
            raise HTTPException(status_code=401, detail="invalid bland signature")
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail="body must be utf-8 json") from exc
        if not text.strip():
            raise HTTPException(status_code=400, detail="json object required")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="invalid json") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="json object required")
        try:
            result = pipeline.ingest(provider, payload, org_id=org_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return result.model_dump()

    @app.post("/v1/ingest/vapi", tags=["ingest"])
    async def ingest_vapi(request: Request, org_id: str = Depends(tenant)):
        return await _ingest(Provider.VAPI, request, org_id)

    @app.post("/v1/ingest/retell", tags=["ingest"])
    async def ingest_retell(request: Request, org_id: str = Depends(tenant)):
        return await _ingest(Provider.RETELL, request, org_id)

    @app.post("/v1/ingest/bland", tags=["ingest"])
    async def ingest_bland(request: Request, org_id: str = Depends(tenant)):
        return await _ingest(Provider.BLAND, request, org_id)

    @app.post("/v1/ingest/openai-realtime", tags=["ingest"])
    async def ingest_openai(request: Request, org_id: str = Depends(tenant)):
        return await _ingest(Provider.OPENAI_REALTIME, request, org_id)

    @app.post("/v1/ingest/native", tags=["ingest"])
    async def ingest_native(request: Request, org_id: str = Depends(tenant)):
        return await _ingest(Provider.NATIVE, request, org_id)

    @app.get("/v1/calls", tags=["calls"])
    def list_calls(
        org_id: str = Depends(tenant),
        agent_id: str | None = Query(default=None),
        provider_call_id: str | None = Query(default=None),
        provider: str | None = Query(default=None),
    ):
        if provider_call_id and provider:
            try:
                provider_enum = parse_provider(provider)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            call = store.get_by_provider_id(org_id, provider_enum, provider_call_id)
            calls = [call] if call is not None else []
        else:
            calls = store.list_calls(org_id, agent_id=agent_id)
            if provider_call_id:
                calls = [c for c in calls if c.provider_call_id == provider_call_id]
        return {"calls": [_call_summary(c) for c in calls], "count": len(calls)}

    @app.get("/v1/calls/{call_id}", tags=["calls"])
    def get_call(call_id: str, org_id: str = Depends(tenant)):
        call = store.get_call(org_id, call_id)
        if call is None:
            raise HTTPException(status_code=404, detail="call not found")
        return call.model_dump(mode="json")

    @app.get("/v1/latency", tags=["calls"])
    def latency(org_id: str = Depends(tenant), agent_id: str | None = Query(default=None)):
        return {"components": [s.model_dump() for s in store.latency_rollup(org_id, agent_id=agent_id)]}

    @app.get("/v1/hangups", tags=["calls"])
    def hangups(org_id: str = Depends(tenant)):
        clusters = store.hangup_clusters(org_id)
        return {
            "clusters": [
                {
                    "key": c.key,
                    "reason": c.reason,
                    "party": c.party,
                    "last_utterance_theme": c.last_utterance_theme,
                    "count": c.count,
                    "avg_loss_score": c.avg_loss_score,
                    "example_call_ids": c.example_call_ids,
                    "lost_customer_call_id": c.lost_customer_call_id,
                }
                for c in clusters
            ]
        }

    @app.get("/v1/tools", tags=["calls"])
    def tools(org_id: str = Depends(tenant), agent_id: str | None = Query(default=None)):
        return {
            "tools": [
                {
                    "name": t.name,
                    "invocations": t.invocations,
                    "success_rate": t.success_rate,
                    "retry_rate": t.retry_rate,
                    "p50_ms": t.p50_ms,
                    "p95_ms": t.p95_ms,
                    "payload_shapes": t.payload_shapes,
                }
                for t in store.tool_rollup(org_id, agent_id=agent_id)
            ]
        }

    @app.post("/v1/search", tags=["calls"])
    def search(body: SearchIn, org_id: str = Depends(tenant)):
        hits = store.search(org_id, body.query, limit=body.limit)
        return {"hits": [{"call_id": h.call_id, "score": h.score, "snippet": h.snippet} for h in hits]}

    @app.get("/v1/search", tags=["calls"])
    def search_get(q: str = Query(min_length=1), limit: int = 10, org_id: str = Depends(tenant)):
        hits = store.search(org_id, q, limit=limit)
        return {"hits": [{"call_id": h.call_id, "score": h.score, "snippet": h.snippet} for h in hits]}

    @app.get("/v1/evals", tags=["evals"])
    def list_evals(org_id: str = Depends(tenant)):
        return {"rubrics": [r.model_dump(mode="json") for r in store.list_rubrics(org_id)]}

    @app.post("/v1/evals/rubrics", tags=["evals"])
    def create_rubric(body: RubricIn, org_id: str = Depends(tenant)):
        rubric = new_rubric(org_id, body.name, body.description, body.threshold)
        store.upsert_rubric(rubric)
        return rubric.model_dump(mode="json")

    @app.post("/v1/evals/run/{call_id}", tags=["evals"])
    def run_eval(call_id: str, org_id: str = Depends(tenant)):
        call = pipeline.reevaluate(org_id, call_id)
        if call is None:
            raise HTTPException(status_code=404, detail="call not found")
        return {"call_id": call.id, "evals": [e.model_dump() for e in call.evals]}

    return app


def _call_summary(call) -> dict:
    return {
        "id": call.id,
        "provider": call.provider.value,
        "provider_call_id": call.provider_call_id,
        "agent_id": call.agent_id,
        "status": call.status.value,
        "duration_ms": call.duration_ms,
        "hangup_reason": call.hangup.reason.value if call.hangup else None,
        "loss_score": call.hangup.loss_score if call.hangup else None,
        "hallucinations": len(call.hallucinations),
        "tool_count": len(call.tools),
        "finalized": call.finalized,
    }


app = create_app()
