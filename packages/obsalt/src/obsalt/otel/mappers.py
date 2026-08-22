from __future__ import annotations

from collections.abc import Sequence

from obsalt.domain.enums import Capability
from obsalt.domain.events import NormalizedEvent
from obsalt.otel.foreign import ForeignConventionMapper
from obsalt.plugin.contract import OtlpMapper
from obsalt.plugin.host import LoadedPlugin
from obsalt.plugin.types import ReadableSpan


class MapperRegistry:
    def __init__(self, plugins: Sequence[LoadedPlugin]) -> None:
        self.mappers: list[OtlpMapper] = [
            p.plugin
            for p in plugins
            if p.has(Capability.OTLP_MAPPER)  # type: ignore[misc]
        ]
        # Conventions, not a provider. Lowest priority so first-party plugins win.
        if not any(getattr(mapper, "name", "") == "foreign-conventions" for mapper in self.mappers):
            self.mappers.append(ForeignConventionMapper())

    def pick(self, span: ReadableSpan) -> OtlpMapper | None:
        best: tuple[int, OtlpMapper] | None = None
        for mapper in self.mappers:
            score = mapper.claims(span)
            if score <= 0:
                continue
            if best is None or score > best[0]:
                best = (score, mapper)
        return best[1] if best else None

    def decode(self, spans: Sequence[ReadableSpan]) -> list[NormalizedEvent]:
        if not spans:
            return []
        groups: dict[int, list[ReadableSpan]] = {}
        mappers: dict[int, OtlpMapper] = {}
        for span in spans:
            mapper = self.pick(span)
            if mapper is None:
                continue
            ident = id(mapper)
            mappers[ident] = mapper
            groups.setdefault(ident, []).append(span)
        events: list[NormalizedEvent] = []
        for ident, group in groups.items():
            mapper = mappers[ident]
            decode_spans = getattr(mapper, "decode_spans", None)
            decode_fn = decode_spans if callable(decode_spans) else mapper.decode
            events.extend(list(decode_fn(group)))
        return events
