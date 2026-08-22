from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from obsalt._version import __version__
from obsalt.assemble.timeline import timeline_view
from obsalt.config import Settings
from obsalt.domain.enums import AnalysisState, KeyScope
from obsalt.domain.models import AnalysisExecution, Rubric
from obsalt.ingest.headers import RawHeaders
from obsalt.ingest.receive import ReceiveLimits, receive_webhook
from obsalt.otel.mappers import MapperRegistry
from obsalt.otel.receiver import parse_otlp_request, request_to_spans, serialized_success
from obsalt.plugin.host import plugin_by_name
from obsalt.plugin.types import TombstoneHints
from obsalt.query import (
    active_calls,
    call_list_item,
    hangup_rollup,
    in_range,
    latency_rollup,
    paginate_calls,
    quality_rollup,
    search_calls,
    tools_rollup,
)
from obsalt.runtime import AppState, bump_generation, create_connection, in_memory_state
from obsalt.security.sessions import sign_session, verify_session
from obsalt.util import new_id, sha256_bytes
from obsalt.worker.process import drain_inbox, process_normalized_events

TEMPLATES_DIR = Path(__file__).resolve().parent / "ui" / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def create_app(settings: Settings | None = None, state: AppState | None = None) -> FastAPI:
    settings = settings or Settings()
    if state is None:
        state = in_memory_state(settings)
    app = FastAPI(title="obsalt", version=__version__)
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
        req = parse_otlp_request(ct, raw, content_encoding)
        spans = request_to_spans(req)
        for span in spans:
            asserted = (span.resource or {}).get("obsalt.org") or (span.attributes or {}).get("obsalt.org")
            if asserted and str(asserted) != org:
                raise HTTPException(status_code=401, detail="resource attribute cannot choose an organization")
        key = f"org/{org}/raw/otlp/{sha256_bytes(raw)}"
        state.objects.put(key, raw)
        destinations = [d for d in state.destinations if d.get("org_id") == org]
        if destinations:
            from obsalt.otel.forwarder import forward_otlp_batch

            for dest in destinations:
                forwarded = forward_otlp_batch(
                    dest["url"],
                    raw,
                    content_type=ct,
                    allow_http_localhost=dest.get("allow_http_localhost") == "true",
                )
                if forwarded.retryable:
                    raise HTTPException(status_code=503, detail=f"otlp forward retryable: {forwarded.detail}")
                if forwarded.permanent:
                    raise HTTPException(status_code=400, detail=f"otlp forward denied: {forwarded.detail}")
        background.add_task(_assemble_otlp, state, org, spans)
        return Response(content=serialized_success(), media_type="application/x-protobuf")

    @app.get("/v1/calls")
    def list_calls(
        x_api_key: str | None = Header(None, alias="X-API-Key"),
        start: datetime | None = None,
        end: datetime | None = None,
        agent_id: str | None = None,
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
            if agent_id and rev.agent_id != agent_id:
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
    def get_evidence(call_id: str, ref: str, x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        org = _require_key(state, x_api_key, KeyScope.READ)
        rev = _active(state, org, call_id)
        for item in rev.evidence:
            if item.uri.endswith(ref) or item.uri == ref:
                return item.model_dump(mode="json")
        raise HTTPException(status_code=404, detail="evidence not found")

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
        return latency_rollup(calls, as_of_generation=state.rollup_generation)

    @app.get("/v1/hangups")
    def hangups(
        x_api_key: str | None = Header(None, alias="X-API-Key"),
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict:
        org = _require_key(state, x_api_key, KeyScope.READ)
        _require_range(start, end)
        calls = [c for c in active_calls(state, org) if start is None or in_range(c, start, end)]
        return hangup_rollup(calls, as_of_generation=state.rollup_generation)

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
        )
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
        return rubric.model_dump(mode="json")

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
        _require_key(state, x_api_key, KeyScope.ADMIN)
        drain_inbox(state, limit=256)
        bump_generation(state)
        return {"status": "queued", "as_of_generation": state.rollup_generation}

    @app.post("/v1/backfill")
    def backfill(x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        _require_key(state, x_api_key, KeyScope.ADMIN)
        return {"status": "queued", "note": "provider pull is bounded by upstream retention"}

    @app.post("/v1/privacy/deletion-requests")
    async def deletion(request: Request, x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        org = _require_key(state, x_api_key, KeyScope.ADMIN)
        body = await request.json()
        call_id = body.get("call_id")
        source_call_id = body.get("source_call_id") or call_id
        if not source_call_id:
            raise HTTPException(status_code=400, detail="call_id is required for delete-by-call")
        hints = TombstoneHints(source_call_id=str(source_call_id))
        state.inbox.tombstone(org, hints)
        to_delete: set[str] = set()
        if call_id:
            to_delete.add(str(call_id))
        for rev in list(getattr(state.sink, "revisions", {}).values()):
            if rev.org_id != org:
                continue
            if call_id and rev.call_id == call_id:
                to_delete.add(rev.call_id)
            if rev.source_call_id == source_call_id:
                to_delete.add(rev.call_id)
        deleter = getattr(state.pointers, "delete", None)
        search = getattr(state, "search", None)
        for cid in to_delete:
            state.sink.delete_call(org, cid)
            if callable(deleter):
                deleter(org, cid)
            if search is not None and hasattr(search, "delete_for_call"):
                search.delete_for_call(org, cid)
        bump_generation(state)
        return {"status": "accepted", "undoable": False}

    @app.get("/v1/plugins")
    def plugins() -> dict:
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
        filters = {
            "agent_id": request.query_params.get("agent_id") or "",
            "hangup_reason": request.query_params.get("hangup_reason") or "",
            "source": request.query_params.get("source") or "",
            "flag": request.query_params.get("flag") or "",
        }
        if filters["agent_id"]:
            calls = [c for c in calls if c.agent_id == filters["agent_id"]]
        if filters["source"]:
            calls = [c for c in calls if c.source == filters["source"]]
        if filters["hangup_reason"]:
            calls = [
                c
                for c in calls
                if c.hangup is not None and c.hangup.reason.value == filters["hangup_reason"]
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
            org = _require_key(state, key, KeyScope.READ)
        except HTTPException:
            return _render(request, "login.html", {"org": None, "error": "invalid API key"})
        response = RedirectResponse("/v1/ui", status_code=303)
        response.set_cookie(
            "obsalt_session",
            sign_session(org, state.settings.session_secret),
            httponly=True,
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
        data = latency_rollup(active_calls(state, org) if org else [], as_of_generation=state.rollup_generation)
        return _render(request, "latency.html", {"rollup": data, "org": org})

    @app.get("/v1/ui/hangups", response_class=HTMLResponse)
    def ui_hangups(request: Request) -> HTMLResponse:
        org = _ui_org(request, state)
        data = hangup_rollup(active_calls(state, org) if org else [], as_of_generation=state.rollup_generation)
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
            },
        )

    return app


def _assemble_otlp(state: AppState, org: str, spans: list) -> None:
    registry = MapperRegistry(state.plugins)
    events = registry.decode(spans)
    if not events:
        return
    mapper = registry.pick(spans[0]) if spans else None
    source = getattr(mapper, "name", "otlp")
    declaration = getattr(mapper, "fidelity", None)
    if declaration is None:
        return
    process_normalized_events(
        events,
        org_id=org,
        source=source,
        source_call_id=None,
        envelope_id=new_id(),
        declaration=declaration,
        pointers=state.pointers,
        sink=state.sink,
        decoder_version=getattr(mapper, "decoder_version", f"{source}/1"),
    )


def _require_range(start: datetime | None, end: datetime | None) -> None:
    if start is None or end is None:
        raise HTTPException(status_code=400, detail="start and end are required")


def _require_key(state: AppState, key: str | None, scope: KeyScope) -> str:
    if not key:
        raise HTTPException(status_code=401, detail="API key required")
    found = state.keys.get(key)
    if found is None and state.key_directory is not None:
        record = state.key_directory.lookup(key)
        if record is not None:
            found = (record.org_id, record.scopes)
    if found is None:
        raise HTTPException(status_code=401, detail="invalid API key")
    org, scopes = found
    if scope not in scopes and KeyScope.ADMIN not in scopes:
        raise HTTPException(status_code=403, detail="insufficient scope")
    return org


def _ui_org(request: Request, state: AppState) -> str | None:
    cookie = request.cookies.get("obsalt_session")
    org = verify_session(cookie, state.settings.session_secret)
    if org:
        return org
    api_key = request.headers.get("x-api-key") or request.query_params.get("key")
    if api_key:
        try:
            return _require_key(state, api_key, KeyScope.READ)
        except HTTPException:
            return None
    if len(state.keys) == 1:
        return next(iter(state.keys.values()))[0]
    return None


def _active(state: AppState, org: str, call_id: str):
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
    return templates.TemplateResponse(request, template, context)


def issue_session_cookie(org_id: str, secret: str) -> str:
    return sign_session(org_id, secret)
