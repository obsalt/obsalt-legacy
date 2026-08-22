from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from obsalt.domain.enums import EnvelopeState, ObservationalEventKind, VerifyOutcome
from obsalt.util import utcnow


class VerifyResult(BaseModel):
    outcome: VerifyOutcome
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is VerifyOutcome.OK


class TombstoneHints(BaseModel):
    source_call_id: str | None = None
    caller_token: str | None = None
    event_time: datetime | None = None


class WebhookResponse(BaseModel):
    status_code: int = 200
    body: bytes = b'{"ok":true}'
    headers: dict[str, str] = Field(default_factory=dict)
    media_type: str = "application/json"


class ConnectionConfig(BaseModel):
    """Per-tenant provider connection. Secrets are decrypted just-in-time."""

    model_config = ConfigDict(extra="allow")

    org_id: str
    provider: str
    connection_id: str
    ingest_key_hash: str
    secrets: dict[str, str] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)


class RawEnvelope(BaseModel):
    envelope_id: str
    org_id: str
    provider: str
    connection_id: str
    object_key: str
    delivery_key: str
    content_sha256: str
    state: EnvelopeState = EnvelopeState.RECEIVED
    event_kind: ObservationalEventKind | None = None
    source_call_id: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    received_at: datetime = Field(default_factory=utcnow)
    body: bytes | None = None  # present only when loaded from object storage


class PluginManifest(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    config_schema: dict[str, Any] = Field(default_factory=dict)
    secret_fields: frozenset[str] = Field(default_factory=frozenset)
    trust: str = "operator_installed"


class BackfillCursor(BaseModel):
    token: str | None = None
    started_after: datetime | None = None
    started_before: datetime | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class BackfillItem(BaseModel):
    upstream_entity_id: str
    content_hash: str | None = None
    upstream_revision: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class BackfillPage(BaseModel):
    items: list[BackfillItem] = Field(default_factory=list)
    next_cursor: BackfillCursor | None = None
    truncated_by_retention: bool = False


class SdkConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    otlp_endpoint: str | None = None
    service_name: str = "voice-agent"


class InstrumentedClient(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    client: object
    notes: str = ""


class JudgeRequest(BaseModel):
    rubric_id: str
    rubric_version: int
    rubric_text: str
    transcript: str
    grounding: list[str] = Field(default_factory=list)
    model: str | None = None


class JudgeResult(BaseModel):
    score: float
    passed: bool
    rationale: str
    quotes: list[str] = Field(default_factory=list)
    model: str | None = None
    prompt_version: str = "1"


class RedactedDocument(BaseModel):
    id: str
    text: str


class Vector(BaseModel):
    id: str
    values: list[float]


class RedactionPolicy(BaseModel):
    version: str = "1"
    redact_emails: bool = True
    redact_phones: bool = True
    redact_cards: bool = True
    extra_patterns: list[str] = Field(default_factory=list)


class RedactionResult(BaseModel):
    events: list[Any]
    policy_version: str
    redacted_fields: list[str] = Field(default_factory=list)


class ReadableSpan(BaseModel):
    """Minimal span view used by OTLP mappers. Not a general span store."""

    model_config = ConfigDict(extra="allow")

    name: str
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    start_unix_nano: int
    end_unix_nano: int
    attributes: dict[str, Any] = Field(default_factory=dict)
    resource: dict[str, Any] = Field(default_factory=dict)
    status_code: str = "UNSET"
    events: list[dict[str, Any]] = Field(default_factory=list)
