"""Webhook auth must see gzip wire bytes, with an expanded-body fallback."""

from __future__ import annotations

import gzip

from obsalt.crypto.primitives import hmac_hex
from obsalt.ingest.headers import RawHeaders
from obsalt.ingest.receive import receive_webhook
from obsalt.plugin.types import ConnectionConfig
from obsalt.testing.fakes import MemoryInbox, MemoryObjectStore, MemoryResolver
from obsalt_example.plugin import ExamplePlugin

from tests.helpers import example_raw


def _cfg(secret: str = "example-secret") -> ConnectionConfig:
    return ConnectionConfig(
        org_id="acme",
        provider="example",
        connection_id="c1",
        ingest_key_hash="x",
        secrets={"hmac_secret": secret},
    )


def test_webhook_authenticates_gzip_wire_bytes() -> None:
    raw = example_raw()
    wire = gzip.compress(raw)
    secret = "example-secret"
    resolver = MemoryResolver()
    resolver.add(_cfg(secret), "ik")
    objects = MemoryObjectStore()
    inbox = MemoryInbox()
    result = receive_webhook(
        provider="example",
        ingest_key="ik",
        raw=wire,
        headers=RawHeaders.from_mapping(
            {"x-obsalt-example-signature": hmac_hex(secret, wire), "content-encoding": "gzip"}
        ),
        resolver=resolver,
        plugin=ExamplePlugin(),
        objects=objects,
        inbox=inbox,
        content_encoding="gzip",
    )
    assert result.rejected is None
    assert result.envelope is not None
    assert result.envelope.body == wire
    assert result.envelope.headers.get("content-encoding") == "gzip"
    assert objects.get(result.envelope.object_key) == wire


def test_webhook_auth_does_not_fall_back_to_expanded_body() -> None:
    raw = example_raw()
    wire = gzip.compress(raw)
    secret = "example-secret"
    resolver = MemoryResolver()
    resolver.add(_cfg(secret), "ik")
    result = receive_webhook(
        provider="example",
        ingest_key="ik",
        raw=wire,
        headers=RawHeaders.from_mapping(
            {"x-obsalt-example-signature": hmac_hex(secret, raw), "content-encoding": "gzip"}
        ),
        resolver=resolver,
        plugin=ExamplePlugin(),
        objects=MemoryObjectStore(),
        inbox=MemoryInbox(),
        content_encoding="gzip",
    )
    assert result.rejected is not None
    assert result.envelope is None
