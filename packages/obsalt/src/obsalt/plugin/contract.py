"""Versioned public plugin contract. First-party providers use this same path."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Sequence
from typing import ClassVar, Protocol, runtime_checkable

from obsalt._version import PLUGIN_API_VERSION
from obsalt.domain.enums import Capability
from obsalt.domain.events import NormalizedEvent
from obsalt.domain.models import FidelityDeclaration
from obsalt.plugin.types import (
    BackfillCursor,
    BackfillItem,
    BackfillPage,
    ConnectionConfig,
    InstrumentedClient,
    JudgeRequest,
    JudgeResult,
    PluginManifest,
    RawEnvelope,
    ReadableSpan,
    RedactedDocument,
    RedactionPolicy,
    RedactionResult,
    SdkConfig,
    TombstoneHints,
    Vector,
    VerifyResult,
    WebhookResponse,
)

RawHeaderList = list[tuple[bytes, bytes]]


@runtime_checkable
class WebhookSource(Protocol):
    singleton_headers: ClassVar[frozenset[bytes]]

    def authenticate(self, raw: bytes, headers: RawHeaderList, cfg: ConnectionConfig) -> VerifyResult: ...

    def classify(self, raw: bytes) -> ObservationalKindLike: ...

    def delivery_key(self, raw: bytes, headers: RawHeaderList) -> str | None: ...

    def tombstone_hints(self, raw: bytes) -> TombstoneHints: ...

    def acknowledgement(self, kind: ObservationalKindLike) -> WebhookResponse: ...

    def decode(self, envelope: RawEnvelope) -> Iterable[NormalizedEvent]: ...


@runtime_checkable
class OtlpMapper(Protocol):
    def claims(self, span: ReadableSpan) -> int: ...

    def decode(self, spans: Sequence[ReadableSpan]) -> Iterable[NormalizedEvent]: ...


@runtime_checkable
class RestBackfill(Protocol):
    def scan(self, cfg: ConnectionConfig, cursor: BackfillCursor) -> BackfillPage: ...

    def hydrate(self, cfg: ConnectionConfig, item: BackfillItem) -> RawEnvelope: ...


@runtime_checkable
class StreamSource(Protocol):
    async def frames(self, cfg: ConnectionConfig) -> AsyncIterator[RawEnvelope]: ...


@runtime_checkable
class SdkInstrumentation(Protocol):
    def instrument(self, client: object, cfg: SdkConfig) -> InstrumentedClient: ...


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
    fidelity: ClassVar[FidelityDeclaration]


ObservationalKindLike = object  # classified by plugins; core maps to ObservationalEventKind

__all__ = [
    "PLUGIN_API_VERSION",
    "Embedder",
    "Judge",
    "ObsaltPlugin",
    "OtlpMapper",
    "RawHeaderList",
    "Redactor",
    "RestBackfill",
    "SdkInstrumentation",
    "StreamSource",
    "WebhookSource",
]
