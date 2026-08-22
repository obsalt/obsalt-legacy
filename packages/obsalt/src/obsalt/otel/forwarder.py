"""Identity-preserving OTLP forwarder.

POST the original raw protobuf/JSON bytes to a destination. Do not rewrite
trace_id, span_id, parents, links, resources, or measured timestamps. The
payload is never reconstructed from the Call aggregate.

Per-destination ``emit_pii``:
    Raw forward is identity-preserving of the inbound batch. If ``emit_pii`` is
    false we do **not** attach a second copy of PII attributes, and we do **not**
    mutate the original bytes to strip them. PII stripping happens on *our*
    export of derived metrics (``obsalt.pii.*`` on spans we emit), not by
    rewriting a foreign OTLP batch. Raw forward = original bytes.

On transient failure the forwarder returns ``retryable=True`` so the caller can
503 the ingest batch. Destination validation uses ``obsalt.egress.validate_destination``
(HTTPS only; ``allow_http_localhost`` for tests).
"""

from __future__ import annotations

from enum import StrEnum

import httpx
from pydantic import BaseModel, Field

from obsalt.egress import EgressDenied, validate_destination

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 507, 509, 529})


class ForwardOutcome(StrEnum):
    OK = "ok"
    RETRYABLE = "retryable"
    PERMANENT = "permanent"


class ForwardResult(BaseModel):
    outcome: ForwardOutcome
    status_code: int | None = None
    detail: str | None = None
    identity_preserved: bool = True

    @property
    def ok(self) -> bool:
        return self.outcome is ForwardOutcome.OK

    @property
    def retryable(self) -> bool:
        return self.outcome is ForwardOutcome.RETRYABLE

    @property
    def permanent(self) -> bool:
        return self.outcome is ForwardOutcome.PERMANENT


class ForwardDestination(BaseModel):
    """Per-destination policy. ``emit_pii`` applies to derived-metric export, not raw forward."""

    url: str
    emit_pii: bool = False
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = 10.0
    allow_http_localhost: bool = False


def forward_otlp_batch(
    destination_url: str,
    raw: bytes,
    *,
    content_type: str,
    emit_pii: bool = False,
    allow_http_localhost: bool = False,
    extra_headers: dict[str, str] | None = None,
    timeout: float = 10.0,
    client: httpx.Client | None = None,
    content_encoding: str | None = None,
) -> ForwardResult:
    """POST ``raw`` unchanged. ``emit_pii`` is not used to rewrite the batch."""

    try:
        validate_destination(destination_url, allow_http_localhost=allow_http_localhost)
    except EgressDenied as exc:
        return ForwardResult(
            outcome=ForwardOutcome.PERMANENT, detail=str(exc), identity_preserved=True
        )

    headers = {"Content-Type": content_type}
    if content_encoding:
        headers["Content-Encoding"] = content_encoding
    if extra_headers:
        headers.update(extra_headers)

    # Raw forward is identity-preserving. emit_pii applies to derived-metric export only.
    _ = emit_pii
    payload = raw

    own_client = client is None
    http = client or httpx.Client(timeout=timeout, follow_redirects=False)
    try:
        response = http.post(destination_url, content=payload, headers=headers, timeout=timeout)
    except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
        return ForwardResult(
            outcome=ForwardOutcome.RETRYABLE, detail=str(exc), identity_preserved=True
        )
    except httpx.HTTPError as exc:
        return ForwardResult(
            outcome=ForwardOutcome.RETRYABLE, detail=str(exc), identity_preserved=True
        )
    finally:
        if own_client:
            http.close()

    if 200 <= response.status_code < 300:
        return ForwardResult(
            outcome=ForwardOutcome.OK,
            status_code=response.status_code,
            identity_preserved=True,
        )
    if response.status_code in RETRYABLE_STATUS:
        return ForwardResult(
            outcome=ForwardOutcome.RETRYABLE,
            status_code=response.status_code,
            detail=response.text[:512] if response.text else None,
            identity_preserved=True,
        )
    return ForwardResult(
        outcome=ForwardOutcome.PERMANENT,
        status_code=response.status_code,
        detail=response.text[:512] if response.text else None,
        identity_preserved=True,
    )


def forward_to_destination(
    destination: ForwardDestination,
    raw: bytes,
    *,
    content_type: str,
    client: httpx.Client | None = None,
    content_encoding: str | None = None,
) -> ForwardResult:
    return forward_otlp_batch(
        destination.url,
        raw,
        content_type=content_type,
        emit_pii=destination.emit_pii,
        allow_http_localhost=destination.allow_http_localhost,
        extra_headers=destination.headers or None,
        timeout=destination.timeout_seconds,
        client=client,
        content_encoding=content_encoding,
    )
