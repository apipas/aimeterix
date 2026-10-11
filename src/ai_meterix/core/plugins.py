"""Plugin registry backed by Python entry points.

Third parties add a storage connector without forking by declaring, in their
package metadata::

    [project.entry-points."ai_meterix.storage"]
    clickhouse = "my_package.connector:ClickHouseConnector"

The entry point name is the URL scheme the connector serves.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Generic, TypeVar
from urllib.parse import urlsplit

from .errors import PluginError

T = TypeVar("T")

STORAGE_GROUP = "ai_meterix.storage"


class PluginRegistry(Generic[T]):
    """Name -> plugin object, filled by explicit registration and by entry points.

    Explicit registrations (built-ins, tests) take precedence over entry points
    with the same name. Entry points are imported only when first looked up.
    """

    def __init__(self, group: str) -> None:
        self.group = group
        self._registered: dict[str, T] = {}
        self._loaded: dict[str, T] = {}

    def register(self, name: str, plugin: T) -> None:
        self._registered[name.lower()] = plugin

    def names(self) -> list[str]:
        discovered = {ep.name.lower() for ep in entry_points(group=self.group)}
        return sorted(discovered | set(self._registered))

    def get(self, name: str) -> T:
        key = name.lower()
        if key in self._registered:
            return self._registered[key]
        if key in self._loaded:
            return self._loaded[key]
        for ep in entry_points(group=self.group):
            if ep.name.lower() == key:
                try:
                    plugin: T = ep.load()
                except Exception as exc:
                    raise PluginError(
                        f"plugin '{name}' in group '{self.group}' failed to load: {exc}"
                    ) from exc
                self._loaded[key] = plugin
                return plugin
        available = ", ".join(self.names()) or "none"
        raise PluginError(f"no plugin '{name}' in group '{self.group}' (available: {available})")


storage_connectors: PluginRegistry[object] = PluginRegistry(STORAGE_GROUP)


def scheme_of(url: str) -> str:
    """URL scheme used to pick a storage connector, e.g. ``postgresql`` -> ``postgresql``.

    A driver suffix is dropped: ``postgresql+psycopg://`` -> ``postgresql``.
    """
    scheme = urlsplit(url).scheme.lower()
    if not scheme:
        raise PluginError(f"storage URL has no scheme: {url!r} (expected e.g. sqlite:///path.db)")
    return scheme.split("+", 1)[0]
