from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from obsalt._version import __version__
from obsalt.assemble.timeline import timeline_view
from obsalt.config import Settings
from obsalt.domain.enums import AnalysisState, KeyScope, Role
from obsalt.domain.models import AnalysisExecution, CallRevision, Rubric
from obsalt.ingest.headers import RawHeaders
from obsalt.ingest.otlp import receive_otlp_batch
from obsalt.ingest.receive import ReceiveLimits, receive_webhook
from obsalt.ops.backfill import run_backfill
from obsalt.ops.privacy import apply_deletion
from obsalt.ops.retention import replay_horizon
from obsalt.otel.receiver import parse_otlp_request, request_to_spans, serialized_success
from obsalt.otel.span_identity import SpanIdentityIndex
from obsalt.plugin.host import plugin_by_name
from obsalt.query import (
    active_calls,
    call_list_item,
    hangup_rollup,
    in_range,
    latency_rollup,
    matches_call_filters,
    paginate_calls,
    quality_rollup,
    search_calls,
    tools_rollup,
)
from obsalt.runtime import AppState, bump_generation, create_connection, in_memory_state
from obsalt.security.sessions import check_csrf, read_session, sign_session, verify_session
from obsalt.util import new_id
from obsalt.worker.process import drain_inbox
from obsalt.worker.replay import replay_envelopes

TEMPLATES_DIR = Path(__file__).resolve().parent / "ui" / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
log = logging.getLogger("obsalt.api")


def create_app(settings: Settings | None = None, state: AppState | None = None) -> FastAPI:
    settings = settings or Settings()
    if state is None:
        state = in_memory_state(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        grpc_server = None
        if settings.otlp_grpc_enabled:
            try:
                from obsalt.otel.grpc_server import serve_otlp_grpc

                grpc_server = await serve_otlp_grpc(state, port=settings.otlp_grpc_port)
            except RuntimeError:
                log.warning("OTLP gRPC requested but obsalt[grpc] is not installed")
        yield
        if grpc_server is not None:
            await grpc_server.stop(grace=2)

    app = FastAPI(title="obsalt", version=__version__, lifespan=lifespan)
    app.state.obsalt = state

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/ready")
    def ready() -> dict[str, object]:
        return {
            "status": "ready",
            "plugins": [p.name for p in state.plugins],
            "insecure_defaults": settings.insecure_defaults(),
        }

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/v1/ingest/{provider}/{ingest_key}")
    async def ingest(
        provider: str,
        ingest_key: str,
        request: Request,
        background: BackgroundTasks,
    ) -> Response:
        raw = await request.body()
        encoding = request.headers.get("content-encoding")
        if encoding and encoding not in {"identity"}:
            from obsalt.otel.receiver import decompress_body

            raw = decompress_body(raw, encoding)
        header_pairs = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in request.headers.items()]
        try:
            loaded = plugin_by_name(provider, state.plugins)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="plugin not installed") from exc
        result = receive_webhook(
            provider=provider,
            ingest_key=ingest_key,
            raw=raw,
            headers=RawHeaders(header_pairs),
            resolver=state.resolver,
            plugin=loaded.plugin,
            objects=state.objects,
            inbox=state.inbox,
            limits=ReceiveLimits(
                compressed_bytes=settings.compressed_body_limit,
                expanded_bytes=settings.expanded_body_limit,
            ),
            compressed_size=int(request.headers.get("content-length") or len(raw)),
            leases=getattr(state, "leases", None),
        )
        if result.envelope is not None and result.rejected is None:
            # Ack first. Decode runs as a background worker (TestClient waits for it).
            background.add_task(drain_inbox, state)
        return Response(
            content=result.response.body,
            status_code=result.response.status_code,
            media_type=result.response.media_type,
            headers=result.response.headers,
        )

    @app.post("/v1/traces")
    async def traces(
        request: Request,
        background: BackgroundTasks,
        content_type: str = Header("application/json"),
        content_encoding: str | None = Header(None),
        x_api_key: str | None = Header(None, alias="X-API-Key"),
    ) -> Response:
        org = _require_key(state, x_api_key, KeyScope.INGEST)
        raw = await request.body()
        ct = content_type.split(";")[0].strip()
        try:
            req = await asyncio.to_thread(parse_otlp_request, ct, raw, content_encoding)
        except HTTPException as exc:
            if exc.status_code == 415:
                raise
            raise
        spans = request_to_spans(req)
        from obsalt.otel.tenancy import reject_tenant_assertions

        denied = reject_tenant_assertions(spans, org)
        if denied:
            raise HTTPException(status_code=401, detail=denied)
        try:
            result = receive_otlp_batch(
                org_id=org,
                raw=raw,
                content_type=ct,
                objects=state.objects,
                inbox=state.inbox,
                limits=ReceiveLimits(
                    compressed_bytes=state.settings.compressed_body_limit,
                    expanded_bytes=state.settings.expanded_body_limit,
                ),
                compressed_size=int(request.headers.get("content-length") or len(raw)),
                spans=spans,
                span_index=_span_index(state),
                leases=getattr(state, "leases", None),
            )
        except Exception as exc:
            raise HTTPException(status_code=503, detail="ingest capacity") from exc
        if result.status_code == 413:
            raise HTTPException(status_code=413, detail=result.rejected)
        if result.status_code == 409:
            raise HTTPException(status_code=409, detail=result.rejected)
        if result.envelope is not None and result.rejected is None:
            background.add_task(drain_inbox, state)
        return Response(content=serialized_success(), media_type="application/x-protobuf")

    @app.get("/v1/calls")
    def list_calls(
        x_api_key: str | None = Header(None, alias="X-API-Key"),
        start: datetime | None = None,
        end: datetime | None = None,
        agent_id: str | None = None,
        outcome: str | None = None,
        source: str | None = None,
        latency_ms: float | None = None,
        flag: str | None = None,
        eval_result: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict:
        org = _require_key(state, x_api_key, KeyScope.READ)
        if start is None or end is None:
            raise HTTPException(status_code=400, detail="start and end are required")
        limit = min(max(limit, 1), 100)
        offset_items = []
        for rev in active_calls(state, org):
            if not in_range(rev, start, end):
                continue
            analysis = getattr(state.sink, "analysis", {}).get((org, rev.call_id, rev.revision), [])
            if not matches_call_filters(
                rev,
                agent_id=agent_id,
                outcome=outcome,
                source=source,
                latency_ms=latency_ms,
                flag=flag,
                eval_result=eval_result,
                analysis=analysis,
            ):
                continue
            offset_items.append(call_list_item(rev))
        page, next_cursor = paginate_calls(offset_items, cursor=cursor, limit=limit)
        return {
            "items": page,
            "as_of_generation": state.rollup_generation,
            "next_cursor": next_cursor,
        }

    @app.get("/v1/calls/{call_id}")
    def get_call(call_id: str, x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        org = _require_key(state, x_api_key, KeyScope.READ)
        rev = _active(state, org, call_id)
        payload = rev.model_dump(mode="json")
        payload["analysis"] = [
            r.model_dump(mode="json") if hasattr(r, "model_dump") else r
            for r in getattr(state.sink, "analysis", {}).get((org, call_id, rev.revision), [])
        ]
        return payload

    @app.get("/v1/calls/{call_id}/timeline")
    def get_timeline(call_id: str, x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        org = _require_key(state, x_api_key, KeyScope.READ)
        return timeline_view(_active(state, org, call_id))

    @app.get("/v1/calls/{call_id}/evidence/{ref}")
    def get_evidence(call_id: str, ref: str, x_api_key: str | None = Header(None, alias="X-API-Key")) -> Response:
        org = _require_key(state, x_api_key, KeyScope.READ)
        rev = _active(state, org, call_id)
        body, content_type = _load_evidence(state, org, rev, ref)
        if body is None:
            raise HTTPException(status_code=404, detail="evidence not found")
        return Response(content=body, media_type=content_type)

    @app.post("/v1/search")
    async def search(request: Request, x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        org = _require_key(state, x_api_key, KeyScope.READ)
        body = await request.json()
        query = str(body.get("q") or "")
        filters = {k: body.get(k) for k in ("agent_id", "source", "hangup_reason") if body.get(k)}
        items = search_calls(
            active_calls(state, org),
            query,
            index=state.search,
            org_id=org,
            filters=filters or None,
        )
        return {"items": items}

    @app.get("/v1/latency")
    def latency(
        x_api_key: str | None = Header(None, alias="X-API-Key"),
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict:
        org = _require_key(state, x_api_key, KeyScope.READ)
        _require_range(start, end)
        calls = [c for c in active_calls(state, org) if start is None or in_range(c, start, end)]
        return latency_rollup(
            calls,
            as_of_generation=state.rollup_generation,
            store=getattr(state, "rollups", None),
            org_id=org,
        )

    @app.get("/v1/hangups")
    def hangups(
        x_api_key: str | None = Header(None, alias="X-API-Key"),
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict:
        org = _require_key(state, x_api_key, KeyScope.READ)
        _require_range(start, end)
        calls = [c for c in active_calls(state, org) if start is None or in_range(c, start, end)]
        return hangup_rollup(
            calls,
            as_of_generation=state.rollup_generation,
            store=getattr(state, "hangup_clusters", None),
            org_id=org,
        )

    @app.get("/v1/tools")
    def tools(
        x_api_key: str | None = Header(None, alias="X-API-Key"),
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict:
        org = _require_key(state, x_api_key, KeyScope.READ)
        _require_range(start, end)
        calls = [c for c in active_calls(state, org) if start is None or in_range(c, start, end)]
        return tools_rollup(calls, as_of_generation=state.rollup_generation)

    @app.get("/v1/quality")
    def quality(
        x_api_key: str | None = Header(None, alias="X-API-Key"),
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict:
        org = _require_key(state, x_api_key, KeyScope.READ)
        _require_range(start, end)
        calls = [c for c in active_calls(state, org) if start is None or in_range(c, start, end)]
        return quality_rollup(
            calls,
            getattr(state.sink, "analysis", {}),
            as_of_generation=state.rollup_generation,
        )

    @app.post("/v1/calls/{call_id}/analyze")
    async def analyze(call_id: str, x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        org = _require_key(state, x_api_key, KeyScope.ANALYZE)
        rev = _active(state, org, call_id)
        if state.spend_usd >= state.settings.llm_monthly_budget_usd > 0:
            execution = AnalysisExecution(
                call_id=call_id,
                revision=rev.revision,
                analyzer_id="eval",
                analyzer_version="1",
                state=AnalysisState.BUDGET_BLOCKED,
            )
            return execution.model_dump(mode="json")
        from obsalt.analysis.tier2 import run_tier2

        rubrics = [r for r in state.rubrics.values() if r.org_id == org]
        result = await run_tier2(
            rev,
            rubric=rubrics[0] if rubrics else None,
            manual=True,
            baseline_sample_rate=state.settings.baseline_sample_rate,
            budget_usd=state.settings.llm_monthly_budget_usd or float("inf"),
            spend_usd=state.spend_usd,
            judge=state.judge,
        )
        cost = float((result.payload or {}).get("cost_usd") or 0.0)
        if cost:
            state.spend_usd += cost
            from obsalt.metrics import tier2_spend_usd

            tier2_spend_usd.labels(org_id=org).set(state.spend_usd)
        writer = getattr(state.sink, "write_analysis", None)
        existing = getattr(state.sink, "analysis", {}).get((org, call_id, rev.revision), [])
        if writer is not None:
            writer(org, call_id, rev.revision, list(existing) + [result])
        return result.model_dump(mode="json")

    @app.get("/v1/rubrics")
    def list_rubrics(x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        org = _require_key(state, x_api_key, KeyScope.READ)
        return {
            "items": [r.model_dump(mode="json") for r in state.rubrics.values() if r.org_id == org]
        }

    @app.post("/v1/rubrics")
    async def create_rubric(request: Request, x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        org = _require_key(state, x_api_key, KeyScope.ADMIN)
        body = await request.json()
        rubric = Rubric(
            id=new_id(),
            org_id=org,
            name=str(body.get("name") or "rubric"),
            description=str(body.get("description") or ""),
            threshold=float(body.get("threshold") or 0.7),
        )
        state.rubrics[rubric.id] = rubric
        store = getattr(state, "rubric_store", None)
        if store is not None and hasattr(store, "insert"):
            store.insert(rubric)
        return rubric.model_dump(mode="json")

    @app.put("/v1/rubrics/{rubric_id}")
    async def update_rubric(rubric_id: str, request: Request, x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        org = _require_key(state, x_api_key, KeyScope.ADMIN)
        existing = state.rubrics.get(rubric_id)
        if existing is None or existing.org_id != org:
            raise HTTPException(status_code=404, detail="not found")
        body = await request.json()
        store = getattr(state, "rubric_store", None)
        if store is not None and hasattr(store, "new_version"):
            updated = store.new_version(
                existing,
                name=str(body["name"]) if body.get("name") else None,
                description=str(body["description"]) if body.get("description") is not None else None,
            )
        else:
            updated = existing.model_copy(
                update={
                    "name": str(body.get("name") or existing.name),
                    "description": str(body.get("description") if body.get("description") is not None else existing.description),
                    "version": existing.version + 1,
                    "threshold": float(body.get("threshold") or existing.threshold),
                }
            )
        if updated.id != rubric_id:
            state.rubrics.pop(rubric_id, None)
        state.rubrics[updated.id] = updated
        return updated.model_dump(mode="json")

    @app.delete("/v1/rubrics/{rubric_id}")
    def delete_rubric(rubric_id: str, x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        org = _require_key(state, x_api_key, KeyScope.ADMIN)
        existing = state.rubrics.get(rubric_id)
        if existing is None or existing.org_id != org:
            raise HTTPException(status_code=404, detail="not found")
        del state.rubrics[rubric_id]
        deleter = getattr(getattr(state, "rubric_store", None), "delete", None)
        if callable(deleter):
            deleter(org, rubric_id)
        return {"status": "deleted"}

    @app.get("/v1/connections")
    def list_connections(x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        org = _require_key(state, x_api_key, KeyScope.ADMIN)
        items = []
        connections = getattr(state.resolver, "connections", {})
        for cfg in connections.values():
            if cfg.org_id != org:
                continue
            items.append(
                {
                    "provider": cfg.provider,
                    "connection_id": cfg.connection_id,
                    "org_id": cfg.org_id,
                    "secret_fields": sorted(cfg.secrets),
                }
            )
        return {"items": items}

    @app.post("/v1/connections")
    async def post_connection(request: Request, x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        org = _require_key(state, x_api_key, KeyScope.ADMIN)
        body = await request.json()
        provider = str(body.get("provider") or "")
        try:
            plugin_by_name(provider, state.plugins)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="plugin not installed") from exc
        created = create_connection(
            state,
            org_id=org,
            provider=provider,
            secrets=dict(body.get("secrets") or {}),
            settings=dict(body.get("settings") or {}),
        )
        return created

    @app.delete("/v1/connections/{connection_id}")
    def delete_connection(connection_id: str, x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        org = _require_key(state, x_api_key, KeyScope.ADMIN)
        deleter = getattr(state.resolver, "delete", None)
        if not callable(deleter) or not deleter(org, connection_id):
            raise HTTPException(status_code=404, detail="not found")
        state.connections_plaintext.pop(connection_id, None)
        return {"status": "deleted"}

    @app.get("/v1/outbound-webhooks")
    def list_outbound(x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        org = _require_key(state, x_api_key, KeyScope.ADMIN)
        items = []
        for dest in state.webhook_destinations:
            if dest.get("org_id") != org:
                continue
            items.append({"id": dest.get("id"), "url": dest.get("url"), "event_type": dest.get("event_type")})
        return {"items": items}

    @app.post("/v1/outbound-webhooks")
    async def create_outbound(request: Request, x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        org = _require_key(state, x_api_key, KeyScope.ADMIN)
        body = await request.json()
        from obsalt.egress import EgressDenied, validate_destination
        from obsalt.webhooks.outbound import mint_whsec

        url = str(body.get("url") or "")
        try:
            validate_destination(url, allow_http_localhost=bool(body.get("allow_http_localhost")))
        except EgressDenied as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        secret = str(body.get("secret") or mint_whsec())
        dest = {
            "id": new_id(),
            "org_id": org,
            "url": url,
            "secret": secret,
            "event_type": str(body.get("event_type") or "call.finalized"),
            "allow_http_localhost": "true" if body.get("allow_http_localhost") else "false",
        }
        state.webhook_destinations.append(dest)
        return {"id": dest["id"], "url": url, "secret": secret}

    @app.post("/v1/replay")
    async def replay(request: Request, x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        org = _require_key(state, x_api_key, KeyScope.ADMIN)
        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        queued = replay_envelopes(
            state,
            org_id=org,
            provider=body.get("provider"),
            source_call_id=body.get("source_call_id") or body.get("call_id"),
        )
        bump_generation(state)
        return {"status": "queued", "replayed": queued, "as_of_generation": state.rollup_generation}

    @app.post("/v1/backfill")
    async def backfill(request: Request, x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        org = _require_key(state, x_api_key, KeyScope.ADMIN)
        body: dict = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        return run_backfill(
            state,
            org_id=org,
            provider=body.get("provider"),
            connection_id=body.get("connection_id"),
        )

    @app.post("/v1/privacy/deletion-requests")
    async def deletion(request: Request, x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        org = _require_key(state, x_api_key, KeyScope.ADMIN)
        body = await request.json()
        call_id = body.get("call_id")
        source_call_id = body.get("source_call_id") or call_id
        start = body.get("start")
        end = body.get("end")
        if not source_call_id and not body.get("caller") and not body.get("caller_token") and not (start and end):
            raise HTTPException(status_code=400, detail="call_id, caller, or start/end is required")
        return apply_deletion(
            state,
            org_id=org,
            call_id=str(call_id) if call_id else None,
            source_call_id=str(source_call_id) if source_call_id else None,
            caller=str(body["caller"]) if body.get("caller") else None,
            caller_token_value=str(body["caller_token"]) if body.get("caller_token") else None,
            start=datetime.fromisoformat(start.replace("Z", "+00:00")) if isinstance(start, str) else start,
            end=datetime.fromisoformat(end.replace("Z", "+00:00")) if isinstance(end, str) else end,
        )

    @app.get("/v1/retention")
    def retention(x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        _require_key(state, x_api_key, KeyScope.READ)
        return replay_horizon(raw_retention_days=state.settings.raw_retention_days)

    @app.post("/v1/export")
    async def export_calls(request: Request, x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        org = _require_key(state, x_api_key, KeyScope.ADMIN)
        body: dict = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        from pathlib import Path

        from obsalt.ops.parquet import export_revisions

        dest = Path(str(body.get("dest") or "/tmp/obsalt-export"))
        return export_revisions(
            active_calls(state, org),
            dest,
            as_of_generation=state.rollup_generation,
        )

    @app.post("/v1/rubrics/{rubric_id}/calibrate")
    async def calibrate(rubric_id: str, request: Request, x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        org = _require_key(state, x_api_key, KeyScope.ANALYZE)
        rubric = state.rubrics.get(rubric_id)
        if rubric is None or rubric.org_id != org:
            raise HTTPException(status_code=404, detail="not found")
        body = await request.json()
        labelled = []
        for item in body.get("labeled") or []:
            rev = _active(state, org, str(item.get("call_id")))
            labelled.append((rev, bool(item.get("expected_pass"))))
        from obsalt.analysis.calibration import calibrate_rubric

        return await calibrate_rubric(rubric, labelled, judge=state.judge)

    @app.post("/v1/quality/review")
    async def review(request: Request, x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        org = _require_key(state, x_api_key, KeyScope.ANALYZE)
        body = await request.json()
        item = {
            "org_id": org,
            "call_id": str(body.get("call_id") or ""),
            "agree": bool(body.get("agree")),
            "note": str(body.get("note") or ""),
        }
        state.reviews.append(item)
        return {"status": "recorded", "item": item}

    @app.get("/v1/plugins")
    def plugins(x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        _require_key(state, x_api_key, KeyScope.READ)
        return {
            "items": [
                {
                    "name": p.name,
                    "display_name": p.display_name,
                    "capabilities": sorted(c.value for c in p.capabilities),
                    "fidelity": p.fidelity.model_dump(mode="json"),
                    "trust": p.manifest.trust,
                }
                for p in state.plugins
            ]
        }

    @app.get("/v1/ui", response_class=HTMLResponse)
    def ui_home(request: Request) -> HTMLResponse:
        org = _ui_org(request, state)
        calls = active_calls(state, org) if org else []
        start = request.query_params.get("start") or ""
        end = request.query_params.get("end") or ""
        filters = {
            "agent_id": request.query_params.get("agent_id") or "",
            "hangup_reason": request.query_params.get("hangup_reason") or "",
            "source": request.query_params.get("source") or "",
            "flag": request.query_params.get("flag") or "",
            "outcome": request.query_params.get("outcome") or "",
            "start": start,
            "end": end,
        }
        if start and end:
            from datetime import datetime as dt

            try:
                start_dt = dt.fromisoformat(start.replace("Z", "+00:00"))
                end_dt = dt.fromisoformat(end.replace("Z", "+00:00"))
                calls = [c for c in calls if in_range(c, start_dt, end_dt)]
            except ValueError:
                pass
        calls = [
            c
            for c in calls
            if matches_call_filters(
                c,
                agent_id=filters["agent_id"] or None,
                outcome=filters["outcome"] or filters["hangup_reason"] or None,
                source=filters["source"] or None,
                flag=filters["flag"] or None,
                analysis=getattr(state.sink, "analysis", {}).get((org, c.call_id, c.revision), []) if org else [],
            )
        ]
        return _render(request, "call_list.html", {"calls": calls, "org": org, "filters": filters})

    @app.get("/v1/ui/login", response_class=HTMLResponse)
    def ui_login(request: Request) -> HTMLResponse:
        return _render(request, "login.html", {"org": None})

    @app.post("/v1/ui/login")
    async def ui_login_post(request: Request) -> Response:
        form = await request.form()
        key = str(form.get("api_key") or "")
        try:
            org, scopes = _lookup_key(state, key)
            if KeyScope.READ not in scopes and KeyScope.ADMIN not in scopes:
                raise HTTPException(status_code=403, detail="insufficient scope")
        except HTTPException:
            return _render(request, "login.html", {"org": None, "error": "invalid API key"})
        response = RedirectResponse("/v1/ui", status_code=303)
        cookie = sign_session(org, state.settings.session_secret, role=_role_for_scopes(scopes))
        info = read_session(cookie, state.settings.session_secret)
        response.set_cookie(
            "obsalt_session",
            cookie,
            httponly=True,
            samesite="lax",
            secure=not state.settings.insecure_defaults(),
        )
        if info and info.csrf:
            response.set_cookie(
                "obsalt_csrf",
                info.csrf,
                httponly=False,
                samesite="lax",
                secure=not state.settings.insecure_defaults(),
            )
        return response

    @app.get("/v1/ui/calls/{call_id}", response_class=HTMLResponse)
    def ui_call(request: Request, call_id: str) -> HTMLResponse:
        org = _ui_org(request, state)
        if not org:
            raise HTTPException(status_code=401, detail="session required")
        rev = _active(state, org, call_id)
        analysis = getattr(state.sink, "analysis", {}).get((org, call_id, rev.revision), [])
        flags: list[dict] = []
        evals: list[dict] = []
        for row in analysis:
            payload = row.payload if hasattr(row, "payload") else {}
            analyzer = row.execution.analyzer_id if hasattr(row, "execution") else ""
            if analyzer == "flags":
                flags.extend(payload.get("flags") or [])
            if analyzer in {"eval", "tier2", "hallucination"}:
                evals.append(
                    {
                        "analyzer_id": analyzer,
                        "state": row.execution.state.value,
                        "passed": payload.get("passed"),
                        "score": payload.get("score"),
                        "rationale": payload.get("rationale"),
                    }
                )
        return _render(
            request,
            "call_detail.html",
            {
                "call": rev,
                "timeline": timeline_view(rev),
                "coverage": rev.coverage,
                "org": org,
                "flags": flags,
                "evals": evals,
            },
        )

    @app.get("/v1/ui/latency", response_class=HTMLResponse)
    def ui_latency(request: Request) -> HTMLResponse:
        org = _ui_org(request, state)
        data = latency_rollup(
            active_calls(state, org) if org else [],
            as_of_generation=state.rollup_generation,
            store=getattr(state, "rollups", None),
            org_id=org,
        )
        return _render(request, "latency.html", {"rollup": data, "org": org})

    @app.get("/v1/ui/hangups", response_class=HTMLResponse)
    def ui_hangups(request: Request) -> HTMLResponse:
        org = _ui_org(request, state)
        data = hangup_rollup(
            active_calls(state, org) if org else [],
            as_of_generation=state.rollup_generation,
            store=getattr(state, "hangup_clusters", None),
            org_id=org,
        )
        return _render(
            request,
            "hangups.html",
            {"clusters": data.get("clusters") or [], "as_of_generation": data.get("as_of_generation"), "org": org},
        )

    @app.get("/v1/ui/quality", response_class=HTMLResponse)
    def ui_quality(request: Request) -> HTMLResponse:
        org = _ui_org(request, state)
        data = quality_rollup(
            active_calls(state, org) if org else [],
            getattr(state.sink, "analysis", {}),
            as_of_generation=state.rollup_generation,
        )
        return _render(
            request,
            "quality.html",
            {
                "quality": data,
                "org": org,
                "spend_usd": state.spend_usd,
                "budget_usd": state.settings.llm_monthly_budget_usd,
            },
        )

    @app.get("/v1/ui/search", response_class=HTMLResponse)
    def ui_search(request: Request, q: str = "") -> HTMLResponse:
        org = _ui_org(request, state)
        filters = {
            "agent_id": request.query_params.get("agent_id") or "",
            "source": request.query_params.get("source") or "",
        }
        hits = []
        if q and org:
            hits = search_calls(
                active_calls(state, org),
                q,
                index=state.search,
                org_id=org,
                filters={k: v for k, v in filters.items() if v} or None,
            )
        return _render(request, "search.html", {"q": q, "results": hits, "org": org, "filters": filters})

    @app.get("/v1/ui/settings", response_class=HTMLResponse)
    def ui_settings(request: Request) -> HTMLResponse:
        org = _ui_org(request, state)
        connections = []
        for cfg in getattr(state.resolver, "connections", {}).values():
            if org and cfg.org_id != org:
                continue
            connections.append({"provider": cfg.provider, "connection_id": cfg.connection_id, "org_id": cfg.org_id})
        return _render(
            request,
            "settings.html",
            {
                "org": org,
                "plugins": state.plugins,
                "connections": connections,
                "rubrics": [r for r in state.rubrics.values() if not org or r.org_id == org],
                "retention": {
                    "transcripts_days": state.settings.transcript_retention_days,
                    "raw_days": state.settings.raw_retention_days,
                    "aggregates_days": state.settings.aggregate_retention_days,
                },
                "csrf": _ui_csrf(request, state),
                "baseline_sample_rate": state.settings.baseline_sample_rate,
                "budget_usd": state.settings.llm_monthly_budget_usd,
            },
        )

    @app.post("/v1/ui/replay")
    async def ui_replay(request: Request) -> Response:
        org = _ui_org(request, state)
        if not org:
            raise HTTPException(status_code=401, detail="session required")
        form = await request.form()
        _require_csrf(request, state, str(form.get("csrf") or request.headers.get("x-csrf-token") or ""))
        replay_envelopes(state, org_id=org)
        bump_generation(state)
        return RedirectResponse("/v1/ui", status_code=303)

    @app.post("/v1/ui/calls/{call_id}/analyze")
    async def ui_analyze(request: Request, call_id: str) -> Response:
        org = _ui_org(request, state)
        if not org:
            raise HTTPException(status_code=401, detail="session required")
        form = await request.form()
        _require_csrf(request, state, str(form.get("csrf") or request.headers.get("x-csrf-token") or ""))
        rev = _active(state, org, call_id)
        if state.spend_usd >= state.settings.llm_monthly_budget_usd > 0:
            return RedirectResponse(f"/v1/ui/calls/{call_id}", status_code=303)
        from obsalt.analysis.tier2 import run_tier2

        rubrics = [r for r in state.rubrics.values() if r.org_id == org]
        result = await run_tier2(
            rev,
            rubric=rubrics[0] if rubrics else None,
            manual=True,
            baseline_sample_rate=state.settings.baseline_sample_rate,
            budget_usd=state.settings.llm_monthly_budget_usd or float("inf"),
            spend_usd=state.spend_usd,
        )
        writer = getattr(state.sink, "write_analysis", None)
        existing = getattr(state.sink, "analysis", {}).get((org, call_id, rev.revision), [])
        if writer is not None:
            writer(org, call_id, rev.revision, list(existing) + [result])
        return RedirectResponse(f"/v1/ui/calls/{call_id}", status_code=303)

    return app


def _span_index(state: AppState) -> SpanIdentityIndex:
    existing = getattr(state, "span_identities", None)
    if existing is None:
        existing = SpanIdentityIndex()
        state.span_identities = existing
    return existing


def _require_range(start: datetime | None, end: datetime | None) -> None:
    if start is None or end is None:
        raise HTTPException(status_code=400, detail="start and end are required")


def _lookup_key(state: AppState, key: str | None) -> tuple[str, frozenset[KeyScope]]:
    if not key:
        raise HTTPException(status_code=401, detail="API key required")
    found = state.keys.get(key)
    if found is None and state.key_directory is not None:
        record = state.key_directory.lookup(key)
        if record is not None:
            found = (record.org_id, record.scopes)
    if found is None:
        raise HTTPException(status_code=401, detail="invalid API key")
    return found


def _require_key(state: AppState, key: str | None, scope: KeyScope) -> str:
    org, scopes = _lookup_key(state, key)
    if scope not in scopes and KeyScope.ADMIN not in scopes:
        raise HTTPException(status_code=403, detail="insufficient scope")
    return org


def _role_for_scopes(scopes: frozenset[KeyScope]) -> Role:
    if scopes >= frozenset(KeyScope):
        return Role.OWNER
    if KeyScope.ADMIN in scopes:
        return Role.ADMIN
    if KeyScope.ANALYZE in scopes:
        return Role.ANALYST
    return Role.REVIEWER


def _load_evidence(state: AppState, org: str, rev: object, ref: str) -> tuple[bytes | None, str]:
    objects = getattr(state, "objects", None)
    candidates: list[str] = []
    for item in getattr(rev, "evidence", []) or []:
        uri = getattr(item, "uri", "")
        if uri == ref or uri.endswith(ref):
            candidates.append(uri)
    for turn in getattr(rev, "turns", []) or []:
        text_ref = getattr(turn, "text_ref", None)
        if text_ref and (text_ref == ref or text_ref.endswith(ref)):
            candidates.append(f"org/{org}/evidence/turn/{text_ref}")
            text = getattr(turn, "text", "") or ""
            if text:
                return text.encode("utf-8"), "text/plain"
    for prefix in ("turn", "grounding", "tool-args", "tool-result"):
        candidates.append(f"org/{org}/evidence/{prefix}/{ref}")
    if objects is not None:
        for key in candidates:
            try:
                body = objects.get(key)
            except Exception:
                continue
            if body is not None:
                return body, "application/octet-stream"
    return None, "application/octet-stream"


def _ui_org(request: Request, state: AppState) -> str | None:
    cookie = request.cookies.get("obsalt_session")
    org = verify_session(cookie, state.settings.session_secret)
    if org:
        return org
    api_key = request.headers.get("x-api-key")
    if api_key:
        try:
            return _require_key(state, api_key, KeyScope.READ)
        except HTTPException:
            return None
    return None


def _ui_csrf(request: Request, state: AppState) -> str | None:
    info = read_session(request.cookies.get("obsalt_session"), state.settings.session_secret)
    return info.csrf if info else None


def _require_csrf(request: Request, state: AppState, provided: str | None) -> None:
    if not check_csrf(request.cookies.get("obsalt_session"), provided, state.settings.session_secret):
        raise HTTPException(status_code=403, detail="csrf required")


def _active(state: AppState, org: str, call_id: str) -> CallRevision:
    rev_id = state.pointers.get(org, call_id)
    if not rev_id:
        raise HTTPException(status_code=404, detail="not found")
    rev = state.sink.get(org, call_id, rev_id)
    if rev is None or rev.org_id != org:
        raise HTTPException(status_code=404, detail="not found")
    return rev


def _render(request: Request, name: str, context: dict) -> HTMLResponse:
    path = TEMPLATES_DIR / name
    template = name if path.exists() else "call_list.html"
    state: AppState = request.app.state.obsalt
    context.setdefault("csrf", _ui_csrf(request, state))
    context.setdefault("raw_retention_days", state.settings.raw_retention_days)
    return templates.TemplateResponse(request, template, context)


def issue_session_cookie(org_id: str, secret: str) -> str:
    return sign_session(org_id, secret)
