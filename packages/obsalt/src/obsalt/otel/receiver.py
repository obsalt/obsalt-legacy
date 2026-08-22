"""OTLP/HTTP protobuf and proto3-JSON receiver. obsalt is not a general span store."""

from __future__ import annotations

import gzip
import zlib
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, HTTPException
from google.protobuf.json_format import Parse
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

from obsalt.plugin.types import ReadableSpan

router = APIRouter()
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="otlp")

MAX_SPANS = 10_000
MAX_ATTRS = 128
MAX_STRING = 8_192


def parse_otlp_request(
    content_type: str,
    raw: bytes,
    encoding: str | None,
    expanded_bytes: int | None = None,
) -> ExportTraceServiceRequest:
    body = _decompress(raw, encoding)
    if expanded_bytes is not None and len(body) > expanded_bytes:
        raise HTTPException(status_code=413, detail="expanded body exceeds limit")
    req = ExportTraceServiceRequest()
    if content_type == "application/x-protobuf":
        try:
            req.ParseFromString(body)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="malformed protobuf") from exc
        return req
    if content_type in {"application/json", "application/json; charset=utf-8"}:
        try:
            Parse(body.decode("utf-8"), req)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="malformed otlp json") from exc
        return req
    raise HTTPException(status_code=415, detail="Content-Type must be application/x-protobuf or application/json")


def request_to_spans(req: ExportTraceServiceRequest) -> list[ReadableSpan]:
    spans: list[ReadableSpan] = []
    for resource_span in req.resource_spans:
        resource = {kv.key: _any_value(kv.value) for kv in resource_span.resource.attributes}
        for scope in resource_span.scope_spans:
            for span in scope.spans:
                parent = span.parent_span_id.hex() if span.parent_span_id else None
                spans.append(
                    ReadableSpan(
                        name=span.name,
                        trace_id=span.trace_id.hex(),
                        span_id=span.span_id.hex(),
                        parent_span_id=parent,
                        start_unix_nano=span.start_time_unix_nano,
                        end_unix_nano=span.end_time_unix_nano,
                        attributes={kv.key: _any_value(kv.value) for kv in span.attributes},
                        resource=resource,
                    )
                )
                if len(spans) > MAX_SPANS:
                    raise HTTPException(status_code=400, detail="span count exceeds limit")
    return spans


def serialized_success() -> bytes:
    return ExportTraceServiceResponse().SerializeToString()


def decompress_body(raw: bytes, encoding: str | None) -> bytes:
    return _decompress(raw, encoding)


def _decompress(raw: bytes, encoding: str | None) -> bytes:
    if not encoding or encoding == "identity":
        return raw
    if encoding == "gzip":
        return gzip.decompress(raw)
    if encoding == "deflate":
        return zlib.decompress(raw)
    raise HTTPException(status_code=415, detail=f"unsupported content-encoding {encoding}")


def _any_value(value) -> object:
    kind = value.WhichOneof("value")
    if kind == "string_value":
        text = value.string_value
        if len(text) > MAX_STRING:
            return text[:MAX_STRING]
        return text
    if kind == "int_value":
        return value.int_value
    if kind == "double_value":
        return value.double_value
    if kind == "bool_value":
        return value.bool_value
    return None
