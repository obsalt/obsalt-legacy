"""OTLP/gRPC on a separate grpc.aio server in the same process, opt-in by config (§6.2)."""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("obsalt.otel.grpc")


async def serve_otlp_grpc(state: Any, *, port: int) -> Any:
    """Start the opt-in gRPC server. Requires the ``obsalt[grpc]`` extra."""

    try:
        import grpc
        from grpc import aio
        from opentelemetry.proto.collector.trace.v1 import trace_service_pb2, trace_service_pb2_grpc
    except ImportError as exc:  # pragma: no cover - extra not installed in unit tests
        raise RuntimeError("OTLP gRPC requires grpcio and opentelemetry proto stubs") from exc

    from obsalt.ingest.otlp import receive_otlp_batch
    from obsalt.ingest.receive import ReceiveLimits
    from obsalt.otel.receiver import request_to_spans

    class _Servicer(trace_service_pb2_grpc.TraceServiceServicer):  # type: ignore[misc]
        async def Export(self, request: Any, context: Any) -> Any:  # noqa: N802
            raw = request.SerializeToString()
            spans = request_to_spans(request)
            org = _org_from_metadata(context, state)
            if org is None:
                await context.abort(grpc.StatusCode.UNAUTHENTICATED, "ingest key required")
                return trace_service_pb2.ExportTraceServiceResponse()
            from obsalt.otel.tenancy import reject_tenant_assertions

            denied = reject_tenant_assertions(spans, org)
            if denied:
                await context.abort(grpc.StatusCode.PERMISSION_DENIED, denied)
            result = receive_otlp_batch(
                org_id=org,
                raw=raw,
                content_type="application/x-protobuf",
                objects=state.objects,
                inbox=state.inbox,
                limits=ReceiveLimits(
                    compressed_bytes=state.settings.compressed_body_limit,
                    expanded_bytes=state.settings.expanded_body_limit,
                ),
                spans=spans,
                span_index=getattr(state, "span_identities", None),
                leases=getattr(state, "leases", None),
                backpressure_limit=getattr(state.settings, "outbox_backpressure_limit", 10_000),
            )
            if result.status_code == 503:
                await context.abort(grpc.StatusCode.UNAVAILABLE, result.rejected or "ingest capacity")
            if result.status_code == 413:
                await context.abort(grpc.StatusCode.UNAVAILABLE, result.rejected or "too large")
            if result.status_code == 409:
                await context.abort(grpc.StatusCode.FAILED_PRECONDITION, result.rejected or "conflict")
            return trace_service_pb2.ExportTraceServiceResponse()

    server = aio.server()
    trace_service_pb2_grpc.add_TraceServiceServicer_to_server(_Servicer(), server)
    server.add_insecure_port(f"[::]:{port}")
    await server.start()
    log.info("OTLP gRPC listening on %s", port)
    return server


def _org_from_metadata(context: Any, state: Any) -> str | None:
    metadata = dict(context.invocation_metadata() or [])
    key = metadata.get("x-api-key") or metadata.get("authorization")
    if key and str(key).lower().startswith("bearer "):
        key = str(key)[7:]
    if not key:
        return None
    found = state.keys.get(str(key))
    if found is None and getattr(state, "key_directory", None) is not None:
        record = state.key_directory.lookup(str(key))
        if record is not None:
            return record.org_id
    if found is None:
        return None
    return found[0]
