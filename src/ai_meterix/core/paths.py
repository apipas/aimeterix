"""Where AI Meterix keeps its files (record 0019).

One place decides every path, so a command run from any folder finds the same
config, database and logs. The defaults follow each system's conventions:

=========  =============================================  ===============================
           macOS                                          Linux and others
=========  =============================================  ===============================
config     ``~/Library/Application Support/aimeterix``    ``$XDG_CONFIG_HOME/aimeterix``
data       ``~/Library/Application Support/aimeterix``    ``$XDG_DATA_HOME/aimeterix``
run        ``~/Library/Application Support/aimeterix/run`` ``$XDG_STATE_HOME/aimeterix/run``
logs       ``~/Library/Logs/aimeterix``                   ``$XDG_STATE_HOME/aimeterix/logs``
=========  =============================================  ===============================

``AIMETERIX_HOME`` puts everything in one folder instead (config and data at its
top, ``run/`` and ``logs/`` inside). Nothing needs admin rights. The config file
is ``aimeterix.yaml`` and its secrets ``.env``, both in the config folder;
``--config`` or ``AIMETERIX_CONFIG`` names another file.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

HOME_ENV = "AIMETERIX_HOME"
APP = "aimeterix"
CONFIG_FILE = "aimeterix.yaml"
DB_FILE = "meter.db"
LEGACY_HOME = Path("~/.aimeterix")


@dataclass(frozen=True, slots=True)
class Paths:
    config_dir: Path
    data_dir: Path
    run_dir: Path
    log_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / CONFIG_FILE

    @property
    def db_file(self) -> Path:
        return self.data_dir / DB_FILE

    @property
    def storage_url(self) -> str:
        return f"sqlite:///{self.db_file}"

    def folders(self) -> list[Path]:
        """Every folder AI Meterix may create, outermost first, none inside another."""
        found: list[Path] = []
        every = {self.config_dir, self.data_dir, self.run_dir, self.log_dir}
        if self.run_dir.parent == self.log_dir.parent and self.run_dir.parent.name == APP:
            every.add(self.run_dir.parent)  # Linux: the state folder holds both
        for folder in sorted(every, key=lambda f: len(f.parts)):
            if not any(folder.is_relative_to(f) for f in found):
                found.append(folder)
        return found


def _home(env: Mapping[str, str]) -> Path:
    return Path(env["HOME"]) if env.get("HOME") else Path.home()


def _xdg(env: Mapping[str, str], name: str, fallback: str) -> Path:
    value = env.get(name, "")
    # the XDG spec ignores relative values
    return Path(value) if value and Path(value).is_absolute() else _home(env) / fallback


def paths(env: Mapping[str, str] | None = None, platform: str | None = None) -> Paths:
    """The folders for this user, read from the environment at call time."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if env.get(HOME_ENV):
        home = Path(env[HOME_ENV]).expanduser().absolute()
        return Paths(home, home, home / "run", home / "logs")
    if platform == "darwin":
        library = _home(env) / "Library"
        support = library / "Application Support" / APP
        return Paths(support, support, support / "run", library / "Logs" / APP)
    state = _xdg(env, "XDG_STATE_HOME", ".local/state") / APP
    return Paths(
        _xdg(env, "XDG_CONFIG_HOME", ".config") / APP,
        _xdg(env, "XDG_DATA_HOME", ".local/share") / APP,
        state / "run",
        state / "logs",
    )


__all__ = ["CONFIG_FILE", "DB_FILE", "HOME_ENV", "LEGACY_HOME", "Paths", "paths"]
