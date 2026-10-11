from importlib.metadata import EntryPoint

import pytest

from ai_meterix.core import plugins
from ai_meterix.core.errors import PluginError
from ai_meterix.core.plugins import PluginRegistry, scheme_of


class FakeConnector:
    pass


@pytest.fixture
def fake_entry_points(monkeypatch: pytest.MonkeyPatch) -> None:
    eps = [
        EntryPoint("fake", f"{__name__}:FakeConnector", "test.group"),
        EntryPoint("broken", "no_such_module_xyz:Thing", "test.group"),
    ]

    def entry_points(*, group: str) -> list[EntryPoint]:
        return [ep for ep in eps if ep.group == group]

    monkeypatch.setattr(plugins, "entry_points", entry_points)


@pytest.mark.usefixtures("fake_entry_points")
def test_entry_point_is_discovered_and_loaded() -> None:
    registry: PluginRegistry[object] = PluginRegistry("test.group")
    assert registry.names() == ["broken", "fake"]
    assert registry.get("FAKE") is FakeConnector


@pytest.mark.usefixtures("fake_entry_points")
def test_registered_plugin_wins_over_entry_point() -> None:
    registry: PluginRegistry[object] = PluginRegistry("test.group")
    registry.register("fake", "builtin")
    assert registry.get("fake") == "builtin"


@pytest.mark.usefixtures("fake_entry_points")
def test_unknown_and_broken_plugins_raise_plugin_error() -> None:
    registry: PluginRegistry[object] = PluginRegistry("test.group")
    with pytest.raises(PluginError, match=r"no plugin 'nope'.*available: broken, fake"):
        registry.get("nope")
    with pytest.raises(PluginError, match="failed to load"):
        registry.get("broken")


@pytest.mark.parametrize(
    ("url", "scheme"),
    [
        ("sqlite:///~/.aimeterix/meter.db", "sqlite"),
        ("postgresql+psycopg://u@db/meter", "postgresql"),
        ("DuckDB:///meter.duckdb", "duckdb"),
    ],
)
def test_scheme_of(url: str, scheme: str) -> None:
    assert scheme_of(url) == scheme


def test_scheme_of_requires_scheme() -> None:
    with pytest.raises(PluginError, match="no scheme"):
        scheme_of("/tmp/meter.db")
