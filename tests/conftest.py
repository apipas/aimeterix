"""Every test gets its own home folder, so none reads or writes the real one."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _own_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    for name in (
        "AIMETERIX_HOME",
        "AIMETERIX_CONFIG",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
    ):
        monkeypatch.delenv(name, raising=False)
