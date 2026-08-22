"""Raw multi-valued headers. Core rejects duplicates for plugin-declared singletons."""

from __future__ import annotations

from dataclasses import dataclass

from obsalt.domain.enums import VerifyOutcome
from obsalt.plugin.protocol import VerifyResult


@dataclass(frozen=True, slots=True)
class RawHeaders:
    pairs: list[tuple[bytes, bytes]]

    @classmethod
    def from_mapping(cls, mapping: dict[str, str] | dict[bytes, bytes]) -> RawHeaders:
        pairs: list[tuple[bytes, bytes]] = []
        for key, value in mapping.items():
            k = key if isinstance(key, bytes) else key.encode("latin-1")
            v = value if isinstance(value, bytes) else value.encode("latin-1")
            pairs.append((k, v))
        return cls(pairs)

    def getall(self, name: str | bytes) -> list[bytes]:
        want = name.lower() if isinstance(name, bytes) else name.encode("latin-1").lower()
        return [value for key, value in self.pairs if key.lower() == want]

    def get(self, name: str | bytes) -> bytes | None:
        values = self.getall(name)
        return values[0] if values else None

    def as_str(self, name: str) -> str | None:
        value = self.get(name)
        return None if value is None else value.decode("latin-1")


def reject_duplicate_singletons(
    headers: list[tuple[bytes, bytes]],
    singleton_headers: frozenset[bytes],
) -> VerifyResult | None:
    seen: dict[bytes, int] = {}
    for key, _value in headers:
        lowered = key.lower()
        if lowered in {s.lower() for s in singleton_headers}:
            seen[lowered] = seen.get(lowered, 0) + 1
    for name, count in seen.items():
        if count > 1:
            return VerifyResult(
                outcome=VerifyOutcome.MALFORMED,
                detail=f"duplicate singleton header {name.decode('latin-1')}",
            )
    return None
