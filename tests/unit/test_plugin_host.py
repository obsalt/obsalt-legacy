from __future__ import annotations

from obsalt.domain.enums import Capability
from obsalt.plugin.host import PluginHost
from obsalt_example.plugin import ExamplePlugin


def test_example_plugin_discovered_via_entry_points() -> None:
    host = PluginHost.load()
    assert "example" in host.plugins
    loaded = host.get("example")
    assert loaded.api_version == 1
    assert Capability.WEBHOOK_SOURCE in loaded.capabilities
    assert Capability.STREAM_SOURCE in loaded.capabilities


def test_explicit_register() -> None:
    host = PluginHost.load(extra=[ExamplePlugin()])
    assert host.webhook("example").name == "example"


def test_version_mismatch_isolated() -> None:
    class Bad:
        API_VERSION = 99
        name = "bad"
        display_name = "Bad"
        capabilities = frozenset()
        manifest = None
        fidelity = None

    host = PluginHost()
    host._register(Bad(), source="test")
    assert "bad" not in host.plugins
    assert any("API_VERSION 99" in e for e in host.errors)
