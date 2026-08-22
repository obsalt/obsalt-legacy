from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Form, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from obsalt._version import __version__
from obsalt.analysis.hallucination import candidate_claims, detect
from obsalt.analysis.judge import HeuristicJudge
from obsalt.analysis.tier2 import judge_rubric, should_run_tier2
from obsalt.api.auth import require_scope
from obsalt.api.sessions import SESSION_COOKIE, dump_session, load_session
from obsalt.assemble.timeline import timeline_view
from obsalt.config import Settings
from obsalt.ingest.otlp import handle_otlp_http
from obsalt.plugin.protocol import ConnectionConfig
from obsalt.runtime import ApiPrincipal, Runtime
from obsalt.search.hybrid import hybrid_search

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "ui" / "templates"))


def create_app(runtime: Runtime | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    runtime = runtime or Runtime.create(settings)
    app = FastAPI(title="obsalt", version=__version__)
    app.state.runtime = runtime
    app.state.settings = settings

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/ready")
    def ready() -> dict[str, Any]:
        return {
            "status": "ok",
            "plugins": sorted(runtime.host.plugins),
            "plugin_errors": runtime.host.errors,
            "durable": runtime.durable is not None,
        }

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/v1/ingest/{provider}/{ingest_key}")
    async def ingest(
        provider: str,
        ingest_key: str,
        request: Request,
        background_tasks: BackgroundTasks,
    ) -> Response:
        raw = await request.body()
        headers = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in request.headers.items()]
        result = runtime.receive.handle(
            provider=provider,
            ingest_key=ingest_key,
            raw=raw,
            headers=headers,
        )
        if result.envelope_id and result.state and result.state.value == "queued":
            background_tasks.add_task(runtime.process_envelope, result.envelope_id, raw)
        return Response(
            content=result.response.body,
            status_code=result.response.status_code,
            media_type=result.response.media_type,
            headers=result.response.headers,
        )

    @app.post("/v1/traces")
    async def traces(request: Request) -> Response:
        return await handle_otlp_http(request, runtime)

    @app.get("/v1/calls")
    def list_calls(
        principal: ApiPrincipal = Depends(require_scope("read")),
        agent_id: str | None = None,
        from_ts: str | None = Query(default=None, alias="from"),
        to_ts: str | None = Query(default=None, alias="to"),
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        _require_range(from_ts, to_ts)
        calls = runtime.list_calls(principal.org_id)
        if agent_id:
            calls = [c for c in calls if c.identity.agent_id == agent_id]
        calls = sorted(calls, key=lambda c: c.identity.call_id)
        start = int(cursor or 0)
        page = calls[start : start + limit]
        return {
            "items": [_call_list_item(c) for c in page],
            "next_cursor": str(start + limit) if start + limit < len(calls) else None,
        }

    @app.get("/v1/calls/{call_id}")
    def get_call(call_id: str, principal: ApiPrincipal = Depends(require_scope("read"))) -> dict[str, Any]:
        revision = _call_or_404(runtime, principal.org_id, call_id)
        return revision.model_dump(mode="json")

    @app.get("/v1/calls/{call_id}/timeline")
    def get_timeline(call_id: str, principal: ApiPrincipal = Depends(require_scope("read"))) -> dict[str, Any]:
        revision = _call_or_404(runtime, principal.org_id, call_id)
        return timeline_view(revision)

    @app.get("/v1/calls/{call_id}/evidence/{ref}")
    def get_evidence(
        call_id: str, ref: str, principal: ApiPrincipal = Depends(require_scope("read"))
    ) -> dict[str, Any]:
        revision = _call_or_404(runtime, principal.org_id, call_id)
        blob = runtime.get_blob(principal.org_id, ref)
        if blob is not None:
            return {"ref": ref, "call_id": revision.call_id, "text": blob}
        for item in revision.evidence:
            if item.content_ref == ref or item.uri == ref:
                return item.model_dump(mode="json")
        raise HTTPException(status_code=404, detail="not found")

    @app.post("/v1/search")
    def search(body: dict[str, Any], principal: ApiPrincipal = Depends(require_scope("read"))) -> dict[str, Any]:
        query = str(body.get("q") or "")
        docs = [
            (rev.call_id, rev.revision, " ".join(t.text or "" for t in rev.turns))
            for rev in runtime.list_calls(principal.org_id)
        ]
        fused = hybrid_search(query, docs)
        return {"hits": [h.__dict__ for h in fused[:20]]}

    @app.get("/v1/latency")
    def latency(principal: ApiPrincipal = Depends(require_scope("read"))) -> dict[str, Any]:
        samples: dict[str, list[float]] = {}
        aggregates: list[dict[str, Any]] = []
        for rev in runtime.list_calls(principal.org_id):
            for m in rev.stage_measurements:
                samples.setdefault(m.stage.value, []).append(m.value_ms)
            for a in rev.aggregate_measurements:
                aggregates.append(
                    {
                        "call_id": rev.call_id,
                        "stage": a.stage.value,
                        "statistic": a.statistic.value,
                        "value_ms": a.value_ms,
                        "source_path": a.source_path,
                    }
                )
        return {
            "as_of_generation": runtime.rollup_generation,
            "sample_percentiles": {k: _pct(v) for k, v in samples.items()},
            "provider_aggregates": aggregates,
            "note": "provider aggregates are never mixed into sample percentiles",
        }

    @app.get("/v1/hangups")
    def hangups(principal: ApiPrincipal = Depends(require_scope("read"))) -> dict[str, Any]:
        clusters: dict[str, list[str]] = {}
        for rev in runtime.list_calls(principal.org_id):
            if not rev.hangup:
                continue
            key = f"{rev.hangup.reason.value}:{rev.hangup.party.value}"
            clusters.setdefault(key, []).append(rev.call_id)
        return {"as_of_generation": runtime.rollup_generation, "clusters": clusters}

    @app.get("/v1/tools")
    def tools(principal: ApiPrincipal = Depends(require_scope("read"))) -> dict[str, Any]:
        rows = []
        for rev in runtime.list_calls(principal.org_id):
            for tool in rev.tools:
                rows.append(
                    {
                        "call_id": rev.call_id,
                        "name": tool.name,
                        "status": tool.status.value,
                        "duration_ms": tool.duration_ms,
                        "duration_label": (
                            None if tool.duration_ms is not None else "not reported"
                        ),
                    }
                )
        return {"as_of_generation": runtime.rollup_generation, "items": rows}

    @app.get("/v1/quality")
    def quality(principal: ApiPrincipal = Depends(require_scope("read"))) -> dict[str, Any]:
        items = []
        for (org, call_id, revision), results in runtime.analysis.items():
            if org != principal.org_id:
                continue
            items.append(
                {
                    "call_id": call_id,
                    "revision": revision,
                    "results": [r.model_dump(mode="json") for r in results],
                    "executions": [
                        ex.model_dump(mode="json")
                        for key, ex in runtime.executions.items()
                        if key[0] == org and key[1] == call_id and key[2] == revision
                    ],
                }
            )
        return {"items": items, "note": "missing output is never a passing call"}

    @app.post("/v1/calls/{call_id}/analyze")
    def request_analyze(
        call_id: str, principal: ApiPrincipal = Depends(require_scope("analyze"))
    ) -> dict[str, Any]:
        revision = _call_or_404(runtime, principal.org_id, call_id)
        state = should_run_tier2(
            manual=True,
            budget_remaining=runtime.budget_remaining(principal.org_id),
        )
        if state.value == "budget_blocked":
            return {"call_id": revision.call_id, "revision": revision.revision, "state": state.value}
        transcript = " ".join(t.text or "" for t in revision.turns)
        grounding = [
            runtime.get_blob(principal.org_id, item.content_ref) or ""
            for item in revision.grounding
        ]
        grounding = [g for g in grounding if g]
        judge = HeuristicJudge()
        execution, analysis = asyncio.run(
            judge_rubric(
                revision,
                rubric_id="manual",
                rubric_version="1",
                rubric_body="Score the call. Fail if the agent invents facts.",
                judge=judge,
                judge_version=judge.version,
                prompt_version="manual/1",
                transcript=transcript,
                grounding=grounding,
            )
        )
        hall_exec, hall_results = asyncio.run(
            detect(revision, judge=judge, transcript=transcript, grounding=grounding)
        )
        key = (revision.org_id, revision.call_id, revision.revision)
        runtime.analysis.setdefault(key, []).extend([analysis, *hall_results])
        runtime.executions[(revision.org_id, revision.call_id, revision.revision, "eval.manual")] = execution
        runtime.executions[(revision.org_id, revision.call_id, revision.revision, "hallucination")] = hall_exec
        return {
            "call_id": revision.call_id,
            "revision": revision.revision,
            "state": execution.state.value,
            "candidates": candidate_claims(revision.turns),
        }

    @app.get("/v1/rubrics")
    def list_rubrics(principal: ApiPrincipal = Depends(require_scope("read"))) -> dict[str, Any]:
        items = [r for r in runtime.rubrics.values() if r.get("org_id") == principal.org_id]
        return {"items": items}

    @app.post("/v1/rubrics")
    def create_rubric(body: dict[str, Any], principal: ApiPrincipal = Depends(require_scope("admin"))) -> dict[str, Any]:
        rubric_id = str(body.get("id") or body.get("name"))
        version = int(body.get("version") or 1)
        row = {
            "id": rubric_id,
            "org_id": principal.org_id,
            "name": body.get("name"),
            "version": version,
            "body": body.get("body"),
            "threshold": body.get("threshold", 0.7),
        }
        runtime.rubrics[f"{principal.org_id}:{rubric_id}:{version}"] = row
        return row

    @app.get("/v1/connections")
    def list_connections(principal: ApiPrincipal = Depends(require_scope("admin"))) -> dict[str, Any]:
        items = []
        for cfg in runtime.connections.values():
            if cfg.org_id != principal.org_id:
                continue
            items.append(
                {
                    "id": cfg.connection_id,
                    "provider": cfg.provider,
                    "settings": cfg.settings,
                    "secret_fields_configured": sorted(cfg.credentials),
                }
            )
        return {"items": items}

    @app.post("/v1/connections")
    def create_connection(
        body: dict[str, Any], principal: ApiPrincipal = Depends(require_scope("admin"))
    ) -> dict[str, Any]:
        from obsalt.crypto.keys import new_ingest_key

        ingest_key = str(body.get("ingest_key") or new_ingest_key())
        cfg = ConnectionConfig(
            org_id=principal.org_id,
            provider=str(body["provider"]),
            connection_id=str(body.get("id") or body["provider"]),
            credentials=dict(body.get("credentials") or {}),
            settings=dict(body.get("settings") or {}),
        )
        runtime.put_connection(cfg, ingest_key)
        return {"id": cfg.connection_id, "provider": cfg.provider, "ingest_key": ingest_key}

    @app.post("/v1/replay")
    def replay(body: dict[str, Any], principal: ApiPrincipal = Depends(require_scope("admin"))) -> dict[str, Any]:
        count = runtime.replay_org(principal.org_id, provider=body.get("provider"))
        return {"accepted": True, "replayed": count, "filter": body}

    @app.post("/v1/backfill")
    def backfill(body: dict[str, Any], principal: ApiPrincipal = Depends(require_scope("admin"))) -> dict[str, Any]:
        return {"accepted": True, "connection_id": body.get("connection_id")}

    @app.post("/v1/privacy/deletion-requests")
    def deletion(body: dict[str, Any], principal: ApiPrincipal = Depends(require_scope("admin"))) -> dict[str, Any]:
        runtime.tombstones.append({"org_id": principal.org_id, **body})
        kind = body.get("kind")
        if kind == "call" and body.get("call_id"):
            runtime.calls.pop((principal.org_id, body["call_id"]), None)
            runtime.active.pop((principal.org_id, body["call_id"]), None)
            runtime.inbox.add_tombstone(org_id=principal.org_id, source_call_id=body.get("source_call_id"))
        return {"status": "accepted", "undoable": False}

    @app.get("/v1/plugins")
    def plugins(_principal: ApiPrincipal = Depends(require_scope("read"))) -> dict[str, Any]:
        return {"items": runtime.host.inventory(), "trusted_operator_installed": True}

    @app.get("/v1/ui", response_class=HTMLResponse)
    def ui_home(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "home.html",
            {"version": __version__, "demo": runtime.settings.demo},
        )

    @app.get("/v1/ui/login", response_class=HTMLResponse)
    def ui_login(request: Request, error: str | None = None) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, "login.html", {"error": error})

    @app.post("/v1/ui/login")
    def ui_login_post(api_key: str = Form(...)) -> Response:
        principal = runtime.authenticate_api_key(api_key)
        if principal is None:
            return RedirectResponse("/v1/ui/login?error=invalid", status_code=303)
        token = dump_session(
            runtime.settings.session_secret,
            {"org_id": principal.org_id, "scope": principal.scope, "role": principal.role},
        )
        response = RedirectResponse("/v1/ui/calls", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            samesite="lax",
            secure=False,
            max_age=14 * 24 * 3600,
        )
        return response

    @app.get("/v1/ui/calls", response_class=HTMLResponse)
    def ui_calls(request: Request, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> HTMLResponse:
        org, _principal = _ui_org(runtime, request, x_api_key)
        if org is None:
            return RedirectResponse("/v1/ui/login", status_code=303)
        calls = runtime.list_calls(org)
        return TEMPLATES.TemplateResponse(request, "calls.html", {"calls": calls, "org_id": org})

    @app.get("/v1/ui/calls/{call_id}", response_class=HTMLResponse)
    def ui_call(
        request: Request,
        call_id: str,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> HTMLResponse:
        org, _principal = _ui_org(runtime, request, x_api_key)
        if org is None:
            return RedirectResponse("/v1/ui/login", status_code=303)
        revision = runtime.get_revision(org, call_id)
        if revision is None:
            raise HTTPException(status_code=404, detail="not found")
        return TEMPLATES.TemplateResponse(
            request,
            "call_detail.html",
            {
                "call": revision,
                "timeline": timeline_view(revision),
                "analysis": runtime.analysis.get((org, call_id, revision.revision), []),
            },
        )

    @app.get("/v1/ui/latency", response_class=HTMLResponse)
    def ui_latency(request: Request, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> HTMLResponse:
        org, principal = _ui_org(runtime, request, x_api_key)
        if org is None:
            return RedirectResponse("/v1/ui/login", status_code=303)
        data = latency(principal or ApiPrincipal(org_id=org, scope="read", kind="session"))
        return TEMPLATES.TemplateResponse(request, "latency.html", data)

    @app.get("/v1/ui/hangups", response_class=HTMLResponse)
    def ui_hangups(request: Request, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> HTMLResponse:
        org, principal = _ui_org(runtime, request, x_api_key)
        if org is None:
            return RedirectResponse("/v1/ui/login", status_code=303)
        data = hangups(principal or ApiPrincipal(org_id=org, scope="read", kind="session"))
        return TEMPLATES.TemplateResponse(request, "hangups.html", data)

    @app.get("/v1/ui/quality", response_class=HTMLResponse)
    def ui_quality(request: Request, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> HTMLResponse:
        org, principal = _ui_org(runtime, request, x_api_key)
        if org is None:
            return RedirectResponse("/v1/ui/login", status_code=303)
        data = quality(principal or ApiPrincipal(org_id=org, scope="read", kind="session"))
        return TEMPLATES.TemplateResponse(request, "quality.html", data)

    @app.get("/v1/ui/search", response_class=HTMLResponse)
    def ui_search(
        request: Request,
        q: str = "",
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> HTMLResponse:
        org, _principal = _ui_org(runtime, request, x_api_key)
        if org is None:
            return RedirectResponse("/v1/ui/login", status_code=303)
        docs = [
            (rev.call_id, rev.revision, " ".join(t.text or "" for t in rev.turns))
            for rev in runtime.list_calls(org)
        ]
        hits = hybrid_search(q, docs) if q else []
        return TEMPLATES.TemplateResponse(request, "search.html", {"q": q, "hits": hits})

    @app.get("/v1/ui/settings", response_class=HTMLResponse)
    def ui_settings(request: Request, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> HTMLResponse:
        org, _principal = _ui_org(runtime, request, x_api_key)
        if org is None:
            return RedirectResponse("/v1/ui/login", status_code=303)
        connections = [
            {"id": cfg.connection_id, "provider": cfg.provider, "settings": cfg.settings}
            for cfg in runtime.connections.values()
            if cfg.org_id == org
        ]
        return TEMPLATES.TemplateResponse(
            request,
            "settings.html",
            {
                "plugins": runtime.host.inventory(),
                "connections": connections,
                "trusted_operator_installed": True,
            },
        )

    return app


def _ui_org(
    runtime: Runtime, request: Request, token: str | None
) -> tuple[str | None, ApiPrincipal | None]:
    principal = _optional_principal(runtime, token)
    if principal is None:
        cookie = request.cookies.get(SESSION_COOKIE)
        if cookie:
            payload = load_session(runtime.settings.session_secret, cookie)
            if payload:
                principal = ApiPrincipal(
                    org_id=str(payload.get("org_id")),
                    scope=str(payload.get("scope") or "read"),
                    kind="session",
                    role=str(payload.get("role") or "analyst"),
                )
    if principal:
        return principal.org_id, principal
    if runtime.settings.demo:
        orgs = {key[0] for key in runtime.calls} or {"demo"}
        return next(iter(orgs)), None
    return None, None


def _optional_principal(runtime: Runtime, token: str | None) -> ApiPrincipal | None:
    if not token:
        return None
    return runtime.authenticate_api_key(token)


def _call_or_404(runtime: Runtime, org_id: str, call_id: str) -> Any:
    revision = runtime.get_revision(org_id, call_id)
    if revision is None:
        raise HTTPException(status_code=404, detail="not found")
    return revision


def _require_range(from_ts: str | None, to_ts: str | None) -> None:
    if not from_ts or not to_ts:
        raise HTTPException(status_code=400, detail="from and to are required")


def _call_list_item(revision: Any) -> dict[str, Any]:
    return {
        "call_id": revision.call_id,
        "revision": revision.revision,
        "source": revision.identity.source,
        "agent_id": revision.identity.agent_id,
        "started_at": revision.lifecycle.started_at,
        "status": revision.lifecycle.status,
        "fidelity": revision.lifecycle.timeline_fidelity,
        "hangup": revision.hangup.reason if revision.hangup else None,
    }


def _pct(values: list[float]) -> dict[str, float | int | bool | None]:
    if not values:
        return {"p50": None, "p95": None, "count": 0, "approx": True}
    ordered = sorted(values)

    def at(p: float) -> float:
        idx = min(len(ordered) - 1, int(round((p / 100) * (len(ordered) - 1))))
        return ordered[idx]

    return {"p50": at(50), "p95": at(95), "count": len(ordered), "approx": True}
