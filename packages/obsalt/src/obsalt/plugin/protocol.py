"""Public plugin contract. First-party providers use this same API."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Sequence
from datetime import date
from typing import Any, ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from obsalt.domain.enums import (
    Capability,
    MeasurementPlacement,
    ObservationalEventKind,
    PipelineArchitecture,
    Signal,
    VerifyOutcome,
)
from obsalt.domain.events import NormalizedEvent


class PluginManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config_schema: dict[str, Any] = Field(default_factory=dict)
    secret_fields: frozenset[str] = Field(default_factory=frozenset)
    documentation_url: str | None = None


class FidelityDeclaration(BaseModel):
    """What a plugin *can* produce. Per-call coverage is derived from decode output."""

    model_config = ConfigDict(extra="forbid")

    source_format: str
    possible_architectures: frozenset[PipelineArchitecture]
    possible_placements: frozenset[MeasurementPlacement]
    provides: frozenset[Signal]
    structurally_absent: dict[Signal, str] = Field(default_factory=dict)
    schema_source: str
    schema_revision: str
    verified_at: date


class ConnectionConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    org_id: str
    provider: str
    connection_id: str
    credentials: dict[str, str] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)


class VerifyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: VerifyOutcome
    detail: str | None = None
    event_time_ms: int | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is VerifyOutcome.OK


class TombstoneHints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_call_id: str | None = None
    caller_token: str | None = None
    event_time: str | None = None


class WebhookResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status_code: int = 200
    body: bytes = b""
    headers: dict[str, str] = Field(default_factory=dict)
    media_type: str = "application/json"


class RawEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    envelope_id: str
    org_id: str
    provider: str
    connection_id: str
    object_key: str
    body: bytes | None = None
    headers: list[tuple[str, str]] = Field(default_factory=list)
    delivery_key: str
    received_at: str
    event_kind: ObservationalEventKind | None = None
    decoder_version: str | None = None


class BackfillCursor(BaseModel):
    model_config = ConfigDict(extra="allow")

    token: str | None = None
    started_after: str | None = None
    started_before: str | None = None


class BackfillItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    upstream_entity_id: str
    content_hash: str | None = None
    revision: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class BackfillPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[BackfillItem] = Field(default_factory=list)
    next_cursor: BackfillCursor | None = None
    truncated_by_retention: bool = False


class JudgeRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    rubric: str
    transcript: str
    grounding: list[str] = Field(default_factory=list)


class JudgeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: float
    passed: bool
    rationale: str
    quotes: list[str] = Field(default_factory=list)
    model: str | None = None
    prompt_version: str | None = None


class RedactedDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    text: str


class Vector(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    values: list[float]


class RedactionPolicy(BaseModel):
    model_config = ConfigDict(extra="allow")

    version: str = "default/1"
    redact_phone: bool = True
    redact_email: bool = True
    redact_card: bool = True


class RedactionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[NormalizedEvent]
    policy_version: str
    redacted_fields: list[str] = Field(default_factory=list)


class SdkConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    org_id: str | None = None
    agent_id: str | None = None


RawHeaderList = list[tuple[bytes, bytes]]


@runtime_checkable
class WebhookSource(Protocol):
    singleton_headers: ClassVar[frozenset[bytes]]

    def authenticate(
        self, raw: bytes, headers: RawHeaderList, cfg: ConnectionConfig
    ) -> VerifyResult: ...

    def classify(self, raw: bytes) -> ObservationalEventKind: ...

    def delivery_key(self, raw: bytes, headers: RawHeaderList) -> str | None: ...

    def tombstone_hints(self, raw: bytes) -> TombstoneHints: ...

    def acknowledgement(self, kind: ObservationalEventKind) -> WebhookResponse: ...

    def decode(self, envelope: RawEnvelope) -> Iterable[NormalizedEvent]: ...


@runtime_checkable
class OtlpMapper(Protocol):
    def claims(self, span: Any) -> int: ...

    def decode(self, spans: Sequence[Any]) -> Iterable[NormalizedEvent]: ...


@runtime_checkable
class RestBackfill(Protocol):
    def scan(self, cfg: ConnectionConfig, cursor: BackfillCursor) -> BackfillPage: ...

    def hydrate(self, cfg: ConnectionConfig, item: BackfillItem) -> RawEnvelope: ...


@runtime_checkable
class StreamSource(Protocol):
    async def frames(self, cfg: ConnectionConfig) -> AsyncIterator[RawEnvelope]: ...


@runtime_checkable
class SdkInstrumentation(Protocol):
    def instrument(self, client: object, cfg: SdkConfig) -> object: ...


@runtime_checkable
class Judge(Protocol):
    async def judge(self, request: JudgeRequest) -> JudgeResult: ...


@runtime_checkable
class Embedder(Protocol):
    async def embed(self, documents: Sequence[RedactedDocument]) -> Sequence[Vector]: ...


@runtime_checkable
class Redactor(Protocol):
    def redact(self, events: Sequence[NormalizedEvent], policy: RedactionPolicy) -> RedactionResult: ...


@runtime_checkable
class ObsaltPlugin(Protocol):
    API_VERSION: ClassVar[int]
    name: ClassVar[str]
    display_name: ClassVar[str]
    capabilities: ClassVar[frozenset[Capability]]
    manifest: ClassVar[PluginManifest]
    fidelity: ClassVar[FidelityDeclaration | None]
