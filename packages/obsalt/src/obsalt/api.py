from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from obsalt._version import __version__
from obsalt.assemble.promote import MemoryPointerStore
from obsalt.assemble.timeline import timeline_view
from obsalt.config import Settings
from obsalt.domain.enums import KeyScope
from obsalt.ingest.headers import RawHeaders
from obsalt.ingest.receive import ReceiveLimits, receive_webhook
from obsalt.otel.receiver import parse_otlp_request, request_to_spans, serialized_success
from obsalt.plugin.host import LoadedPlugin, discover_plugins, plugin_by_name
from obsalt.testing.fakes import MemoryInbox, MemoryObjectStore, MemoryResolver
from obsalt.worker.process import MemoryRevisionSink, process_envelope

TEMPLATES_DIR = Path(__file__).resolve().parent / "ui" / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@dataclass
class AppState:
    settings: Settings
    plugins: list[LoadedPlugin]
    resolver: MemoryResolver
    objects: MemoryObjectStore
    inbox: MemoryInbox
    pointers: MemoryPointerStore
    sink: MemoryRevisionSink
    keys: dict[str, tuple[str, frozenset[KeyScope]]] = field(default_factory=dict)
    # plaintext hash -> (org_id, scopes)  -- production hashes at rest


def create_app(settings: Settings | None = None, state: AppState | None = None) -> FastAPI:
    settings = settings or Settings()
    if state is None:
        plugins = discover_plugins()
        state = AppState(
            settings=settings,
            plugins=plugins,
            resolver=MemoryResolver(),
            objects=MemoryObjectStore(),
            inbox=MemoryInbox(),
            pointers=MemoryPointerStore(),
            sink=MemoryRevisionSink(),
        )
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
    async def ingest(provider: str, ingest_key: str, request: Request) -> Response:
        raw = await request.body()
        header_pairs = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in request.headers.items()]
        try:
            loaded = plugin_by_name(provider, state.plugins)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="plugin not installed") from exc
        plugin = loaded.plugin
        result = receive_webhook(
            provider=provider,
            ingest_key=ingest_key,
            raw=raw,
            headers=RawHeaders(header_pairs),
            resolver=state.resolver,
            plugin=plugin,
            objects=state.objects,
            inbox=state.inbox,
            limits=ReceiveLimits(
                compressed_bytes=settings.compressed_body_limit,
                expanded_bytes=settings.expanded_body_limit,
            ),
            compressed_size=int(request.headers.get("content-length") or len(raw)),
        )
        if result.envelope is not None and result.rejected is None:
            process_envelope(
                result.envelope,
                plugin,
                declaration=loaded.fidelity,
                pointers=state.pointers,
                sink=state.sink,
                decoder_version=getattr(plugin, "decoder_version", loaded.name + "/1"),
                source=provider,
            )
        return Response(
            content=result.response.body,
            status_code=result.response.status_code,
            media_type=result.response.media_type,
            headers=result.response.headers,
        )

    @app.post("/v1/traces")
    async def traces(
        request: Request,
        content_type: str = Header("application/json"),
        content_encoding: str | None = Header(None),
        x_api_key: str | None = Header(None, alias="X-API-Key"),
    ) -> Response:
        _require_key(state, x_api_key, KeyScope.INGEST)
        raw = await request.body()
        ct = content_type.split(";")[0].strip()
        try:
            req = parse_otlp_request(ct, raw, content_encoding)
        except HTTPException as exc:
            if exc.status_code == 400:
                raise
            raise
        spans = request_to_spans(req)
        # Durable path: persist raw batch. Decode is a worker concern; for the in-process
        # server we map immediately after the raw write.
        from obsalt.util import sha256_bytes

        org_id = _org_from_key(state, x_api_key)
        key = f"org/{org_id}/raw/otlp/{sha256_bytes(raw)}"
        state.objects.put(key, raw)
        _ = spans  # mapped by worker in production
        return Response(content=serialized_success(), media_type="application/x-protobuf")

    @app.get("/v1/calls")
    def list_calls(
        request: Request,
        x_api_key: str | None = Header(None, alias="X-API-Key"),
        start: datetime | None = None,
        end: datetime | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict:
        org = _require_key(state, x_api_key, KeyScope.READ)
        if start is None or end is None:
            raise HTTPException(status_code=400, detail="start and end are required")
        limit = min(max(limit, 1), 100)
        items = []
        for rev in state.sink.revisions.values():
            if rev.org_id != org:
                continue
            if rev.started_at and (rev.started_at < start or rev.started_at > end):
                continue
            if state.pointers.get(rev.org_id, rev.call_id) != rev.revision:
                continue
            items.append(_call_list_item(rev))
        items.sort(key=lambda r: r.get("started_at") or "", reverse=True)
        return {"items": items[:limit], "as_of_generation": None, "next_cursor": None}

    @app.get("/v1/calls/{call_id}")
    def get_call(call_id: str, x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        org = _require_key(state, x_api_key, KeyScope.READ)
        rev = _active(state, org, call_id)
        return rev.model_dump(mode="json")

    @app.get("/v1/calls/{call_id}/timeline")
    def get_timeline(call_id: str, x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        org = _require_key(state, x_api_key, KeyScope.READ)
        rev = _active(state, org, call_id)
        return timeline_view(rev)

    @app.get("/v1/calls/{call_id}/evidence/{ref}")
    def get_evidence(call_id: str, ref: str, x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        org = _require_key(state, x_api_key, KeyScope.READ)
        _active(state, org, call_id)
        raise HTTPException(status_code=404, detail="evidence not found")

    @app.post("/v1/search")
    async def search(request: Request, x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        _require_key(state, x_api_key, KeyScope.READ)
        body = await request.json()
        query = str(body.get("q") or "")
        hits = []
        for rev in state.sink.revisions.values():
            blob = " ".join(t.text for t in rev.turns)
            if query.lower() in blob.lower():
                hits.append({"call_id": rev.call_id, "revision": rev.revision})
        return {"items": hits}

    @app.get("/v1/latency")
    def latency(x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        _require_key(state, x_api_key, KeyScope.READ)
        return {"items": [], "as_of_generation": None, "note": "approximate percentiles from serving generation"}

    @app.get("/v1/hangups")
    def hangups(x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        _require_key(state, x_api_key, KeyScope.READ)
        return {"items": [], "as_of_generation": None}

    @app.get("/v1/tools")
    def tools(x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        _require_key(state, x_api_key, KeyScope.READ)
        return {"items": [], "as_of_generation": None}

    @app.get("/v1/quality")
    def quality(x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        _require_key(state, x_api_key, KeyScope.READ)
        return {"items": [], "as_of_generation": None}

    @app.post("/v1/calls/{call_id}/analyze")
    def analyze(call_id: str, x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        _require_key(state, x_api_key, KeyScope.ANALYZE)
        return {"state": "pending"}

    @app.get("/v1/rubrics")
    def list_rubrics(x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        _require_key(state, x_api_key, KeyScope.READ)
        return {"items": []}

    @app.get("/v1/connections")
    def list_connections(x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        _require_key(state, x_api_key, KeyScope.ADMIN)
        return {
            "items": [
                {"provider": cfg.provider, "connection_id": cfg.connection_id, "org_id": cfg.org_id}
                for cfg in state.resolver.connections.values()
            ]
        }

    @app.post("/v1/replay")
    def replay(x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        _require_key(state, x_api_key, KeyScope.ADMIN)
        return {"status": "queued"}

    @app.post("/v1/backfill")
    def backfill(x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        _require_key(state, x_api_key, KeyScope.ADMIN)
        return {"status": "queued"}

    @app.post("/v1/privacy/deletion-requests")
    async def deletion(request: Request, x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict:
        org = _require_key(state, x_api_key, KeyScope.ADMIN)
        body = await request.json()
        call_id = body.get("call_id")
        if call_id:
            from obsalt.plugin.types import TombstoneHints

            state.inbox.tombstone(org, TombstoneHints(source_call_id=call_id))
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
        calls = [
            rev
            for rev in state.sink.revisions.values()
            if state.pointers.get(rev.org_id, rev.call_id) == rev.revision
        ]
        return templates.TemplateResponse(request, "call_list.html", {"calls": calls})

    @app.get("/v1/ui/calls/{call_id}", response_class=HTMLResponse)
    def ui_call(request: Request, call_id: str) -> HTMLResponse:
        rev = next((r for r in state.sink.revisions.values() if r.call_id == call_id), None)
        if rev is None:
            raise HTTPException(status_code=404)
        return templates.TemplateResponse(
            request,
            "call_detail.html",
            {"call": rev, "timeline": timeline_view(rev), "coverage": rev.coverage},
        )

    return app


def _require_key(state: AppState, key: str | None, scope: KeyScope) -> str:
    if not state.keys:
        # Bootstrap: if no keys configured, reject. require_auth=false is deleted.
        raise HTTPException(status_code=401, detail="API key required")
    if not key:
        raise HTTPException(status_code=401, detail="API key required")
    found = state.keys.get(key)
    if found is None:
        raise HTTPException(status_code=401, detail="invalid API key")
    org, scopes = found
    if scope not in scopes and KeyScope.ADMIN not in scopes:
        raise HTTPException(status_code=403, detail="insufficient scope")
    return org


def _org_from_key(state: AppState, key: str | None) -> str:
    if not key or key not in state.keys:
        raise HTTPException(status_code=401, detail="invalid API key")
    return state.keys[key][0]


def _active(state: AppState, org: str, call_id: str):
    rev_id = state.pointers.get(org, call_id)
    if not rev_id:
        raise HTTPException(status_code=404, detail="not found")
    rev = state.sink.get(org, call_id, rev_id)
    if rev is None or rev.org_id != org:
        raise HTTPException(status_code=404, detail="not found")
    return rev


def _call_list_item(rev) -> dict:
    return {
        "id": rev.call_id,
        "revision": rev.revision,
        "source": rev.source,
        "agent_id": rev.agent_id,
        "status": rev.status.value,
        "started_at": rev.started_at.isoformat() if rev.started_at else None,
        "timeline_fidelity": rev.timeline_fidelity.value,
        "hangup": rev.hangup.reason.value if rev.hangup else None,
    }
