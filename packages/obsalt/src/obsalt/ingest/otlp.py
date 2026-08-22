"""OTLP/HTTP protobuf and proto3-JSON receiver. Not a general span store."""

from __future__ import annotations

import gzip
import json
import zlib
from typing import Any

from fastapi import Request
from fastapi.responses import Response

from obsalt.domain.identity import content_hash, sha256_bytes

MAX_BODY = 8 * 1024 * 1024


async def handle_otlp_http(request: Request, runtime: Any) -> Response:
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type not in {"application/x-protobuf", "application/json"}:
        return Response(status_code=415, content=b"unsupported content type")
    raw = await request.body()
    encoding = (request.headers.get("content-encoding") or "").lower()
    if len(raw) > MAX_BODY:
        return Response(status_code=503, content=_partial(rejected=True))
    try:
        body = _inflate(raw, encoding)
    except Exception:
        return Response(status_code=400, content=b"invalid compression")
    if len(body) > MAX_BODY:
        return Response(status_code=503, content=_partial(rejected=True))

    org = _org_from_request(request, runtime)
    if org is None:
        return Response(status_code=401, content=b"unauthenticated")

    if content_type == "application/json":
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return Response(status_code=400, content=b"malformed json")
        spans = _spans_from_json(payload)
    else:
        try:
            spans = _spans_from_protobuf(body)
        except Exception:
            return Response(status_code=400, content=b"malformed protobuf")

    if _mixed_org(spans, org):
        return Response(status_code=400, content=b"mixed-org batch rejected")

    key = f"raw/{org}/otlp/{sha256_bytes(body)[:32]}"
    runtime.objects.put(key, body, headers={"content-type": content_type})
    # Durable forwarding identity is (org, trace, span, content_fingerprint)
    _ = content_hash
    return Response(
        status_code=200,
        content=_partial(rejected=False),
        media_type="application/x-protobuf" if content_type.endswith("protobuf") else "application/json",
    )


def _inflate(raw: bytes, encoding: str) -> bytes:
    if encoding == "gzip":
        return gzip.decompress(raw)
    if encoding == "deflate":
        return zlib.decompress(raw)
    return raw


def _org_from_request(request: Request, runtime: Any) -> str | None:
    token = request.headers.get("x-api-key") or request.headers.get("authorization", "").removeprefix("Bearer ")
    if not token:
        return None
    principal = runtime.authenticate_api_key(token)
    return principal.org_id if principal else None


def _spans_from_json(payload: dict[str, Any]) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for resource in payload.get("resourceSpans") or payload.get("resource_spans") or []:
        resource_attrs = _attrs(resource.get("resource", {}).get("attributes") or [])
        for scope in resource.get("scopeSpans") or resource.get("scope_spans") or []:
            for span in scope.get("spans") or []:
                row = dict(span)
                row["_resource"] = resource_attrs
                spans.append(row)
    return spans


def _spans_from_protobuf(body: bytes) -> list[dict[str, Any]]:
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    req = ExportTraceServiceRequest()
    req.ParseFromString(body)
    spans: list[dict[str, Any]] = []
    for resource in req.resource_spans:
        resource_attrs = {kv.key: _proto_value(kv.value) for kv in resource.resource.attributes}
        for scope in resource.scope_spans:
            for span in scope.spans:
                spans.append(
                    {
                        "traceId": span.trace_id.hex(),
                        "spanId": span.span_id.hex(),
                        "parentSpanId": span.parent_span_id.hex() if span.parent_span_id else "",
                        "name": span.name,
                        "attributes": {kv.key: _proto_value(kv.value) for kv in span.attributes},
                        "_resource": resource_attrs,
                    }
                )
    return spans


def _proto_value(value: Any) -> Any:
    kind = value.WhichOneof("value")
    if kind is None:
        return None
    return getattr(value, kind)


def _attrs(items: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in items:
        key = item.get("key")
        val = item.get("value") or {}
        if not key:
            continue
        if "stringValue" in val or "string_value" in val:
            out[key] = val.get("stringValue") or val.get("string_value")
        else:
            out[key] = val
    return out


def _mixed_org(spans: list[dict[str, Any]], org: str) -> bool:
    for span in spans:
        resource = span.get("_resource") or {}
        claimed = resource.get("obsalt.org") or resource.get("service.namespace")
        if claimed and claimed != org:
            return True
    return False


def _partial(*, rejected: bool) -> bytes:
    if rejected:
        return b""
    try:
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse

        return ExportTraceServiceResponse().SerializeToString()
    except Exception:
        return b"{}"
