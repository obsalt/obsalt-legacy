"""Shared builders for the layered v2 suite. Existing tests keep their local helpers."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from obsalt.api import create_app
from obsalt.assemble.promote import MemoryPointerStore
from obsalt.config import Settings
from obsalt.crypto.primitives import hmac_hex
from obsalt.domain.enums import KeyScope, MeasurementPlacement, PipelineArchitecture, Signal
from obsalt.domain.models import FidelityDeclaration
from obsalt.plugin.host import LoadedPlugin
from obsalt.plugin.types import ConnectionConfig
from obsalt.runtime import AppState
from obsalt.search.index import MemorySearchIndex
from obsalt.testing.fakes import MemoryInbox, MemoryObjectStore, MemoryResolver
from obsalt.worker.process import MemoryRevisionSink
from obsalt_example.plugin import ExamplePlugin
from obsalt_retell.plugin import RetellPlugin
from obsalt_vapi.plugin import VapiPlugin

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_FIXTURES = ROOT / "packages/obsalt-example/src/obsalt_example/fixtures"
VAPI_FIXTURES = ROOT / "packages/obsalt-vapi/src/obsalt_vapi/fixtures"
RETELL_FIXTURES = ROOT / "packages/obsalt-retell/src/obsalt_retell/fixtures"
ELEVEN_FIXTURES = ROOT / "packages/obsalt-elevenlabs/src/obsalt_elevenlabs/fixtures"


def fidelity_declaration() -> FidelityDeclaration:
    return FidelityDeclaration(
        source_format="test",
        possible_architectures=frozenset({PipelineArchitecture.CASCADE}),
        possible_placements=frozenset(MeasurementPlacement),
        provides=frozenset({Signal.STT_DURATION, Signal.TURN_INTERVAL, Signal.TOOL_RESULT}),
        structurally_absent={Signal.VAD: "not in fixture"},
        schema_source="test",
        schema_revision="1",
        verified_at=date(2026, 8, 22),
    )


def provider_state(
    plugin: object,
    *,
    secrets: dict[str, str],
    org_id: str = "acme",
    api_key: str = "k",
    ingest_key: str = "ik",
    extra_keys: dict[str, tuple[str, frozenset[KeyScope]]] | None = None,
    extra_plugins: list[object] | None = None,
) -> AppState:
    resolver = MemoryResolver()
    settings = {"auth_mode": "legacy_secret"} if getattr(plugin, "name", "") == "vapi" else {}
    resolver.add(
        ConnectionConfig(
            org_id=org_id,
            provider=getattr(plugin, "name"),
            connection_id="c1",
            ingest_key_hash="",
            secrets=secrets,
            settings=settings,
        ),
        ingest_key,
    )
    plugins = [LoadedPlugin(plugin)]  # type: ignore[arg-type]
    for extra in extra_plugins or []:
        plugins.append(LoadedPlugin(extra))  # type: ignore[arg-type]
    keys = {api_key: (org_id, frozenset(KeyScope))}
    if extra_keys:
        keys.update(extra_keys)
    return AppState(
        settings=Settings(),
        plugins=plugins,
        resolver=resolver,
        objects=MemoryObjectStore(),
        inbox=MemoryInbox(),
        pointers=MemoryPointerStore(),
        sink=MemoryRevisionSink(),
        keys=keys,
        rollup_generation="g1",
        search=MemorySearchIndex(),
    )


def example_state(**kwargs) -> AppState:
    return provider_state(ExamplePlugin(), secrets={"hmac_secret": "s"}, **kwargs)


def vapi_state(**kwargs) -> AppState:
    return provider_state(VapiPlugin(), secrets={"legacy_secret": "vapi-secret"}, **kwargs)


def retell_state(**kwargs) -> AppState:
    return provider_state(RetellPlugin(), secrets={"api_key": "retell-key"}, **kwargs)


def api_client(state: AppState) -> TestClient:
    return TestClient(create_app(Settings(), state))


def vapi_headers(secret: str = "vapi-secret") -> dict[str, str]:
    return {"x-vapi-secret": secret, "content-type": "application/json"}


def retell_headers(raw: bytes, api_key: str = "retell-key") -> dict[str, str]:
    import time

    ts = str(int(time.time() * 1000))
    digest = hmac_hex(api_key, raw + ts.encode("utf-8"))
    return {"x-retell-signature": f"v={ts},d={digest}", "content-type": "application/json"}


def example_headers(raw: bytes, secret: str = "s") -> dict[str, str]:
    return {"x-obsalt-example-signature": hmac_hex(secret, raw), "content-type": "application/json"}


@pytest.fixture
def decl() -> FidelityDeclaration:
    return fidelity_declaration()
