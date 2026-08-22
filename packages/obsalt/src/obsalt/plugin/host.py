from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from importlib.metadata import entry_points
from typing import Any, TypeVar

from obsalt._version import SUPPORTED_PLUGIN_API
from obsalt.domain.enums import Capability
from obsalt.domain.models import FidelityDeclaration
from obsalt.plugin.contract import ObsaltPlugin
from obsalt.plugin.types import PluginManifest

log = logging.getLogger("obsalt.plugin")

ENTRY_POINT_GROUP = "obsalt.plugins"

_T = TypeVar("_T")


def invoke_with_deadline(
    fn: Callable[..., _T],
    *args: Any,
    timeout_seconds: float = 10.0,
    **kwargs: Any,
) -> _T:
    """Isolate a plugin call with a join deadline. Not a security sandbox (§7.2)."""

    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["result"] = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — surface the plugin error after join
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True, name="obsalt-plugin")
    worker.start()
    worker.join(timeout_seconds)
    if worker.is_alive():
        raise TimeoutError(f"plugin invocation exceeded {timeout_seconds}s")
    error = box.get("error")
    if error is not None:
        raise error
    return box["result"]


class LoadedPlugin:
    def __init__(self, plugin: ObsaltPlugin) -> None:
        self.plugin = plugin
        self.name = plugin.name
        self.display_name = plugin.display_name
        self.api_version = plugin.API_VERSION
        self.capabilities = plugin.capabilities
        self.manifest: PluginManifest = plugin.manifest
        self.fidelity: FidelityDeclaration = plugin.fidelity

    def has(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def as_capability(self, protocol: type) -> Any:
        if isinstance(self.plugin, protocol):
            return self.plugin
        # Concrete classes often implement the methods without inheriting the Protocol.
        return self.plugin


class PluginLoadError(Exception):
    pass


def discover_plugins(group: str = ENTRY_POINT_GROUP) -> list[LoadedPlugin]:
    """Load operator-installed plugins. Version mismatches are isolated, not fatal to others."""

    loaded: list[LoadedPlugin] = []
    try:
        eps = entry_points(group=group)
    except TypeError:  # pragma: no cover - py3.11 compat path
        eps = entry_points().select(group=group)
    for ep in eps:
        try:
            plugin = ep.load()
            plugin = plugin() if isinstance(plugin, type) else plugin
            version = int(getattr(plugin, "API_VERSION", -1))
            if version not in SUPPORTED_PLUGIN_API:
                log.error(
                    "plugin %s API_VERSION=%s is outside supported %s; skipped",
                    ep.name,
                    version,
                    f"{SUPPORTED_PLUGIN_API.start}..{SUPPORTED_PLUGIN_API.stop - 1}",
                )
                continue
            name = getattr(plugin, "name", None)
            if not name:
                raise PluginLoadError("plugin is missing name")
            loaded.append(LoadedPlugin(plugin))
        except Exception:
            log.exception("failed to load plugin from entry point %s", ep.name)
    return loaded


def plugin_by_name(name: str, plugins: Iterable[LoadedPlugin] | None = None) -> LoadedPlugin:
    for plugin in plugins or discover_plugins():
        if plugin.name == name:
            return plugin
    raise KeyError(f"plugin {name!r} is not installed")
