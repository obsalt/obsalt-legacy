from __future__ import annotations

from collections.abc import Sequence

from obsalt.domain.enums import Capability
from obsalt.domain.events import NormalizedEvent
from obsalt.plugin.contract import OtlpMapper
from obsalt.plugin.host import LoadedPlugin
from obsalt.plugin.types import ReadableSpan


class MapperRegistry:
    def __init__(self, plugins: Sequence[LoadedPlugin]) -> None:
        self.mappers: list[OtlpMapper] = [
            p.plugin for p in plugins if p.has(Capability.OTLP_MAPPER)  # type: ignore[misc]
        ]

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
            events.extend(list(mappers[ident].decode(group)))
        return events
