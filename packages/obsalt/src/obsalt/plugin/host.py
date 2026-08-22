"""Plugin discovery via ``importlib.metadata`` entry-point group ``obsalt.plugins``.

First-party providers register through the same group — no privileged path.
Plugins are trusted operator-installed code. Loading isolates ordinary
exceptions and version mismatches; it is not a security sandbox.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from importlib.metadata import EntryPoint, entry_points
from typing import Any, cast

from obsalt._version import SUPPORTED_PLUGIN_API
from obsalt.domain.enums import Capability
from obsalt.plugin.protocol import FidelityDeclaration, OtlpMapper, WebhookSource

log = logging.getLogger("obsalt.plugin")

ENTRY_POINT_GROUP = "obsalt.plugins"


@dataclass
class LoadedPlugin:
    name: str
    display_name: str
    api_version: int
    capabilities: frozenset[Capability]
    instance: Any
    fidelity: FidelityDeclaration | None
    module: str


@dataclass
class PluginHost:
    plugins: dict[str, LoadedPlugin] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, extra: Iterable[type | object] = ()) -> PluginHost:
        host = cls()
        discovered = _entry_points()
        for ep in discovered:
            try:
                loaded = ep.load()
                host._register(loaded, source=f"entry_point:{ep.value}")
            except Exception as exc:  # noqa: BLE001 — isolate plugin load failures
                host.errors.append(f"{ep.name}: failed to load ({exc})")
                log.exception("plugin %s failed to load", ep.name)
        for item in extra:
            host._register(item, source="explicit")
        return host

    def _register(self, loaded: Any, *, source: str) -> None:
        plugin = loaded() if isinstance(loaded, type) else loaded
        name = str(getattr(plugin, "name", "") or "")
        api = getattr(plugin, "API_VERSION", None)
        if not name:
            self.errors.append(f"{source}: plugin missing name")
            return
        if not isinstance(api, int):
            self.errors.append(f"{name}: missing API_VERSION")
            return
        lo, hi = SUPPORTED_PLUGIN_API
        if api < lo or api > hi:
            self.errors.append(f"{name}: API_VERSION {api} outside core supported range {lo}-{hi}")
            return
        caps = frozenset(getattr(plugin, "capabilities", frozenset()))
        self.plugins[name] = LoadedPlugin(
            name=name,
            display_name=str(getattr(plugin, "display_name", name)),
            api_version=api,
            capabilities=caps,
            instance=plugin,
            fidelity=getattr(plugin, "fidelity", None),
            module=source,
        )

    def get(self, name: str) -> LoadedPlugin:
        try:
            return self.plugins[name]
        except KeyError as exc:
            raise KeyError(f"plugin {name!r} is not installed") from exc

    def webhook(self, name: str) -> WebhookSource:
        plugin = self.get(name).instance
        if Capability.WEBHOOK_SOURCE not in self.get(name).capabilities:
            raise TypeError(f"plugin {name!r} does not declare webhook_source")
        return cast(WebhookSource, plugin)

    def mappers(self) -> list[tuple[str, OtlpMapper]]:
        out: list[tuple[str, OtlpMapper]] = []
        for loaded in self.plugins.values():
            if Capability.OTLP_MAPPER in loaded.capabilities:
                out.append((loaded.name, loaded.instance))
        return out

    def by_capability(self, capability: Capability) -> list[LoadedPlugin]:
        return [p for p in self.plugins.values() if capability in p.capabilities]

    def inventory(self) -> list[dict[str, Any]]:
        rows = []
        for plugin in self.plugins.values():
            rows.append(
                {
                    "name": plugin.name,
                    "display_name": plugin.display_name,
                    "api_version": plugin.api_version,
                    "capabilities": sorted(c.value for c in plugin.capabilities),
                    "fidelity": plugin.fidelity.model_dump(mode="json") if plugin.fidelity else None,
                    "module": plugin.module,
                }
            )
        return rows


def _entry_points() -> tuple[EntryPoint, ...]:
    eps = entry_points()
    if hasattr(eps, "select"):
        return tuple(eps.select(group=ENTRY_POINT_GROUP))
    return tuple(eps.get(ENTRY_POINT_GROUP, ()))  # type: ignore[union-attr]
