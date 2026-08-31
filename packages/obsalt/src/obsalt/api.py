"""HTTP API and server-rendered UI. Tests must call `create_test_app()`."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from obsalt._version import __version__
from obsalt.analysis.hallucination import hallucination_claim_list
from obsalt.analysis.rollups import _is_confirmed_claim
from obsalt.assemble.timeline import timeline_view
from obsalt.config import Settings
from obsalt.domain.enums import KeyScope, Role
from obsalt.domain.models import CallRevision, Rubric
from obsalt.ingest.headers import RawHeaders
from obsalt.ingest.otlp import receive_otlp_batch
from obsalt.ingest.receive import ReceiveLimits, receive_webhook
from obsalt.ops.backfill import run_backfill
from obsalt.ops.health import collect_health
from obsalt.ops.privacy import apply_deletion
from obsalt.ops.seed import environment_allowed, ingest_seed_corpus
from obsalt.otel.receiver import (
    parse_otlp_request,
    request_to_spans,
    serialized_partial_success,
    serialized_success,
)
from obsalt.otel.span_identity import SpanIdentityIndex
from obsalt.plugin.contract import WebhookSource
from obsalt.plugin.host import plugin_by_name
from obsalt.query import (
    active_calls,
    analysis_for,
    analysis_for_active,
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
from obsalt.runtime import (
    AppState,
    add_org_spend,
    bump_generation,
    create_connection,
    in_memory_state,
    org_spend_usd,
)
from obsalt.security.authz import allowed
from obsalt.security.sessions import check_csrf, read_session, sign_session
from obsalt.ui.filters import parse_ui_range, present_filter_bar
from obsalt.ui.present import (
    SETTING_NOTICES,
    empty_calls,
    present_call_detail,
    present_call_rows,
    present_can,
    present_connection_rows,
    present_dlq,
    present_fleet,
    present_hangups,
    present_health,
    present_latency,
    present_plugin_rows,
    present_quality,
    present_search,
    role_label,
)
from obsalt.util import canonical_json, new_id
from obsalt.worker.process import drain_inbox
from obsalt.worker.replay import replay_envelopes

TEMPLATES_DIR = Path(__file__).resolve().parent / "ui" / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
log = logging.getLogger("obsalt.api")


def create_test_app(settings: Settings | None = None, state: AppState | None = None) -> FastAPI:
    """Test helper. Memory stores are doubles, not a production backend."""
    settings = settings or Settings(environment="test")
    return create_app(settings, state or in_memory_state(settings))


def create_app(settings: Settings | None = None, state: AppState | None = None) -> FastAPI:
    settings = settings or Settings()
    if state is None:
        env = (settings.environment or "").lower()
        if env in {"test", "testing"}:
            state = in_memory_state(settings)
        else:
            raise RuntimeError(
                "create_app requires an AppState. obsalt serve uses production_state(); "
                "tests should call create_test_app(). Memory stores are not a production backend."
            )

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

    app = FastAPI(
        title="obsalt",
        version=__version__,
        summary="Self-hosted call analytics and quality for AI voice agents",
        description=(
            "HTTP API for the obsalt service. The console at `/v1/ui` is HTML over "
            "these routes. Scripts authenticate with `X-API-Key`. Collection list "
            "endpoints require a bounded `start` and `end`. Cross-org identifiers "
            "return 404.\n\n"
            "Interactive OpenAPI lives at `/docs`. Human docs: `docs/api.md` and "
            "`docs/product.md` in the repository."
        ),
        lifespan=lifespan,
        license_info={"name": "Apache-2.0", "url": "https://www.apache.org/licenses/LICENSE-2.0"},
    )
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
            **collect_health(state),
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
        header_pairs = [
            (k.encode("latin-1"), v.encode("latin-1")) for k, v in request.headers.items()
        ]
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
            plugin=cast(WebhookSource, loaded.plugin),
            objects=state.objects,
            inbox=state.inbox,
            limits=ReceiveLimits(
                compressed_bytes=settings.compressed_body_limit,
                expanded_bytes=settings.expanded_body_limit,
            ),
            compressed_size=int(request.headers.get("content-length") or len(raw)),
            leases=getattr(state, "leases", None),
            content_encoding=encoding,
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
            req = await asyncio.to_thread(
                parse_otlp_request,
                ct,
                raw,
                content_encoding,
                expanded_bytes=settings.expanded_body_limit,
            )
        except HTTPException as exc:
            if exc.status_code == 415:
                raise
            raise
        spans = request_to_spans(req)
        from obsalt.otel.tenancy import reject_tenant_assertions

        denied = reject_tenant_assertions(spans, org)
        if denied:
            raise HTTPException(status_code=401, detail=denied)
        extra_headers = {}
        if content_encoding:
            extra_headers["content-encoding"] = content_encoding
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
            extra_headers=extra_headers or None,
            backpressure_limit=state.settings.outbox_backpressure_limit,
        )
        if result.status_code == 503:
            raise HTTPException(status_code=503, detail=result.rejected or "ingest capacity")
        if result.status_code == 413:
            raise HTTPException(status_code=413, detail=result.rejected)
        if result.status_code == 409:
            # Permanent identity conflict: OTLP partial success, clients must not retry.
            return Response(
                content=serialized_partial_success(
                    rejected=1, error_message=result.rejected or "span identity conflict"
                ),
                media_type="application/x-protobuf",
            )
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
    ) -> dict[str, Any]:
        org = _authorize(state, x_api_key, KeyScope.READ, "calls.read")
        if start is None or end is None:
            raise HTTPException(status_code=400, detail="start and end are required")
        limit = min(max(limit, 1), 100)
        summaries_page = None
        if not flag and not eval_result and latency_ms is None:
            summaries_page = state.pointers.list_summaries(
                org,
                start=start,
                end=end,
                cursor=cursor,
                limit=limit,
                agent_id=agent_id,
                source=source,
                outcome=outcome,
            )
        if summaries_page is not None:
            summaries, next_cursor = summaries_page
            page = []
            for row in summaries:
                rev = state.sink.get(org, row["call_id"], row["revision"])
                if rev is None:
                    page.append(
                        {
                            "id": row["call_id"],
                            "revision": row["revision"],
                            "source": row.get("source"),
                            "agent_id": row.get("agent_id"),
                            "status": row.get("status"),
                            "started_at": row["started_at"].isoformat()
                            if row.get("started_at")
                            else None,
                            "hangup": row.get("hangup_reason"),
                        }
                    )
                    continue
                page.append(call_list_item(rev))
            return {
                "items": page,
                "as_of_generation": state.rollup_generation,
                "next_cursor": next_cursor,
            }
        offset_items = []
        for rev in active_calls(state, org):
            if not in_range(rev, start, end):
                continue
            analysis = analysis_for(state, org, rev.call_id, rev.revision)
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
    def get_call(
        call_id: str, x_api_key: str | None = Header(None, alias="X-API-Key")
    ) -> dict[str, Any]:
        org = _authorize(state, x_api_key, KeyScope.READ, "calls.read")
        rev = _active(state, org, call_id)
        payload = rev.model_dump(mode="json")
        payload["analysis"] = [
            r.model_dump(mode="json") if hasattr(r, "model_dump") else r
            for r in analysis_for(state, org, call_id, rev.revision)
        ]
        return payload

    @app.get("/v1/calls/{call_id}/timeline")
    def get_timeline(
        call_id: str, x_api_key: str | None = Header(None, alias="X-API-Key")
    ) -> dict[str, Any]:
        org = _authorize(state, x_api_key, KeyScope.READ, "calls.read")
        return timeline_view(_active(state, org, call_id))

    @app.get("/v1/calls/{call_id}/evidence/{ref}")
    def get_evidence(
        call_id: str, ref: str, x_api_key: str | None = Header(None, alias="X-API-Key")
    ) -> Response:
        org = _authorize(state, x_api_key, KeyScope.READ, "calls.read")
        rev = _active(state, org, call_id)
        body, content_type = _load_evidence(state, org, rev, ref)
        if body is None:
            raise HTTPException(status_code=404, detail="evidence not found")
        return Response(content=body, media_type=content_type)

    @app.post("/v1/search")
    async def search(
        request: Request, x_api_key: str | None = Header(None, alias="X-API-Key")
    ) -> dict[str, Any]:
        org = _authorize(state, x_api_key, KeyScope.READ, "search.read")
        body = await request.json()
        query = str(body.get("q") or "")
        start = body.get("start")
        end = body.get("end")
        if not start or not end:
            raise HTTPException(status_code=400, detail="start and end are required")
        start_dt = (
            datetime.fromisoformat(start.replace("Z", "+00:00"))
            if isinstance(start, str)
            else start
        )
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00")) if isinstance(end, str) else end
        filters = {k: body.get(k) for k in ("agent_id", "source", "hangup_reason") if body.get(k)}
        filters["start"] = start_dt
        filters["end"] = end_dt
        calls = [c for c in active_calls(state, org) if in_range(c, start_dt, end_dt)]
        items = search_calls(
            calls,
            query,
            index=state.search,
            org_id=org,
            filters=filters,
        )
        return {"items": items}

    @app.get("/v1/latency")
    def latency(
        x_api_key: str | None = Header(None, alias="X-API-Key"),
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict[str, Any]:
        org = _authorize(state, x_api_key, KeyScope.READ, "fleet.read")
        start, end = _require_range(start, end)
        calls = [c for c in active_calls(state, org) if in_range(c, start, end)]
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
    ) -> dict[str, Any]:
        org = _authorize(state, x_api_key, KeyScope.READ, "fleet.read")
        start, end = _require_range(start, end)
        calls = [c for c in active_calls(state, org) if in_range(c, start, end)]
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
    ) -> dict[str, Any]:
        org = _authorize(state, x_api_key, KeyScope.READ, "fleet.read")
        start, end = _require_range(start, end)
        calls = [c for c in active_calls(state, org) if in_range(c, start, end)]
        return tools_rollup(calls, as_of_generation=state.rollup_generation)

    @app.get("/v1/quality")
    def quality(
        x_api_key: str | None = Header(None, alias="X-API-Key"),
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict[str, Any]:
        org = _authorize(state, x_api_key, KeyScope.READ, "fleet.read")
        start, end = _require_range(start, end)
        calls = [c for c in active_calls(state, org) if in_range(c, start, end)]
        return quality_rollup(
            calls,
            analysis_for_active(state, org, calls),
            as_of_generation=state.rollup_generation,
        )

    @app.post("/v1/calls/{call_id}/analyze")
    async def analyze(
        call_id: str, x_api_key: str | None = Header(None, alias="X-API-Key")
    ) -> dict[str, Any]:
        org = _authorize(state, x_api_key, KeyScope.ANALYZE, "calls.analyze")
        return await _run_manual_analysis(state, org, call_id)

    @app.get("/v1/rubrics")
    def list_rubrics(x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict[str, Any]:
        org = _authorize(state, x_api_key, KeyScope.READ, "settings.read")
        return {
            "items": [r.model_dump(mode="json") for r in state.rubrics.values() if r.org_id == org]
        }

    @app.post("/v1/rubrics")
    async def create_rubric(
        request: Request, x_api_key: str | None = Header(None, alias="X-API-Key")
    ) -> dict[str, Any]:
        org = _authorize(state, x_api_key, KeyScope.ANALYZE, "rubrics.write")
        body = await request.json()
        kind, spec = _rubric_kind_spec(body)
        rubric = Rubric(
            id=new_id(),
            org_id=org,
            name=str(body.get("name") or "rubric"),
            description=str(body.get("description") or ""),
            threshold=float(body.get("threshold") or 0.7),
            kind=kind,
            spec=spec,
        )
        if state.rubric_store is not None:
            state.rubric_store.insert(rubric)
        state.rubrics[rubric.id] = rubric
        return rubric.model_dump(mode="json")

    @app.put("/v1/rubrics/{rubric_id}")
    async def update_rubric(
        rubric_id: str, request: Request, x_api_key: str | None = Header(None, alias="X-API-Key")
    ) -> dict[str, Any]:
        org = _authorize(state, x_api_key, KeyScope.ANALYZE, "rubrics.write")
        existing = state.rubrics.get(rubric_id)
        if existing is None or existing.org_id != org:
            raise HTTPException(status_code=404, detail="not found")
        body = await request.json()
        store = state.rubric_store
        enabled = existing.enabled if "enabled" not in body else _as_bool(body.get("enabled"))
        threshold = (
            float(body["threshold"]) if body.get("threshold") is not None else existing.threshold
        )
        updated: Rubric
        kind, spec = _rubric_kind_spec(body, existing=existing)
        if store is not None:
            updated = store.new_version(
                existing,
                name=str(body["name"]) if body.get("name") else None,
                description=str(body["description"])
                if body.get("description") is not None
                else None,
                threshold=threshold,
                enabled=enabled,
                kind=kind,
                spec=spec,
            )
        else:
            updated = existing.model_copy(
                update={
                    "name": str(body.get("name") or existing.name),
                    "description": str(
                        body.get("description")
                        if body.get("description") is not None
                        else existing.description
                    ),
                    "version": existing.version + 1,
                    "threshold": threshold,
                    "enabled": enabled,
                    "kind": kind,
                    "spec": spec,
                }
            )
        if updated.id != rubric_id:
            state.rubrics.pop(rubric_id, None)
        state.rubrics[updated.id] = updated
        return updated.model_dump(mode="json")

    @app.delete("/v1/rubrics/{rubric_id}")
    def delete_rubric(
        rubric_id: str, x_api_key: str | None = Header(None, alias="X-API-Key")
    ) -> dict[str, Any]:
        org = _authorize(state, x_api_key, KeyScope.ANALYZE, "rubrics.write")
        existing = state.rubrics.get(rubric_id)
        if existing is None or existing.org_id != org:
            raise HTTPException(status_code=404, detail="not found")
        del state.rubrics[rubric_id]
        if state.rubric_store is not None:
            state.rubric_store.delete(org, rubric_id)
        return {"status": "deleted"}

    @app.get("/v1/eval-runners")
    def list_eval_runners(
        x_api_key: str | None = Header(None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        org = _authorize(state, x_api_key, KeyScope.ANALYZE, "settings.read")
        from obsalt.analysis.pack import catalog_rows
        from obsalt.analysis.runners import enabled_pack, get_policy, list_runners

        policy = get_policy(state, org).model_dump(mode="json")
        pack = list(enabled_pack(state, org))
        policy["pack"] = pack
        return {
            "items": [item.public_dict() for item in list_runners(state, org)],
            "policy": policy,
            "pack": catalog_rows(pack),
        }

    @app.post("/v1/eval-runners")
    async def post_eval_runner(
        request: Request, x_api_key: str | None = Header(None, alias="X-API-Key")
    ) -> dict[str, Any]:
        org = _authorize(state, x_api_key, KeyScope.ADMIN, "eval_runners.write")
        from obsalt.analysis.runners import runner_from_body, store_of
        from obsalt.egress import EgressDenied

        body = await request.json()
        store = store_of(state)
        if store is None:
            raise HTTPException(status_code=503, detail="eval runner store unavailable")
        try:
            runner = runner_from_body(org, body)
            stored = store.upsert(runner)
        except (ValueError, EgressDenied) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return stored.public_dict()

    @app.delete("/v1/eval-runners/{runner_id}")
    def delete_eval_runner(
        runner_id: str, x_api_key: str | None = Header(None, alias="X-API-Key")
    ) -> dict[str, Any]:
        org = _authorize(state, x_api_key, KeyScope.ADMIN, "eval_runners.write")
        from obsalt.analysis.runners import store_of

        store = store_of(state)
        if store is None or not store.delete(org, runner_id):
            raise HTTPException(status_code=404, detail="not found")
        return {"status": "deleted"}

    @app.put("/v1/eval-policy")
    async def put_eval_policy(
        request: Request, x_api_key: str | None = Header(None, alias="X-API-Key")
    ) -> dict[str, Any]:
        org = _authorize(state, x_api_key, KeyScope.ADMIN, "eval_runners.write")
        from obsalt.analysis.runners import get_policy, list_runners, store_of, validate_policy
        from obsalt.domain.models import EvalPolicy

        store = store_of(state)
        if store is None:
            raise HTTPException(status_code=503, detail="eval runner store unavailable")
        body = await request.json()
        current = get_policy(state, org)
        enabled_raw = body.get("llm_evals_enabled")
        if enabled_raw is None:
            enabled = current.llm_evals_enabled
        else:
            enabled = (
                bool(enabled_raw)
                if not isinstance(enabled_raw, str)
                else enabled_raw.lower()
                in {
                    "1",
                    "true",
                    "on",
                    "yes",
                }
            )
        raw_pack = body.get("pack")
        if raw_pack is None:
            pack = list(current.pack)
        elif isinstance(raw_pack, list):
            pack = [str(item) for item in raw_pack]
        else:
            pack = [str(raw_pack)]
        try:
            policy = validate_policy(
                EvalPolicy(
                    org_id=org,
                    monthly_budget_usd=float(
                        body.get("monthly_budget_usd", current.monthly_budget_usd)
                    ),
                    baseline_sample_rate=float(
                        body.get("baseline_sample_rate", current.baseline_sample_rate)
                    ),
                    llm_evals_enabled=enabled,
                    pack=pack,
                    groundedness_enabled=_policy_flag(
                        body.get("groundedness_enabled"), current.groundedness_enabled
                    ),
                    groundedness_sample_rate=float(
                        body.get("groundedness_sample_rate", current.groundedness_sample_rate)
                    ),
                ),
                list_runners(state, org),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return store.set_policy(policy).model_dump(mode="json")

    @app.post("/v1/eval-runners/{runner_id}/ping")
    async def ping_eval_runner(
        runner_id: str, x_api_key: str | None = Header(None, alias="X-API-Key")
    ) -> dict[str, Any]:
        org = _authorize(state, x_api_key, KeyScope.ADMIN, "eval_runners.write")
        from obsalt.analysis.runners import ping_runner, store_of
        from obsalt.egress import EgressDenied

        store = store_of(state)
        runner = store.get(org, runner_id) if store is not None else None
        if runner is None:
            raise HTTPException(status_code=404, detail="not found")
        try:
            return await ping_runner(runner)
        except EgressDenied as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 — ping must surface the judge error
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/v1/connections")
    def list_connections(x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict[str, Any]:
        org = _authorize(state, x_api_key, KeyScope.ADMIN, "connections.write")
        items = []
        for cfg in state.resolver.list_for_org(org):
            items.append(
                {
                    "provider": cfg.provider,
                    "connection_id": cfg.connection_id,
                    "org_id": cfg.org_id,
                    "secret_fields": sorted(cfg.secrets),
                    "settings": dict(cfg.settings or {}),
                }
            )
        return {"items": items}

    @app.post("/v1/connections")
    async def post_connection(
        request: Request, x_api_key: str | None = Header(None, alias="X-API-Key")
    ) -> dict[str, Any]:
        org = _authorize(state, x_api_key, KeyScope.ADMIN, "connections.write")
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
    def delete_connection(
        connection_id: str, x_api_key: str | None = Header(None, alias="X-API-Key")
    ) -> dict[str, Any]:
        org = _authorize(state, x_api_key, KeyScope.ADMIN, "connections.write")
        if not state.resolver.delete(org, connection_id):
            raise HTTPException(status_code=404, detail="not found")
        state.connections_plaintext.pop(connection_id, None)
        return {"status": "deleted"}

    @app.get("/v1/outbound-webhooks")
    def list_outbound(x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict[str, Any]:
        org = _authorize(state, x_api_key, KeyScope.ADMIN, "webhooks.write")
        items = []
        dests = (
            state.webhook_store.list_destinations(org)
            if state.webhook_store is not None
            else state.webhook_destinations
        )
        for dest in dests:
            if dest.get("org_id") and dest.get("org_id") != org:
                continue
            items.append(
                {"id": dest.get("id"), "url": dest.get("url"), "event_type": dest.get("event_type")}
            )
        return {"items": items}

    @app.post("/v1/outbound-webhooks")
    async def create_outbound(
        request: Request, x_api_key: str | None = Header(None, alias="X-API-Key")
    ) -> dict[str, Any]:
        org = _authorize(state, x_api_key, KeyScope.ADMIN, "webhooks.write")
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
        store = getattr(state, "webhook_store", None)
        if store is not None and getattr(store, "durable", False):
            store.create(dest)
        else:
            state.webhook_destinations.append(dest)
        return {"id": dest["id"], "url": url, "secret": secret}

    @app.post("/v1/outbound-webhooks/{dest_id}/rotate")
    async def rotate_outbound(
        dest_id: str, request: Request, x_api_key: str | None = Header(None, alias="X-API-Key")
    ) -> dict[str, Any]:
        org = _authorize(state, x_api_key, KeyScope.ADMIN, "webhooks.write")
        from obsalt.webhooks.outbound import mint_whsec

        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        secret = str(body.get("secret") or mint_whsec())
        overlap = int(body.get("overlap_seconds") or state.settings.key_rotation_overlap_seconds)
        store = getattr(state, "webhook_store", None)
        if store is not None and getattr(store, "durable", False):
            store.rotate_secret(org, dest_id, secret, overlap_seconds=overlap)
        else:
            from datetime import timedelta

            from obsalt.util import utcnow

            for dest in state.webhook_destinations:
                if dest.get("id") == dest_id and dest.get("org_id") == org:
                    dest["previous_secret"] = dest.get("secret")
                    dest["previous_secret_expires_at"] = (
                        utcnow() + timedelta(seconds=overlap)
                    ).isoformat()
                    dest["secret"] = secret
                    break
        return {"id": dest_id, "secret": secret, "overlap_seconds": overlap}

    @app.post("/v1/keys/rotate")
    async def rotate_key(
        request: Request, x_api_key: str | None = Header(None, alias="X-API-Key")
    ) -> dict[str, Any]:
        org = _authorize(state, x_api_key, KeyScope.ADMIN, "keys.rotate")
        import secrets as secretsmod
        from datetime import timedelta

        from obsalt.util import utcnow

        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        old = str(body.get("current_key") or x_api_key or "")
        overlap = int(body.get("overlap_seconds") or state.settings.key_rotation_overlap_seconds)
        new_key = secretsmod.token_urlsafe(24)
        if state.key_directory is not None:
            state.key_directory.rotate(org, old, new_key, overlap_seconds=overlap)
        scopes = state.keys.get(old, (org, frozenset(KeyScope)))[1]
        state.keys[new_key] = (org, scopes)
        state.key_roles[new_key] = _role_for_key(state, old, scopes)
        state.key_expiry[old] = utcnow() + timedelta(seconds=overlap)
        return {"key": new_key, "overlap_seconds": overlap}

    @app.post("/v1/replay")
    async def replay(
        request: Request, x_api_key: str | None = Header(None, alias="X-API-Key")
    ) -> dict[str, Any]:
        org = _authorize(state, x_api_key, KeyScope.ADMIN, "replay")
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
    async def backfill(
        request: Request, x_api_key: str | None = Header(None, alias="X-API-Key")
    ) -> dict[str, Any]:
        org = _authorize(state, x_api_key, KeyScope.ADMIN, "backfill")
        body: dict[str, Any] = {}
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
    async def deletion(
        request: Request, x_api_key: str | None = Header(None, alias="X-API-Key")
    ) -> dict[str, Any]:
        org = _authorize(state, x_api_key, KeyScope.ADMIN, "privacy.delete")
        body = await request.json()
        call_id = body.get("call_id")
        source_call_id = body.get("source_call_id")
        start = body.get("start")
        end = body.get("end")
        if (
            not call_id
            and not source_call_id
            and not body.get("caller")
            and not body.get("caller_token")
            and not (start and end)
        ):
            raise HTTPException(
                status_code=400, detail="call_id, source_call_id, caller, or start/end is required"
            )
        return apply_deletion(
            state,
            org_id=org,
            call_id=str(call_id) if call_id else None,
            source_call_id=str(source_call_id) if source_call_id else None,
            caller=str(body["caller"]) if body.get("caller") else None,
            caller_token_value=str(body["caller_token"]) if body.get("caller_token") else None,
            start=datetime.fromisoformat(start.replace("Z", "+00:00"))
            if isinstance(start, str)
            else start,
            end=datetime.fromisoformat(end.replace("Z", "+00:00")) if isinstance(end, str) else end,
        )

    @app.post("/v1/quality/review")
    async def review(
        request: Request, x_api_key: str | None = Header(None, alias="X-API-Key")
    ) -> dict[str, Any]:
        org = _authorize(state, x_api_key, KeyScope.READ, "quality.review")
        body = await request.json()
        item = {
            "org_id": org,
            "call_id": str(body.get("call_id") or ""),
            "revision": str(body.get("revision") or ""),
            "analyzer_id": str(body.get("analyzer_id") or ""),
            "severity": str(body.get("severity") or ""),
            "agree": bool(body.get("agree")),
            "note": str(body.get("note") or ""),
        }
        if state.review_store is not None:
            item = state.review_store.insert(item)
        else:
            state.reviews.append(item)
        return {"status": "recorded", "item": item}

    @app.get("/v1/plugins")
    def plugins(x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict[str, Any]:
        _authorize(state, x_api_key, KeyScope.READ, "plugins.read")
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
        ui_range = parse_ui_range(request.query_params)
        values = _ui_filter_values(request)
        ranged = _ui_in_range_calls(state, org, ui_range.start, ui_range.end)
        error = "start and end are required" if org and not ui_range.valid else None
        analysis_by_call: dict[str, list[Any]] = {}
        filtered: list[CallRevision] = []
        for call in ranged:
            analysis = analysis_for(state, org, call.call_id, call.revision) if org else []
            analysis_by_call[call.call_id] = analysis
            if matches_call_filters(
                call,
                agent_id=values["agent_id"] or None,
                outcome=values["hangup_reason"] or None,
                source=values["source"] or None,
                latency_ms=_optional_float(values.get("latency_ms")),
                flag=values["flag"] or None,
                eval_result=values.get("eval_result") or None,
                analysis=analysis,
            ):
                filtered.append(call)
        limit = min(max(int(request.query_params.get("limit") or 50), 1), 100)
        cursor = request.query_params.get("cursor")
        page, next_cursor = paginate_calls(
            [{"id": c.call_id, "revision": c.revision} for c in filtered],
            cursor=cursor,
            limit=limit,
        )
        id_set = {(item["id"], item["revision"]) for item in page}
        page_calls = [c for c in filtered if (c.call_id, c.revision) in id_set]
        flags_by_call = {
            call.call_id: _ui_flags_and_evals(analysis_by_call.get(call.call_id) or [])[0]
            for call in page_calls
        }
        generation = state.rollup_generation
        latency = latency_rollup(
            filtered, as_of_generation=generation, store=getattr(state, "rollups", None), org_id=org
        )
        hangups = hangup_rollup(
            filtered,
            as_of_generation=generation,
            store=getattr(state, "hangup_clusters", None),
            org_id=org,
        )
        quality = quality_rollup(
            filtered,
            analysis_for_active(state, org, filtered) if org else [],
            as_of_generation=generation,
        )
        tools = tools_rollup(filtered, as_of_generation=generation)
        filter_bar = present_filter_bar(
            action="/v1/ui",
            ui_range=ui_range,
            values=values,
            fields=("source", "agent", "hangup", "flag", "eval", "latency"),
            plugins=state.plugins,
            calls=ranged,
            next_cursor=next_cursor,
        )
        return _render(
            request,
            "call_list.html",
            {
                "calls": present_call_rows(page_calls, flags_by_call),
                "fleet": present_fleet(
                    filtered,
                    latency=latency,
                    hangups=hangups,
                    quality=quality,
                    tools=tools,
                    range_qs=ui_range.range_qs,
                ),
                "empty": empty_calls(
                    signed_in=bool(org),
                    has_connections=bool(org and _org_connections(state, org)),
                    can_connect=bool(org)
                    and allowed(_ui_role(request, state), "connections.write"),
                    can_seed=bool(org)
                    and allowed(_ui_role(request, state), "connections.write")
                    and environment_allowed(state.settings),
                    worker_stuck=bool(
                        org
                        and not _org_has_calls(state, org)
                        and int(collect_health(state).get("outbox_depth") or 0) > 0
                    ),
                ),
                "org": org,
                "filters": filter_bar,
                "filter_bar": filter_bar,
                "next_cursor": next_cursor,
                "error": error,
                "nav_qs": filter_bar["nav_qs"],
            },
        )

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
        cookie = sign_session(
            org, state.settings.session_secret, role=_role_for_key(state, key, scopes)
        )
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

    @app.post("/v1/ui/logout")
    async def ui_logout(request: Request) -> Response:
        form = await request.form()
        provided = str(form.get("csrf") or request.headers.get("x-csrf-token") or "")
        if request.cookies.get("obsalt_session"):
            _require_csrf(request, state, provided)
        response = RedirectResponse("/v1/ui/login", status_code=303)
        response.delete_cookie("obsalt_session")
        response.delete_cookie("obsalt_csrf")
        return response

    @app.get("/v1/ui/calls/{call_id}", response_class=HTMLResponse)
    def ui_call(request: Request, call_id: str) -> HTMLResponse:
        org = _ui_org(request, state)
        if not org:
            raise HTTPException(status_code=401, detail="session required")
        rev = _active(state, org, call_id)
        analysis = analysis_for(state, org, call_id, rev.revision)
        flags, evals, card = _ui_flags_and_evals(analysis)
        timeline = timeline_view(rev)
        view = present_call_detail(
            rev,
            timeline,
            flags,
            evals,
            quality_card=card,
            groundedness=_ui_groundedness_payload(analysis),
        )
        ui_range = parse_ui_range(request.query_params)
        nav_qs = f"?{ui_range.range_qs}" if ui_range.range_qs else ""
        return _render(
            request,
            "call_detail.html",
            {
                "call": rev,
                "view": view,
                "timeline": view["timeline"],
                "coverage": rev.coverage,
                "org": org,
                "flags": flags,
                "evals": evals,
                "calls_href": f"/v1/ui{nav_qs}",
                "nav_qs": nav_qs,
            },
        )

    @app.get("/v1/ui/latency", response_class=HTMLResponse)
    def ui_latency(request: Request) -> HTMLResponse:
        org = _ui_org(request, state)
        _ui_range, _values, _ranged, calls, filter_bar, error = _ui_collection(
            request,
            state,
            org,
            action="/v1/ui/latency",
            fields=("source", "agent"),
        )
        data = latency_rollup(
            calls,
            as_of_generation=state.rollup_generation,
            store=getattr(state, "rollups", None),
            org_id=org,
        )
        tools = tools_rollup(calls, as_of_generation=state.rollup_generation)
        return _render(
            request,
            "latency.html",
            {
                "rollup": data,
                "view": present_latency(data, calls, tools),
                "org": org,
                "filters": filter_bar,
                "filter_bar": filter_bar,
                "error": error,
                "nav_qs": filter_bar["nav_qs"],
            },
        )

    @app.get("/v1/ui/hangups", response_class=HTMLResponse)
    def ui_hangups(request: Request) -> HTMLResponse:
        org = _ui_org(request, state)
        _ui_range, _values, _ranged, calls, filter_bar, error = _ui_collection(
            request,
            state,
            org,
            action="/v1/ui/hangups",
            fields=("source", "agent", "hangup"),
        )
        data = hangup_rollup(
            calls,
            as_of_generation=state.rollup_generation,
            store=getattr(state, "hangup_clusters", None),
            org_id=org,
        )
        view = present_hangups(data, len(calls), calls)
        return _render(
            request,
            "hangups.html",
            {
                "clusters": view["clusters"],
                "as_of_generation": view["as_of_generation"],
                "view": view,
                "org": org,
                "filters": filter_bar,
                "filter_bar": filter_bar,
                "error": error,
                "nav_qs": filter_bar["nav_qs"],
            },
        )

    @app.get("/v1/ui/quality", response_class=HTMLResponse)
    def ui_quality(request: Request) -> HTMLResponse:
        org = _ui_org(request, state)
        _ui_range, _values, _ranged, calls, filter_bar, error = _ui_collection(
            request,
            state,
            org,
            action="/v1/ui/quality",
            fields=("source", "agent", "flag"),
        )
        data = quality_rollup(
            calls,
            analysis_for_active(state, org, calls) if org else [],
            as_of_generation=state.rollup_generation,
        )
        spend = org_spend_usd(state, org) if org else 0.0
        budget = _ui_eval_budget(state, org) if org else 0.0
        return _render(
            request,
            "quality.html",
            {
                "quality": data,
                "view": present_quality(data, spend, budget, calls),
                "org": org,
                "spend_usd": spend,
                "budget_usd": budget,
                "filters": filter_bar,
                "filter_bar": filter_bar,
                "error": error,
                "nav_qs": filter_bar["nav_qs"],
            },
        )

    @app.get("/v1/ui/search", response_class=HTMLResponse)
    def ui_search(request: Request, q: str = "") -> HTMLResponse:
        org = _ui_org(request, state)
        ui_range, values, ranged, _calls, filter_bar, error = _ui_collection(
            request,
            state,
            org,
            action="/v1/ui/search",
            fields=("search", "source", "agent", "hangup"),
            submit_label="Search",
        )
        hits = []
        if q and org:
            if not ui_range.valid:
                error = "start and end are required"
            else:
                structured: dict[str, Any] = {
                    key: value
                    for key, value in values.items()
                    if value and key in {"agent_id", "source"}
                }
                structured["start"] = ui_range.start
                structured["end"] = ui_range.end
                hits = search_calls(
                    ranged,
                    q,
                    index=state.search,
                    org_id=org,
                    filters=structured,
                )
                hits = present_search(hits, ranged, q)
        return _render(
            request,
            "search.html",
            {
                "q": q,
                "results": hits,
                "org": org,
                "filters": filter_bar,
                "filter_bar": filter_bar,
                "error": error,
                "nav_qs": filter_bar["nav_qs"],
            },
        )

    @app.get("/v1/ui/settings", response_class=HTMLResponse)
    def ui_settings(request: Request) -> HTMLResponse:
        org = _ui_require(request, state, "settings.read")
        plugin_rows = present_plugin_rows(state.plugins)
        notice_key = request.query_params.get("notice") or ""
        ui_range = parse_ui_range(request.query_params)
        nav_qs = f"?{ui_range.range_qs}" if ui_range.range_qs else ""
        return _render(
            request,
            "settings.html",
            {
                "org": org,
                "nav_qs": nav_qs,
                "plugins": state.plugins,
                "plugin_rows": plugin_rows,
                "webhook_plugins": [row for row in plugin_rows if row["webhook"]],
                "otlp_plugins": [row for row in plugin_rows if row["otlp"]],
                "connections": present_connection_rows(_org_connections(state, org), state.plugins),
                "rubrics": [r for r in state.rubrics.values() if r.org_id == org],
                "outbound": _org_webhooks(state, org),
                "webhook_events": ("call.finalized", "eval.failed", "flag.raised", "slo.breached"),
                "retention": {
                    "transcripts_days": state.settings.transcript_retention_days,
                    "raw_days": state.settings.raw_retention_days,
                    "aggregates_days": state.settings.aggregate_retention_days,
                },
                "csrf": _ui_csrf(request, state),
                "budget_usd": _ui_eval_budget(state, org),
                "spend_usd": org_spend_usd(state, org),
                "eval_runners": [item.public_dict() for item in _ui_eval_runners(state, org)],
                "eval_policy": _ui_eval_policy(state, org),
                "eval_enable_blocked": _ui_eval_blocked(state, org),
                "eval_pack": _ui_eval_pack(state, org),
                "baseline_sample_rate": _ui_eval_sample(state, org),
                "groundedness_extra": _ui_groundedness_extra(),
                "groundedness_model": state.settings.groundedness_model,
                "environment": state.settings.environment,
                "dlq": present_dlq(_org_dlq(state, org)),
                "notice": SETTING_NOTICES.get(notice_key, ""),
            },
        )

    @app.post("/v1/ui/replay")
    async def ui_replay(request: Request) -> Response:
        org = _ui_require(request, state, "replay")
        form = await request.form()
        _require_csrf(
            request, state, str(form.get("csrf") or request.headers.get("x-csrf-token") or "")
        )
        replay_envelopes(
            state,
            org_id=org,
            provider=str(form.get("provider") or "") or None,
            source_call_id=str(form.get("source_call_id") or "") or None,
        )
        bump_generation(state)
        dest = "/v1/ui/settings?notice=replayed#privacy"
        if str(form.get("next") or "").startswith("/v1/ui"):
            dest = str(form.get("next"))
        return RedirectResponse(dest, status_code=303)

    @app.post("/v1/ui/dlq/purge")
    async def ui_purge_dlq(request: Request) -> Response:
        org = _ui_require(request, state, "replay")
        form = await request.form()
        _require_csrf(
            request, state, str(form.get("csrf") or request.headers.get("x-csrf-token") or "")
        )
        _require_confirm(str(form.get("confirm") or ""), "DELETE")
        state.inbox.purge_dlq(org)
        return RedirectResponse("/v1/ui/settings?notice=dlq_cleared#status", status_code=303)

    @app.post("/v1/ui/calls/{call_id}/analyze")
    async def ui_analyze(request: Request, call_id: str) -> Response:
        org = _ui_require(request, state, "calls.analyze")
        form = await request.form()
        _require_csrf(
            request, state, str(form.get("csrf") or request.headers.get("x-csrf-token") or "")
        )
        await _run_manual_analysis(state, org, call_id)
        return RedirectResponse(f"/v1/ui/calls/{call_id}", status_code=303)

    @app.post("/v1/ui/calls/{call_id}/replay")
    async def ui_call_replay(request: Request, call_id: str) -> Response:
        org = _ui_require(request, state, "replay")
        form = await request.form()
        _require_csrf(
            request, state, str(form.get("csrf") or request.headers.get("x-csrf-token") or "")
        )
        rev = _active(state, org, call_id)
        replay_envelopes(
            state,
            org_id=org,
            provider=rev.source,
            source_call_id=rev.source_call_id or call_id,
        )
        bump_generation(state)
        return RedirectResponse(f"/v1/ui/calls/{call_id}", status_code=303)

    @app.post("/v1/ui/calls/{call_id}/delete")
    async def ui_call_delete(request: Request, call_id: str) -> Response:
        org = _ui_require(request, state, "privacy.delete")
        form = await request.form()
        _require_csrf(
            request, state, str(form.get("csrf") or request.headers.get("x-csrf-token") or "")
        )
        _require_confirm(str(form.get("confirm") or ""), call_id)
        apply_deletion(state, org_id=org, call_id=call_id)
        return RedirectResponse("/v1/ui/settings?notice=deletion_accepted#privacy", status_code=303)

    @app.post("/v1/ui/quality/review")
    async def ui_quality_review(request: Request) -> Response:
        org = _ui_require(request, state, "quality.review")
        form = await request.form()
        _require_csrf(
            request, state, str(form.get("csrf") or request.headers.get("x-csrf-token") or "")
        )
        item = {
            "org_id": org,
            "call_id": str(form.get("call_id") or ""),
            "revision": str(form.get("revision") or ""),
            "analyzer_id": str(form.get("analyzer_id") or ""),
            "severity": str(form.get("severity") or ""),
            "agree": str(form.get("agree") or "") in {"1", "true", "yes", "on"},
            "note": str(form.get("note") or ""),
        }
        if state.review_store is not None:
            state.review_store.insert(item)
        else:
            state.reviews.append(item)
        nxt = str(form.get("next") or "")
        if nxt.startswith("/v1/ui/calls/"):
            return RedirectResponse(nxt, status_code=303)
        dest_params = {
            key: str(form.get(key) or "")
            for key in ("preset", "start", "end", "agent_id", "source", "flag")
            if form.get(key)
        }
        dest = "/v1/ui/quality"
        if dest_params:
            dest = f"/v1/ui/quality?{urlencode(dest_params)}"
        return RedirectResponse(dest, status_code=303)

    @app.post("/v1/ui/connections")
    async def ui_create_connection(request: Request) -> HTMLResponse:
        org = _ui_require(request, state, "connections.write")
        form = await request.form()
        _require_csrf(
            request, state, str(form.get("csrf") or request.headers.get("x-csrf-token") or "")
        )
        provider = str(form.get("provider") or "")
        try:
            loaded = plugin_by_name(provider, state.plugins)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="plugin not installed") from exc
        secrets_map = {
            name: str(form.get(f"secret_{name}") or "")
            for name in loaded.manifest.secret_fields
            if form.get(f"secret_{name}")
        }
        settings_map = {
            key[len("setting_") :]: str(value)
            for key, value in form.items()
            if str(key).startswith("setting_") and value
        }
        created = create_connection(
            state, org_id=org, provider=provider, secrets=secrets_map, settings=settings_map
        )
        base = str(request.base_url).rstrip("/")
        ingest_url = f"{base}/v1/ingest/{provider}/{created['ingest_key']}"
        return _render(
            request,
            "secret_once.html",
            {
                "org": org,
                "view": {
                    "title": "Copy the ingest URL now",
                    "lead": (
                        "The ingest key is shown once. Paste this URL into the provider "
                        "dashboard. Secrets never come back out."
                    ),
                    "rows": [
                        {
                            "label": "Webhook URL",
                            "value": ingest_url,
                            "hint": f"Observational events only for {loaded.display_name}.",
                        },
                        {
                            "label": "ingest_key",
                            "value": created["ingest_key"],
                            "hint": "Part of the URL. Not listed again.",
                        },
                    ],
                    "next_href": "/v1/ui/settings#connections",
                },
            },
        )

    @app.post("/v1/ui/connections/{connection_id}/delete")
    async def ui_delete_connection(request: Request, connection_id: str) -> Response:
        org = _ui_require(request, state, "connections.write")
        form = await request.form()
        _require_csrf(
            request, state, str(form.get("csrf") or request.headers.get("x-csrf-token") or "")
        )
        _require_confirm(str(form.get("confirm") or ""), "DELETE")
        if not state.resolver.delete(org, connection_id):
            raise HTTPException(status_code=404, detail="not found")
        state.connections_plaintext.pop(connection_id, None)
        return RedirectResponse(
            "/v1/ui/settings?notice=connection_deleted#connections", status_code=303
        )

    @app.post("/v1/ui/connections/{connection_id}/backfill")
    async def ui_backfill_connection(request: Request, connection_id: str) -> Response:
        org = _ui_require(request, state, "backfill")
        form = await request.form()
        _require_csrf(
            request, state, str(form.get("csrf") or request.headers.get("x-csrf-token") or "")
        )
        run_backfill(state, org_id=org, connection_id=connection_id)
        return RedirectResponse(
            "/v1/ui/settings?notice=backfill_queued#connections", status_code=303
        )

    @app.post("/v1/ui/rubrics")
    async def ui_create_rubric(request: Request) -> Response:
        org = _ui_require(request, state, "rubrics.write")
        form = await request.form()
        _require_csrf(
            request, state, str(form.get("csrf") or request.headers.get("x-csrf-token") or "")
        )
        kind_raw = str(form.get("kind") or "llm_judge")
        if kind_raw == "predicate":
            from obsalt.analysis.predicates import spec_from_form
            from obsalt.domain.enums import RubricKind

            try:
                spec = spec_from_form(form)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            rubric = Rubric(
                id=new_id(),
                org_id=org,
                name=str(form.get("name") or "predicate"),
                description=str(form.get("description") or "Deterministic check"),
                kind=RubricKind.PREDICATE,
                spec=spec,
            )
        else:
            rubric = Rubric(
                id=new_id(),
                org_id=org,
                name=str(form.get("name") or "rubric"),
                description=str(form.get("description") or ""),
                threshold=float(str(form.get("threshold") or 0.7)),
            )
        if state.rubric_store is not None:
            state.rubric_store.insert(rubric)
        state.rubrics[rubric.id] = rubric
        return RedirectResponse("/v1/ui/settings?notice=rubric_created#rubrics", status_code=303)

    @app.post("/v1/ui/rubrics/{rubric_id}")
    async def ui_update_rubric(request: Request, rubric_id: str) -> Response:
        org = _ui_require(request, state, "rubrics.write")
        form = await request.form()
        _require_csrf(
            request, state, str(form.get("csrf") or request.headers.get("x-csrf-token") or "")
        )
        existing = state.rubrics.get(rubric_id)
        if existing is None or existing.org_id != org:
            raise HTTPException(status_code=404, detail="not found")
        store = state.rubric_store
        name = str(form.get("name") or existing.name)
        if form.get("description") is not None:
            description = str(form.get("description"))
        else:
            description = existing.description
        threshold = float(str(form.get("threshold") or existing.threshold))
        enabled = _as_bool(form.get("enabled"), default=existing.enabled)
        if store is not None:
            updated = store.new_version(
                existing,
                name=name,
                description=description,
                threshold=threshold,
                enabled=enabled,
            )
        else:
            updated = existing.model_copy(
                update={
                    "name": name,
                    "description": description,
                    "version": existing.version + 1,
                    "threshold": threshold,
                    "enabled": enabled,
                }
            )
        state.rubrics[updated.id] = updated
        return RedirectResponse("/v1/ui/settings?notice=rubric_saved#rubrics", status_code=303)

    @app.post("/v1/ui/rubrics/{rubric_id}/delete")
    async def ui_delete_rubric(request: Request, rubric_id: str) -> Response:
        org = _ui_require(request, state, "rubrics.write")
        form = await request.form()
        _require_csrf(
            request, state, str(form.get("csrf") or request.headers.get("x-csrf-token") or "")
        )
        _require_confirm(str(form.get("confirm") or ""), "DELETE")
        existing = state.rubrics.get(rubric_id)
        if existing is None or existing.org_id != org:
            raise HTTPException(status_code=404, detail="not found")
        del state.rubrics[rubric_id]
        if state.rubric_store is not None:
            state.rubric_store.delete(org, rubric_id)
        return RedirectResponse("/v1/ui/settings?notice=rubric_deleted#rubrics", status_code=303)

    @app.post("/v1/ui/eval-runners")
    async def ui_create_eval_runner(request: Request) -> Response:
        org = _ui_require(request, state, "eval_runners.write")
        form = await request.form()
        _require_csrf(
            request, state, str(form.get("csrf") or request.headers.get("x-csrf-token") or "")
        )
        from obsalt.analysis.runners import runner_from_body, store_of
        from obsalt.egress import EgressDenied

        store = store_of(state)
        if store is None:
            raise HTTPException(status_code=503, detail="eval runner store unavailable")
        try:
            runner = runner_from_body(
                org,
                {
                    "slot": str(form.get("slot") or "cheap"),
                    "base_url": str(form.get("base_url") or ""),
                    "model": str(form.get("model") or ""),
                    "api_key": str(form.get("api_key") or ""),
                    "allow_http_localhost": form.get("allow_http_localhost"),
                },
            )
            store.upsert(runner)
        except (ValueError, EgressDenied) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse("/v1/ui/settings?notice=eval_runner_saved#evals", status_code=303)

    @app.post("/v1/ui/eval-runners/{runner_id}/delete")
    async def ui_delete_eval_runner(request: Request, runner_id: str) -> Response:
        org = _ui_require(request, state, "eval_runners.write")
        form = await request.form()
        _require_csrf(
            request, state, str(form.get("csrf") or request.headers.get("x-csrf-token") or "")
        )
        from obsalt.analysis.runners import store_of

        store = store_of(state)
        if store is None or not store.delete(org, runner_id):
            raise HTTPException(status_code=404, detail="not found")
        return RedirectResponse("/v1/ui/settings?notice=eval_runner_deleted#evals", status_code=303)

    @app.post("/v1/ui/eval-runners/{runner_id}/ping")
    async def ui_ping_eval_runner(request: Request, runner_id: str) -> Response:
        org = _ui_require(request, state, "eval_runners.write")
        form = await request.form()
        _require_csrf(
            request, state, str(form.get("csrf") or request.headers.get("x-csrf-token") or "")
        )
        from obsalt.analysis.runners import ping_runner, store_of
        from obsalt.egress import EgressDenied

        store = store_of(state)
        runner = store.get(org, runner_id) if store is not None else None
        if runner is None:
            raise HTTPException(status_code=404, detail="not found")
        try:
            await ping_runner(runner)
        except EgressDenied as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse("/v1/ui/settings?notice=eval_runner_pinged#evals", status_code=303)

    @app.post("/v1/ui/eval-policy")
    async def ui_eval_policy(request: Request) -> Response:
        org = _ui_require(request, state, "eval_runners.write")
        form = await request.form()
        _require_csrf(
            request, state, str(form.get("csrf") or request.headers.get("x-csrf-token") or "")
        )
        from obsalt.analysis.runners import get_policy, list_runners, store_of, validate_policy
        from obsalt.domain.models import EvalPolicy

        store = store_of(state)
        if store is None:
            raise HTTPException(status_code=503, detail="eval runner store unavailable")
        current = get_policy(state, org)
        enabled = str(form.get("llm_evals_enabled") or "") in {"1", "true", "on", "yes"}
        try:
            budget = float(str(form.get("monthly_budget_usd") or current.monthly_budget_usd))
            sample = float(str(form.get("baseline_sample_rate") or current.baseline_sample_rate))
            pack = [str(item) for item in form.getlist("pack")]
            grounded_rate = float(
                str(form.get("groundedness_sample_rate") or current.groundedness_sample_rate)
            )
            grounded_on = str(form.get("groundedness_enabled") or "") in {
                "1",
                "true",
                "on",
                "yes",
            }
            policy = validate_policy(
                EvalPolicy(
                    org_id=org,
                    monthly_budget_usd=budget,
                    baseline_sample_rate=sample,
                    llm_evals_enabled=enabled,
                    pack=pack,
                    groundedness_enabled=grounded_on,
                    groundedness_sample_rate=grounded_rate,
                ),
                list_runners(state, org),
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        store.set_policy(policy)
        return RedirectResponse("/v1/ui/settings?notice=eval_policy_saved#evals", status_code=303)

    @app.post("/v1/ui/keys/rotate")
    async def ui_rotate_key(request: Request) -> HTMLResponse:
        org = _ui_require(request, state, "keys.rotate")
        form = await request.form()
        _require_csrf(
            request, state, str(form.get("csrf") or request.headers.get("x-csrf-token") or "")
        )
        import secrets as secretsmod
        from datetime import timedelta

        from obsalt.util import utcnow

        old = str(form.get("current_key") or "")
        try:
            found_org, scopes = _lookup_key(state, old)
        except HTTPException as exc:
            raise HTTPException(status_code=400, detail="current key is invalid") from exc
        if found_org != org:
            raise HTTPException(status_code=404, detail="not found")
        overlap = int(state.settings.key_rotation_overlap_seconds)
        new_key = secretsmod.token_urlsafe(24)
        if state.key_directory is not None:
            state.key_directory.rotate(org, old, new_key, overlap_seconds=overlap)
        state.keys[new_key] = (org, scopes)
        state.key_roles[new_key] = _role_for_key(state, old, scopes)
        state.key_expiry[old] = utcnow() + timedelta(seconds=overlap)
        return _render(
            request,
            "secret_once.html",
            {
                "org": org,
                "view": {
                    "title": "Copy the new API key now",
                    "lead": (
                        f"Old and new keys both work for {overlap} seconds. "
                        "The plaintext is never stored in the session cookie."
                    ),
                    "rows": [{"label": "API key", "value": new_key, "hint": "Hashed at rest."}],
                    "next_href": "/v1/ui/settings#keys",
                },
            },
        )

    @app.post("/v1/ui/outbound-webhooks")
    async def ui_create_outbound(request: Request) -> HTMLResponse:
        org = _ui_require(request, state, "webhooks.write")
        form = await request.form()
        _require_csrf(
            request, state, str(form.get("csrf") or request.headers.get("x-csrf-token") or "")
        )
        from obsalt.egress import EgressDenied, validate_destination
        from obsalt.webhooks.outbound import mint_whsec

        url = str(form.get("url") or "")
        allow_local = str(form.get("allow_http_localhost") or "") in {"1", "true", "on", "yes"}
        try:
            validate_destination(url, allow_http_localhost=allow_local)
        except EgressDenied as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        secret = mint_whsec()
        dest = {
            "id": new_id(),
            "org_id": org,
            "url": url,
            "secret": secret,
            "event_type": str(form.get("event_type") or "call.finalized"),
            "allow_http_localhost": "true" if allow_local else "false",
        }
        store = getattr(state, "webhook_store", None)
        if store is not None and getattr(store, "durable", False):
            store.create(dest)
        else:
            state.webhook_destinations.append(dest)
        return _render(
            request,
            "secret_once.html",
            {
                "org": org,
                "view": {
                    "title": "Copy the webhook secret now",
                    "lead": "Standard Webhooks signing secret. It is shown once.",
                    "rows": [
                        {"label": "URL", "value": url, "hint": dest["event_type"]},
                        {"label": "whsec", "value": secret, "hint": "Prefix whsec_."},
                    ],
                    "next_href": "/v1/ui/settings#webhooks",
                },
            },
        )

    @app.post("/v1/ui/outbound-webhooks/{dest_id}/rotate")
    async def ui_rotate_outbound(request: Request, dest_id: str) -> HTMLResponse:
        org = _ui_require(request, state, "webhooks.write")
        form = await request.form()
        _require_csrf(
            request, state, str(form.get("csrf") or request.headers.get("x-csrf-token") or "")
        )
        from datetime import timedelta

        from obsalt.util import utcnow
        from obsalt.webhooks.outbound import mint_whsec

        secret = mint_whsec()
        overlap = int(state.settings.key_rotation_overlap_seconds)
        store = getattr(state, "webhook_store", None)
        if store is not None and getattr(store, "durable", False):
            store.rotate_secret(org, dest_id, secret, overlap_seconds=overlap)
        else:
            for dest in state.webhook_destinations:
                if dest.get("id") == dest_id and dest.get("org_id") == org:
                    dest["previous_secret"] = dest.get("secret")
                    dest["previous_secret_expires_at"] = (
                        utcnow() + timedelta(seconds=overlap)
                    ).isoformat()
                    dest["secret"] = secret
                    break
        return _render(
            request,
            "secret_once.html",
            {
                "org": org,
                "view": {
                    "title": "Copy the rotated webhook secret now",
                    "lead": f"Previous secret works for {overlap} seconds.",
                    "rows": [{"label": "whsec", "value": secret, "hint": "Prefix whsec_."}],
                    "next_href": "/v1/ui/settings#webhooks",
                },
            },
        )

    @app.post("/v1/ui/privacy/deletion-requests")
    async def ui_deletion(request: Request) -> Response:
        org = _ui_require(request, state, "privacy.delete")
        form = await request.form()
        _require_csrf(
            request, state, str(form.get("csrf") or request.headers.get("x-csrf-token") or "")
        )
        _require_confirm(str(form.get("confirm") or ""), "DELETE")
        call_id = str(form.get("call_id") or "") or None
        source_call_id = str(form.get("source_call_id") or "") or None
        caller = str(form.get("caller") or "") or None
        start_raw = str(form.get("start") or "") or None
        end_raw = str(form.get("end") or "") or None
        if not call_id and not source_call_id and not caller and not (start_raw and end_raw):
            raise HTTPException(
                status_code=400, detail="call_id, source_call_id, caller, or start/end is required"
            )
        apply_deletion(
            state,
            org_id=org,
            call_id=call_id,
            source_call_id=source_call_id,
            caller=caller,
            start=datetime.fromisoformat(start_raw.replace("Z", "+00:00")) if start_raw else None,
            end=datetime.fromisoformat(end_raw.replace("Z", "+00:00")) if end_raw else None,
        )
        return RedirectResponse("/v1/ui/settings?notice=deletion_accepted#privacy", status_code=303)

    @app.post("/v1/ui/export")
    async def ui_export(request: Request) -> Response:
        org = _ui_require(request, state, "export")
        form = await request.form()
        _require_csrf(
            request, state, str(form.get("csrf") or request.headers.get("x-csrf-token") or "")
        )
        records = [
            {
                "org_id": rev.org_id,
                "call_id": rev.call_id,
                "revision": rev.revision,
                "source": rev.source,
                "agent_id": rev.agent_id,
                "started_at": rev.started_at.isoformat() if rev.started_at else None,
                "timeline_fidelity": rev.timeline_fidelity.value,
                "deleted": False,
            }
            for rev in active_calls(state, org)
        ]
        body = "".join(canonical_json(row) + "\n" for row in records)
        return Response(
            content=body,
            media_type="application/x-ndjson",
            headers={"Content-Disposition": 'attachment; filename="obsalt-calls.jsonl"'},
        )

    @app.post("/v1/ui/seed")
    async def ui_seed(request: Request, background: BackgroundTasks) -> Response:
        org = _ui_require(request, state, "connections.write")
        form = await request.form()
        _require_csrf(
            request, state, str(form.get("csrf") or request.headers.get("x-csrf-token") or "")
        )
        if not environment_allowed(state.settings):
            raise HTTPException(status_code=403, detail="seed refuses production")
        report = ingest_seed_corpus(state, org_id=org, count=1)
        if report.posted:
            background.add_task(drain_inbox, state)
        return RedirectResponse("/v1/ui?notice=seeded", status_code=303)

    return app


def _span_index(state: AppState) -> SpanIdentityIndex:
    existing = getattr(state, "span_identities", None)
    if existing is None:
        existing = SpanIdentityIndex()
        state.span_identities = existing
    return existing


def _require_range(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    if start is None or end is None:
        raise HTTPException(status_code=400, detail="start and end are required")
    return start, end


def _ui_filter_values(request: Request) -> dict[str, str]:
    hangup = request.query_params.get("hangup_reason") or request.query_params.get("outcome") or ""
    return {
        "agent_id": request.query_params.get("agent_id") or "",
        "source": request.query_params.get("source") or "",
        "hangup_reason": hangup,
        "flag": request.query_params.get("flag") or "",
        "q": request.query_params.get("q") or "",
        "eval_result": request.query_params.get("eval_result") or "",
        "latency_ms": request.query_params.get("latency_ms") or "",
    }


def _ui_apply_filters(
    state: AppState,
    org: str | None,
    calls: list[CallRevision],
    values: Mapping[str, str],
    fields: Sequence[str],
) -> list[CallRevision]:
    field_set = frozenset(fields)
    agent_id = values.get("agent_id") or None if "agent" in field_set else None
    source = values.get("source") or None if "source" in field_set else None
    hangup = values.get("hangup_reason") or None if "hangup" in field_set else None
    flag = values.get("flag") or None if "flag" in field_set else None
    eval_result = values.get("eval_result") or None if "eval" in field_set else None
    latency_ms = _optional_float(values.get("latency_ms")) if "latency" in field_set else None
    if not any((agent_id, source, hangup, flag, eval_result, latency_ms is not None)):
        return calls
    filtered: list[CallRevision] = []
    for call in calls:
        analysis: list[Any] = (
            analysis_for(state, org, call.call_id, call.revision)
            if org and (flag or eval_result)
            else []
        )
        if matches_call_filters(
            call,
            agent_id=agent_id,
            source=source,
            outcome=hangup,
            latency_ms=latency_ms,
            flag=flag,
            eval_result=eval_result,
            analysis=analysis,
        ):
            filtered.append(call)
    return filtered


def _ui_collection(
    request: Request,
    state: AppState,
    org: str | None,
    *,
    action: str,
    fields: Sequence[str],
    submit_label: str = "Filter",
    next_cursor: str | None = None,
) -> tuple[Any, dict[str, str], list[CallRevision], list[CallRevision], dict[str, Any], str | None]:
    ui_range = parse_ui_range(request.query_params)
    values = _ui_filter_values(request)
    ranged = _ui_in_range_calls(state, org, ui_range.start, ui_range.end)
    calls = _ui_apply_filters(state, org, ranged, values, fields)
    error = "start and end are required" if org and not ui_range.valid else None
    filter_bar = present_filter_bar(
        action=action,
        ui_range=ui_range,
        values=values,
        fields=fields,
        plugins=state.plugins,
        calls=ranged,
        submit_label=submit_label,
        next_cursor=next_cursor,
    )
    return ui_range, values, ranged, calls, filter_bar, error


def _ui_in_range_calls(
    state: AppState, org: str | None, start: datetime | None, end: datetime | None
) -> list[CallRevision]:
    if not org or start is None or end is None:
        return []
    return [call for call in active_calls(state, org) if in_range(call, start, end)]


def _ui_flags_and_evals(
    analysis: list[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None]:
    flags: list[dict[str, Any]] = []
    evals: list[dict[str, Any]] = []
    card: dict[str, Any] | None = None
    for row in analysis:
        payload = row.payload if hasattr(row, "payload") else {}
        analyzer = row.execution.analyzer_id if hasattr(row, "execution") else ""
        if analyzer == "flags":
            flags.extend(payload.get("flags") or [])
        if analyzer == "hallucination":
            confirmed = [
                {**claim, "pending": False}
                for claim in payload.get("claims") or []
                if isinstance(claim, dict) and _is_confirmed_claim(claim, payload)
            ]
            if confirmed:
                flags.extend(confirmed)
            source = hallucination_claim_list(payload)
            confirmed_spans = {
                (item.get("kind"), item.get("span_text"), item.get("turn_index"))
                for item in confirmed
            }
            for claim in source:
                if not isinstance(claim, dict) or not claim.get("kind"):
                    continue
                if claim.get("verdict") == "grounded":
                    continue
                key = (claim.get("kind"), claim.get("span_text"), claim.get("turn_index"))
                if key in confirmed_spans:
                    continue
                flags.append({**claim, "pending": True})
        if analyzer == "quality_card":
            card = payload if isinstance(payload, dict) else None
        if (
            analyzer in {"eval", "tier2", "rubric"}
            or str(analyzer).startswith("rubric:")
            or str(analyzer).startswith("pack:")
        ):
            evals.append(
                {
                    "analyzer_id": analyzer,
                    "state": row.execution.state.value,
                    "passed": payload.get("passed"),
                    "verdict": payload.get("verdict"),
                    "score": payload.get("score"),
                    "rationale": payload.get("rationale"),
                    "shadow": payload.get("shadow"),
                    "kind": payload.get("kind"),
                }
            )
    return flags, evals, card


def _lookup_key(state: AppState, key: str | None) -> tuple[str, frozenset[KeyScope]]:
    if not key:
        raise HTTPException(status_code=401, detail="API key required")
    found = state.keys.get(key)
    expiry = state.key_expiry.get(key)
    if expiry is not None:
        from obsalt.util import utcnow

        if expiry < utcnow():
            found = None
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


def _authorize(state: AppState, key: str | None, scope: KeyScope, action: str) -> str:
    org, scopes = _lookup_key(state, key)
    if scope not in scopes and KeyScope.ADMIN not in scopes:
        raise HTTPException(status_code=403, detail="insufficient scope")
    if not allowed(_role_for_key(state, key, scopes), action):
        raise HTTPException(status_code=403, detail="insufficient role")
    return org


def _role_for_scopes(scopes: frozenset[KeyScope]) -> Role:
    if KeyScope.ADMIN in scopes:
        return Role.ADMIN
    if KeyScope.ANALYZE in scopes:
        return Role.ANALYST
    return Role.REVIEWER


def _role_for_key(state: AppState, key: str | None, scopes: frozenset[KeyScope]) -> Role:
    """Owner vs admin cannot be told apart from the four key scopes alone (§11.1)."""

    stored: Role | None = getattr(state, "key_roles", {}).get(key) if key else None
    if stored is not None:
        return stored
    if key and key == getattr(state.settings, "bootstrap_api_key", None):
        return Role.OWNER
    return _role_for_scopes(scopes)


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
    principal = _ui_principal(request, state)
    return principal[0] if principal else None


def _ui_principal(request: Request, state: AppState) -> tuple[str, Role] | None:
    cookie = request.cookies.get("obsalt_session")
    info = read_session(cookie, state.settings.session_secret)
    if info:
        return info.org_id, info.role
    api_key = request.headers.get("x-api-key")
    if api_key:
        try:
            org, scopes = _lookup_key(state, api_key)
            if KeyScope.READ not in scopes and KeyScope.ADMIN not in scopes:
                return None
            return org, _role_for_key(state, api_key, scopes)
        except HTTPException:
            return None
    return None


def _ui_require(request: Request, state: AppState, action: str) -> str:
    principal = _ui_principal(request, state)
    if principal is None:
        raise HTTPException(status_code=401, detail="session required")
    org, role = principal
    if not allowed(role, action):
        raise HTTPException(status_code=403, detail="insufficient role")
    return org


def _ui_csrf(request: Request, state: AppState) -> str | None:
    info = read_session(request.cookies.get("obsalt_session"), state.settings.session_secret)
    return info.csrf if info else None


def _require_csrf(request: Request, state: AppState, provided: str | None) -> None:
    if not check_csrf(
        request.cookies.get("obsalt_session"), provided, state.settings.session_secret
    ):
        raise HTTPException(status_code=403, detail="csrf required")


async def _run_manual_analysis(state: AppState, org: str, call_id: str) -> dict[str, Any]:
    """Pack judges plus every org rubric. $0 blocks paid judges."""
    from obsalt.analysis.calibration import is_calibrated
    from obsalt.analysis.hallucination import detect_claims
    from obsalt.analysis.pack import run_pack
    from obsalt.analysis.predicates import run_predicate
    from obsalt.analysis.quality_card import ANALYZER_ID as QUALITY_CARD
    from obsalt.analysis.quality_card import quality_card_result
    from obsalt.analysis.runners import (
        enabled_pack,
        expensive_judge_if_distinct,
        org_eval_budget,
        org_eval_sample_rate,
        resolve_judge,
    )
    from obsalt.analysis.tier2 import budget_for_judge, is_paid_judge, run_tier2

    rev = _active(state, org, call_id)
    spend = org_spend_usd(state, org)
    judge = resolve_judge(state, org)
    cap = budget_for_judge(org_eval_budget(state, org), judge)
    pack_judge = None if is_paid_judge(judge) and spend >= cap else judge
    rubrics = [r for r in state.rubrics.values() if r.org_id == org and r.enabled]
    results: list[Any] = []
    writer = state.sink.write_analysis
    existing = analysis_for(state, org, call_id, rev.revision)
    claims = detect_claims(rev)
    pack_rows = await run_pack(
        rev,
        names=enabled_pack(state, org),
        judge=pack_judge,
        expensive_judge=expensive_judge_if_distinct(state, org),
        selection="manual",
    )
    results.extend(pack_rows)
    for item in pack_rows:
        cost = float((item.payload or {}).get("cost_usd") or 0.0)
        if cost:
            add_org_spend(state, org, cost)
    for rubric in rubrics:
        if rubric.is_predicate():
            results.append(run_predicate(rev, rubric, selection="manual"))
            continue
        spend = org_spend_usd(state, org)
        result = await run_tier2(
            rev,
            rubric=rubric,
            manual=True,
            baseline_sample_rate=org_eval_sample_rate(state, org),
            budget_usd=cap,
            spend_usd=spend,
            judge=judge,
            analyzer_id=f"rubric:{rubric.id}",
            calibrated=is_calibrated(
                getattr(state, "calibration", None), org, rubric.id, rubric.version
            ),
        )
        cost = float((result.payload or {}).get("cost_usd") or 0.0)
        if cost:
            add_org_spend(state, org, cost)
        results.append(result)
    replacing = {item.execution.analyzer_id for item in results} | {QUALITY_CARD}
    kept = [
        row
        for row in existing
        if getattr(getattr(row, "execution", None), "analyzer_id", "") not in replacing
    ]
    card = quality_card_result(rev, claims=claims)
    if writer is not None:
        writer(org, call_id, rev.revision, kept + results + [card])
    return {"items": [item.model_dump(mode="json") for item in results]}


def _active(state: AppState, org: str, call_id: str) -> CallRevision:
    rev_id = state.pointers.get(org, call_id)
    if not rev_id:
        raise HTTPException(status_code=404, detail="not found")
    rev = state.sink.get(org, call_id, rev_id)
    if rev is None or rev.org_id != org:
        raise HTTPException(status_code=404, detail="not found")
    return rev


def _render(request: Request, name: str, context: dict[str, Any]) -> HTMLResponse:
    path = TEMPLATES_DIR / name
    template = name if path.exists() else "call_list.html"
    state: AppState = request.app.state.obsalt
    principal = _ui_principal(request, state)
    role = principal[1] if principal else None
    context.setdefault("csrf", _ui_csrf(request, state))
    context.setdefault("raw_retention_days", state.settings.raw_retention_days)
    context.setdefault("nav_qs", "")
    if principal:
        context.setdefault("org", principal[0])
        context.setdefault("role", principal[1].value)
        context.setdefault("role_label", role_label(principal[1]))
    else:
        context.setdefault("org", None)
        context.setdefault("role", "")
        context.setdefault("role_label", "")
    context.setdefault("can", present_can(role))
    ready = collect_health(state)
    context.setdefault(
        "health",
        present_health(
            plugins=[plugin.name for plugin in state.plugins],
            insecure_defaults=state.settings.insecure_defaults(),
            inbox_age_seconds=float(ready.get("inbox_age_seconds") or 0),
            outbox_depth=int(ready.get("outbox_depth") or 0),
            dlq_depth=int(ready.get("dlq_depth") or 0),
            orphan_blobs=int(ready.get("orphan_blobs") or 0),
            deletion_backlog=int(ready.get("deletion_backlog") or 0),
            environment=state.settings.environment,
        ),
    )
    return templates.TemplateResponse(request, template, context)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _optional_float(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _require_confirm(provided: str, expected: str) -> None:
    if provided != expected:
        raise HTTPException(status_code=400, detail="confirmation required")


def _ui_role(request: Request, state: AppState) -> Role:
    principal = _ui_principal(request, state)
    return principal[1] if principal else Role.REVIEWER


def _ui_eval_runners(state: AppState, org: str) -> list[Any]:
    from obsalt.analysis.runners import list_runners

    return list_runners(state, org)


def _ui_eval_policy(state: AppState, org: str) -> dict[str, Any]:
    from obsalt.analysis.runners import get_policy

    return get_policy(state, org).model_dump(mode="json")


def _ui_groundedness_extra() -> bool:
    from obsalt.analysis.groundedness import groundedness_available

    return groundedness_available()


def _ui_groundedness_payload(analysis: list[Any]) -> dict[str, Any] | None:
    for row in analysis:
        execution = getattr(row, "execution", None)
        if getattr(execution, "analyzer_id", "") == "groundedness":
            payload = getattr(row, "payload", None)
            return payload if isinstance(payload, dict) else None
    return None


def _policy_flag(raw: Any, current: bool) -> bool:
    if raw is None:
        return current
    if isinstance(raw, str):
        return raw.lower() in {"1", "true", "on", "yes"}
    return bool(raw)


def _ui_eval_budget(state: AppState, org: str) -> float:
    from obsalt.analysis.runners import org_eval_budget

    return org_eval_budget(state, org)


def _ui_eval_sample(state: AppState, org: str) -> float:
    from obsalt.analysis.runners import org_eval_sample_rate

    return org_eval_sample_rate(state, org)


def _ui_eval_blocked(state: AppState, org: str) -> str | None:
    from obsalt.analysis.runners import enable_blocked_reason

    return enable_blocked_reason(state, org)


def _rubric_kind_spec(
    body: Mapping[str, Any], *, existing: Rubric | None = None
) -> tuple[Any, dict[str, Any]]:
    from obsalt.analysis.predicates import parse_spec
    from obsalt.domain.enums import RubricKind

    raw_kind = str(
        body.get("kind") or (existing.kind.value if existing else RubricKind.LLM_JUDGE.value)
    )
    try:
        kind = RubricKind(raw_kind)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="kind must be llm_judge or predicate") from exc
    spec = body.get("spec")
    if spec is None:
        spec = dict(existing.spec) if existing else {}
    if not isinstance(spec, dict):
        raise HTTPException(status_code=400, detail="spec must be an object")
    if kind is RubricKind.PREDICATE:
        try:
            parse_spec(spec)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return kind, spec


def _ui_eval_pack(state: AppState, org: str) -> list[dict[str, Any]]:
    from obsalt.analysis.pack import catalog_rows
    from obsalt.analysis.runners import enabled_pack

    return catalog_rows(enabled_pack(state, org))


def _org_connections(state: AppState, org: str) -> list[Any]:
    return list(state.resolver.list_for_org(org))


def _org_has_calls(state: AppState, org: str) -> bool:
    return bool(state.pointers.list_org(org))


def _org_webhooks(state: AppState, org: str) -> list[dict[str, Any]]:
    dests = (
        state.webhook_store.list_destinations(org)
        if state.webhook_store is not None
        else state.webhook_destinations
    )
    items = []
    for dest in dests:
        if dest.get("org_id") and dest.get("org_id") != org:
            continue
        items.append(
            {"id": dest.get("id"), "url": dest.get("url"), "event_type": dest.get("event_type")}
        )
    return items


def _org_reviews(state: AppState, org: str) -> list[dict[str, Any]]:
    if state.review_store is not None:
        return list(state.review_store.list(org))
    return [item for item in state.reviews if item.get("org_id") == org]


def _org_dlq(state: AppState, org: str) -> list[dict[str, Any]]:
    rows = list(state.inbox.list_dlq())
    return [row for row in rows if not row.get("org_id") or row.get("org_id") == org]


def issue_session_cookie(org_id: str, secret: str) -> str:
    return sign_session(org_id, secret)
