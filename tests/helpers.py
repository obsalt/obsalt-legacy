"""SET harness: fixtures, signed headers, fakes, and independent oracles.

Memory stores are test doubles, not a product backend. Oracles here are
independent of the code under test — they state what the product promised.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from obsalt.api import create_test_app
from obsalt.assemble.facts import stamp_event
from obsalt.assemble.promote import MemoryPointerStore
from obsalt.config import Settings
from obsalt.crypto.primitives import hmac_hex
from obsalt.domain.enums import KeyScope, MeasurementPlacement, PipelineArchitecture, Role, Signal
from obsalt.domain.models import FidelityDeclaration
from obsalt.plugin.host import LoadedPlugin
from obsalt.plugin.types import ConnectionConfig
from obsalt.runtime import AppState, MemoryOrgSpend
from obsalt.search.index import MemorySearchIndex
from obsalt.testing.fakes import MemoryInbox, MemoryObjectStore, MemoryResolver
from obsalt.worker.process import MemoryRevisionSink
from obsalt_cartesia.plugin import CartesiaPlugin
from obsalt_elevenlabs.plugin import ElevenLabsPlugin
from obsalt_example.plugin import ExamplePlugin
from obsalt_retell.plugin import RetellPlugin
from obsalt_vapi.plugin import VapiPlugin

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_FIXTURES = ROOT / "packages" / "obsalt-example" / "src" / "obsalt_example" / "fixtures"
VAPI_FIXTURES = ROOT / "packages" / "obsalt-vapi" / "src" / "obsalt_vapi" / "fixtures"
RETELL_FIXTURES = ROOT / "packages" / "obsalt-retell" / "src" / "obsalt_retell" / "fixtures"
ELEVEN_FIXTURES = ROOT / "packages" / "obsalt-elevenlabs" / "src" / "obsalt_elevenlabs" / "fixtures"
CARTESIA_FIXTURES = ROOT / "packages" / "obsalt-cartesia" / "src" / "obsalt_cartesia" / "fixtures"
PIPECAT_OTLP = ROOT / "packages" / "obsalt-pipecat" / "src" / "obsalt_pipecat" / "fixtures" / "otlp"
LIVEKIT_OTLP = ROOT / "packages" / "obsalt-livekit" / "src" / "obsalt_livekit" / "fixtures" / "otlp"
ELEVEN_OTLP = (
    ROOT / "packages" / "obsalt-elevenlabs" / "src" / "obsalt_elevenlabs" / "fixtures" / "otlp"
)
OPENAI_OTLP = (
    ROOT
    / "packages"
    / "obsalt-openai-realtime"
    / "src"
    / "obsalt_openai_realtime"
    / "fixtures"
    / "otlp"
)
GEMINI_OTLP = (
    ROOT / "packages" / "obsalt-gemini-live" / "src" / "obsalt_gemini_live" / "fixtures" / "otlp"
)

# Wide window used when a test is not itself about the time filter.
RANGE_START = "2020-01-01T00:00:00Z"
RANGE_END = "2030-01-01T00:00:00Z"
RANGE_QS = f"start={RANGE_START}&end={RANGE_END}"

# Independent oracles from the committed example fixture (not from running decode).
EXAMPLE_SOURCE_CALL_ID = "ex-1"
EXAMPLE_AGENT_ID = "support"
EXAMPLE_USER_TEXT = "I want a refund."
EXAMPLE_STARTED_AT = "2026-08-22T12:00:01"
EXAMPLE_STT_MS = 180.0


def auth(key: str = "k") -> dict[str, str]:
    return {"X-API-Key": key}


def signed_example_headers(raw: bytes, secret: str = "s") -> dict[str, str]:
    return {"x-obsalt-example-signature": hmac_hex(secret, raw), "content-type": "application/json"}


def example_raw() -> bytes:
    return (EXAMPLE_FIXTURES / "raw" / "call_ended.json").read_bytes()


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


def stamp(event, seq: int, *, call_key: str = "k"):
    return stamp_event(
        event,
        org_id="acme",
        call_key=call_key,
        envelope_id="e",
        decoder_version="t/1",
        processing_run_id="r",
        envelope_sequence=seq,
    )


def provider_state(
    plugin: Any,
    *,
    secrets: dict[str, str],
    org_id: str = "acme",
    api_key: str = "k",
    ingest_key: str = "ik",
    extra_keys: dict[str, tuple[str, frozenset[KeyScope]]] | None = None,
    extra_plugins: list[Any] | None = None,
    settings: Settings | None = None,
    **kwargs: Any,
) -> AppState:
    resolver = kwargs.pop("resolver", None) or MemoryResolver()
    plugin_settings = (
        {"auth_mode": "legacy_secret"} if getattr(plugin, "name", "") == "vapi" else {}
    )
    resolver.add(
        ConnectionConfig(
            org_id=org_id,
            provider=plugin.name,
            connection_id="c1",
            ingest_key_hash="",
            secrets=secrets,
            settings=plugin_settings,
        ),
        ingest_key,
    )
    plugins = [LoadedPlugin(plugin)]
    for extra in extra_plugins or []:
        plugins.append(LoadedPlugin(extra) if not isinstance(extra, LoadedPlugin) else extra)
    keys = {api_key: (org_id, frozenset(KeyScope))}
    if extra_keys:
        keys.update(extra_keys)
    key_roles = {name: Role.OWNER for name in keys}
    return AppState(
        settings=settings or Settings(environment="test", trace_grace_seconds=0),
        plugins=plugins,
        resolver=resolver,
        objects=kwargs.pop("objects", MemoryObjectStore()),
        inbox=kwargs.pop("inbox", MemoryInbox()),
        pointers=kwargs.pop("pointers", MemoryPointerStore()),
        sink=kwargs.pop("sink", MemoryRevisionSink()),
        keys=keys,
        key_roles=key_roles,
        rollup_generation=kwargs.pop("rollup_generation", "g1"),
        search=kwargs.pop("search", MemorySearchIndex()),
        spend_store=kwargs.pop("spend_store", MemoryOrgSpend()),
        org_spend=kwargs.pop("org_spend", {org_id: 0.0}),
        **kwargs,
    )


def example_state(**kwargs: Any) -> AppState:
    extras = kwargs.pop("extra_plugins", [])
    extra_keys = kwargs.pop("extra_keys", None) or {"other": ("other", frozenset(KeyScope))}
    return provider_state(
        ExamplePlugin(),
        secrets={"hmac_secret": "s"},
        extra_plugins=extras,
        extra_keys=extra_keys,
        **kwargs,
    )


def vapi_state(**kwargs: Any) -> AppState:
    return provider_state(VapiPlugin(), secrets={"legacy_secret": "vapi-secret"}, **kwargs)


def retell_state(**kwargs: Any) -> AppState:
    return provider_state(RetellPlugin(), secrets={"api_key": "retell-key"}, **kwargs)


def api_client(state: AppState) -> TestClient:
    settings = getattr(state, "settings", None) or Settings(environment="test")
    if (settings.environment or "").lower() not in {"test", "testing"}:
        settings = Settings(environment="test")
    return TestClient(create_test_app(settings, state))


def vapi_headers(secret: str = "vapi-secret") -> dict[str, str]:
    return {"x-vapi-secret": secret, "content-type": "application/json"}


def retell_headers(raw: bytes, api_key: str = "retell-key") -> dict[str, str]:
    import time

    ts = str(int(time.time() * 1000))
    digest = hmac_hex(api_key, raw + ts.encode("utf-8"))
    return {"x-retell-signature": f"v={ts},d={digest}", "content-type": "application/json"}


def example_headers(raw: bytes, secret: str = "s") -> dict[str, str]:
    return signed_example_headers(raw, secret)


def elevenlabs_state(**kwargs: Any) -> AppState:
    return provider_state(ElevenLabsPlugin(), secrets={"webhook_secret": "eleven-secret"}, **kwargs)


def cartesia_state(**kwargs: Any) -> AppState:
    return provider_state(CartesiaPlugin(), secrets={"webhook_secret": "line-secret"}, **kwargs)


def elevenlabs_headers(raw: bytes, secret: str = "eleven-secret") -> dict[str, str]:
    import time

    ts = str(int(time.time()))
    digest = hmac_hex(secret, f"{ts}.".encode() + raw)
    return {"elevenlabs-signature": f"t={ts},v0={digest}", "content-type": "application/json"}


def cartesia_headers(secret: str = "line-secret") -> dict[str, str]:
    return {"x-webhook-secret": secret, "content-type": "application/json"}


def ingest_example(client: TestClient, raw: bytes | None = None, ingest_key: str = "ik") -> None:
    payload = raw if raw is not None else example_raw()
    posted = client.post(
        f"/v1/ingest/example/{ingest_key}", content=payload, headers=example_headers(payload)
    )
    assert posted.status_code == 200, posted.text


def list_calls(client: TestClient, key: str = "k", qs: str = RANGE_QS) -> dict[str, Any]:
    res = client.get(f"/v1/calls?{qs}", headers=auth(key))
    assert res.status_code == 200, res.text
    return res.json()


def first_call_id(client: TestClient, key: str = "k") -> str:
    items = list_calls(client, key)["items"]
    assert items, "expected at least one call in range"
    return items[0]["id"]


def assert_no_stage_waterfall(body: dict[str, Any]) -> None:
    """Independent of provider: a waterfall requires real stage intervals."""
    assert body["draw_stage_waterfall"] is False
    assert body["stage_intervals"] == []


def assert_generation_labelled(body: dict[str, Any]) -> None:
    assert body.get("as_of_generation"), "fleet responses must carry one serving generation"
