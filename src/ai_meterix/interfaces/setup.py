"""``aimeterix init`` and ``aimeterix uninstall`` (records 0016 and 0017).

``init`` without a config file writes a commented starter file and stops. With
one, it validates it, shows what it will create and asks for confirmation; then
it creates the database, the two accounts (password = name, changed at first
login), the live settings and the built-in providers. An existing database is
never initialised twice: change it with ``aimeterix config``.

``uninstall`` stops everything; with ``--purge`` it also deletes the AI Meterix
folders (config, database, logs, buffers; ``core/paths.py``, record 0019).
Nothing else on the machine needs cleaning, except a database you put somewhere
else, which is named, not deleted. ``aimeterix paths`` prints every folder.
"""

from __future__ import annotations

import shutil
from importlib import resources
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from ai_meterix.core.config import Config
from ai_meterix.core.paths import LEGACY_HOME, Paths, paths


def starter_text() -> str:
    return resources.files("ai_meterix.interfaces").joinpath("starter.yaml").read_text("utf-8")


def write_starter(path: Path) -> None:
    """Write the starter file; never overwrites."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8") as f:
        f.write(starter_text())


def safe_url(url: str) -> str:
    """The storage URL without its password."""
    parts = urlsplit(url)
    if not parts.password:
        return url
    netloc = f"{parts.username}:***@{parts.hostname}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit(parts._replace(netloc=netloc))


def summary(config: Config, config_path: Path) -> list[str]:
    from ai_meterix.pricing.providers import catalog

    proxy, api = config.proxy, config.api
    lines = [
        f"Config file    {config_path}",
        f"Database       {safe_url(config.storage.url)}",
        f"Agent          a sample every {config.agent.interval_s:g} s",
        f"Retention      raw data {config.storage.raw_retention_days} days, "
        f"hourly totals {config.storage.rollup_retention_days} days",
        f"Proxy          http://{proxy.host}:{proxy.port}",
        f"Providers      {len(catalog())} built in"
        + (f" + {len(proxy.upstreams)} from the file" if proxy.upstreams else ""),
        f"Local server   {proxy.default_upstream or 'none (set proxy.default_upstream)'}",
        f"Dashboard      http://{api.host}:{api.port}/",
        "Accounts       user and admin; the first password is the name, changed at first login",
    ]
    return lines


def sqlite_path(url: str) -> Path | None:
    if not url.startswith("sqlite"):
        return None
    from ai_meterix.storage.sqlite import parse_url

    path = parse_url(url)
    return None if path in ("", ":memory:") else Path(path).expanduser()


def legacy_home() -> Path | None:
    """The ``~/.aimeterix`` folder of builds before 0.1.0, if it is still there."""
    old = LEGACY_HOME.expanduser()
    return old if old.is_dir() and old not in paths().folders() else None


def where_lines(where: Paths, config_path: Path) -> list[str]:
    def mark(path: Path) -> str:
        return f"{path}" + ("" if path.exists() else "  (not created yet)")

    return [
        f"Config file    {mark(config_path)}",
        f"Secrets        {mark(config_path.parent / '.env')}",
        f"Data           {mark(where.data_dir)}",
        f"Logs           {mark(where.log_dir)}",
        f"Process IDs    {mark(where.run_dir)}",
    ]


def purge(where: Paths | None = None) -> list[Path]:
    """Delete every AI Meterix folder; returns the ones that were there.

    A folder that holds the home folder (a mistaken ``AIMETERIX_HOME=~``) is refused.
    """
    folders = (where or paths()).folders()
    home = Path.home().resolve()
    unsafe = [f for f in folders if home.is_relative_to(f.resolve())]
    if unsafe:
        raise ValueError(f"refusing to delete {unsafe[0]}: it holds your home folder")
    deleted = []
    for folder in folders:
        if folder.exists():
            shutil.rmtree(folder)
            deleted.append(folder)
    return deleted


__all__ = [
    "legacy_home",
    "purge",
    "safe_url",
    "sqlite_path",
    "starter_text",
    "summary",
    "where_lines",
    "write_starter",
]
