from __future__ import annotations

from collections import defaultdict

# Never archive authorization or cookie headers. Raw blobs are unredacted; headers are not.
DIAGNOSTIC_HEADER_ALLOWLIST = frozenset(
    {
        "content-type",
        "content-encoding",
        "user-agent",
        "x-request-id",
        "x-correlation-id",
        "elevenlabs-signature",  # timestamp only is not stored; the header name is allowlisted for diagnostics of presence
        "x-retell-signature",
        "x-vapi-secret",
        "x-webhook-secret",
    }
)

FORBIDDEN_HEADER_ARCHIVE = frozenset({"authorization", "cookie", "set-cookie", "proxy-authorization"})


class RawHeaders:
    """Preserves multi-valued headers. Core rejects duplicates for plugin-declared singletons."""

    def __init__(self, pairs: list[tuple[bytes, bytes]]) -> None:
        self.pairs = [(k, v) for k, v in pairs]

    @classmethod
    def from_mapping(cls, mapping: dict[str, str]) -> RawHeaders:
        return cls([(k.encode("latin-1"), v.encode("latin-1")) for k, v in mapping.items()])

    def as_list(self) -> list[tuple[bytes, bytes]]:
        return list(self.pairs)

    def allowlisted(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for key, value in self.pairs:
            name = key.decode("latin-1").lower()
            if name in FORBIDDEN_HEADER_ARCHIVE:
                continue
            if name in DIAGNOSTIC_HEADER_ALLOWLIST:
                # Store presence + non-secret diagnostic shape, never the credential itself.
                if name.endswith("signature") or name.endswith("secret"):
                    out[name] = "<present>"
                else:
                    out[name] = value.decode("latin-1")
        return out

    def grouped(self) -> dict[bytes, list[bytes]]:
        grouped: dict[bytes, list[bytes]] = defaultdict(list)
        for key, value in self.pairs:
            grouped[key.lower()].append(value)
        return dict(grouped)
