from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from obsalt._version import __version__
from obsalt.api.auth import current_principal, require_scope
from obsalt.assemble.timeline import timeline_view
from obsalt.config import Settings
from obsalt.ingest.otlp import handle_otlp_http
from obsalt.plugin.protocol import ConnectionConfig
from obsalt.runtime import ApiPrincipal, Runtime
from obsalt.search.hybrid import lexical_rank, reciprocal_rank_fusion

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
        }

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/v1/ingest/{provider}/{ingest_key}")
    async def ingest(provider: str, ingest_key: str, request: Request) -> Response:
        raw = await request.body()
        headers = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in request.headers.items()]
        result = runtime.receive.handle(
            provider=provider,
            ingest_key=ingest_key,
            raw=raw,
            headers=headers,
        )
        # Decode in-process for the functional product; workers lease from outbox in compose.
        if result.envelope_id and result.state and result.state.value == "queued":
            _try_decode(runtime, provider, result.envelope_id, raw)
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
        lexical = lexical_rank(query, docs)
        fused = reciprocal_rank_fusion([lexical])
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
                }
            )
        return {"items": items}

    @app.post("/v1/calls/{call_id}/analyze")
    def request_analyze(
        call_id: str, principal: ApiPrincipal = Depends(require_scope("analyze"))
    ) -> dict[str, Any]:
        revision = _call_or_404(runtime, principal.org_id, call_id)
        return {"call_id": revision.call_id, "revision": revision.revision, "state": "pending"}

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
                    "secret_fields": sorted(cfg.credentials),
                }
            )
        return {"items": items}

    @app.post("/v1/connections")
    def create_connection(
        body: dict[str, Any], principal: ApiPrincipal = Depends(require_scope("admin"))
    ) -> dict[str, Any]:
        ingest_key = str(body.get("ingest_key") or "")
        if not ingest_key:
            from obsalt.crypto.keys import new_ingest_key

            ingest_key = new_ingest_key()
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
        return {"accepted": True, "filter": body}

    @app.post("/v1/backfill")
    def backfill(body: dict[str, Any], principal: ApiPrincipal = Depends(require_scope("admin"))) -> dict[str, Any]:
        return {"accepted": True, "connection_id": body.get("connection_id")}

    @app.post("/v1/privacy/deletion-requests")
    def deletion(body: dict[str, Any], principal: ApiPrincipal = Depends(require_scope("admin"))) -> dict[str, Any]:
        runtime.tombstones.append({"org_id": principal.org_id, **body})
        kind = body.get("kind")
        if kind == "call" and body.get("call_id"):
            runtime.calls.pop((principal.org_id, body["call_id"]), None)
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

    @app.get("/v1/ui/calls", response_class=HTMLResponse)
    def ui_calls(request: Request, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> HTMLResponse:
        principal = _optional_principal(runtime, x_api_key)
        org = principal.org_id if principal else next(iter({k[0] for k in runtime.calls}), "demo")
        calls = runtime.list_calls(org)
        return TEMPLATES.TemplateResponse(request, "calls.html", {"calls": calls, "org_id": org})

    @app.get("/v1/ui/calls/{call_id}", response_class=HTMLResponse)
    def ui_call(request: Request, call_id: str) -> HTMLResponse:
        org = next((org for (org, cid) in runtime.calls if cid == call_id), None)
        if org is None:
            raise HTTPException(status_code=404, detail="not found")
        revision = runtime.get_revision(org, call_id)
        assert revision is not None
        return TEMPLATES.TemplateResponse(
            request,
            "call_detail.html",
            {
                "call": revision,
                "timeline": timeline_view(revision),
                "analysis": runtime.analysis.get((org, call_id, revision.revision), []),
            },
        )

    return app


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


def _pct(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"p50": None, "p95": None, "count": 0}
    ordered = sorted(values)
    def at(p: float) -> float:
        idx = min(len(ordered) - 1, int(round((p / 100) * (len(ordered) - 1))))
        return ordered[idx]
    return {"p50": at(50), "p95": at(95), "count": len(ordered), "approx": True}


def _try_decode(runtime: Runtime, provider: str, envelope_id: str, raw: bytes) -> None:
    from obsalt.plugin.protocol import RawEnvelope
    from obsalt.workers.decode import decode_envelope

    envelope = runtime.inbox.envelopes.get(envelope_id)
    if envelope is None:
        return
    loaded = envelope.model_copy(update={"body": raw})
    try:
        plugin = runtime.host.webhook(provider)
    except Exception:
        return
    try:
        revision = decode_envelope(
            loaded,
            plugin=plugin,
            declaration=runtime.host.get(provider).fidelity,
        )
    except Exception:
        return
    runtime.store_revision(revision)
