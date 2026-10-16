"""Where AI Meterix keeps its files (record 0019)."""

from __future__ import annotations

from pathlib import Path

from ai_meterix.core.paths import Paths, paths

HOME = {"HOME": "/Users/ana"}


def test_macos_uses_application_support_and_the_logs_folder() -> None:
    where = paths(HOME, "darwin")
    support = Path("/Users/ana/Library/Application Support/aimeterix")
    assert where == Paths(
        support, support, support / "run", Path("/Users/ana/Library/Logs/aimeterix")
    )
    assert where.config_file == support / "aimeterix.yaml"
    assert where.storage_url == f"sqlite:///{support / 'meter.db'}"
    assert sorted(where.folders()) == [support, Path("/Users/ana/Library/Logs/aimeterix")]


def test_linux_follows_xdg() -> None:
    assert paths({"HOME": "/home/ana"}, "linux") == Paths(
        Path("/home/ana/.config/aimeterix"),
        Path("/home/ana/.local/share/aimeterix"),
        Path("/home/ana/.local/state/aimeterix/run"),
        Path("/home/ana/.local/state/aimeterix/logs"),
    )
    assert sorted(paths({"HOME": "/home/ana"}, "linux").folders()) == [
        Path("/home/ana/.config/aimeterix"),
        Path("/home/ana/.local/share/aimeterix"),
        Path("/home/ana/.local/state/aimeterix"),
    ]
    env = {"HOME": "/home/ana", "XDG_CONFIG_HOME": "/cfg", "XDG_DATA_HOME": "relative"}
    where = paths(env, "linux")
    assert where.config_dir == Path("/cfg/aimeterix")
    assert where.data_dir == Path("/home/ana/.local/share/aimeterix")  # relative is ignored


def test_aimeterix_home_puts_everything_in_one_folder() -> None:
    where = paths({**HOME, "AIMETERIX_HOME": "/srv/meter"}, "darwin")
    assert where == Paths(
        Path("/srv/meter"), Path("/srv/meter"), Path("/srv/meter/run"), Path("/srv/meter/logs")
    )
    assert where.folders() == [Path("/srv/meter")]
