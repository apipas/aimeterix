"""Live settings in the database (record 0013 items 4 and 5).

The first start of the agent, proxy or API copies the live settings from
``aimeterix.yaml`` (and the environment) into the database; from then on the
database wins and the admin changes them with ``aimeterix config set`` or the
API. Only bootstrap settings stay in the file or environment: the database URL,
addresses and ports, and secrets.

The agent and proxy re-read the settings about every 60 s (``SettingsWatcher``)
and apply changes without a restart. Every change is logged with who and when.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

import yaml
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from .config import Config

if TYPE_CHECKING:
    from ai_meterix.storage.base import StorageReader, StorageWriter

log = logging.getLogger("ai_meterix.settings")
RELOAD_S = 60.0

# key -> (section, field); everything else in Config is bootstrap only
LIVE: dict[str, tuple[str, str]] = {
    "agent.interval_s": ("agent", "interval_s"),
    "storage.raw_retention_days": ("storage", "raw_retention_days"),
    "storage.rollup_retention_days": ("storage", "rollup_retention_days"),
    "proxy.upstreams": ("proxy", "upstreams"),
    "proxy.default_upstream": ("proxy", "default_upstream"),
    "proxy.allowed_key_hashes": ("proxy", "allowed_key_hashes"),
}


def _plain(value: Any) -> Any:
    return list(value) if isinstance(value, tuple) else value


def live_values(config: Config) -> dict[str, Any]:
    """The live settings as they are in ``config``."""
    return {
        key: _plain(getattr(getattr(config, section), field))
        for key, (section, field) in LIVE.items()
    }


def _with(config: Config, values: Mapping[str, Any]) -> Config:
    """``config`` with the live ``values`` applied, validated like the YAML file."""
    sections: dict[str, dict[str, Any]] = {}
    for key, value in values.items():
        section, field = LIVE[key]
        sections.setdefault(section, getattr(config, section).model_dump())[field] = value
    try:
        updates: dict[str, BaseModel] = {
            name: type(getattr(config, name)).model_validate(data)
            for name, data in sections.items()
        }
    except PydanticValidationError as exc:
        error = exc.errors()[0]
        where = ".".join(str(p) for p in error["loc"][:1])
        raise ValueError(f"{where or 'value'}: {error['msg']}") from None
    return config.model_copy(update=updates)


def seed_settings(storage: StorageWriter, config: Config, *, by: str = "init") -> list[str]:
    """Copy the live settings missing from the database out of ``config``."""
    saved = storage.get_settings()
    added = []
    for key, value in live_values(config).items():
        if key not in saved:
            storage.save_setting(key, value, by=by)
            added.append(key)
    return added


def effective(config: Config, storage: StorageReader) -> tuple[Config, list[str]]:
    """``config`` with the database's live settings applied, and a note for
    each one the file or environment sets differently (the database wins)."""
    saved = {k: s.value for k, s in storage.get_settings().items() if k in LIVE}
    file_values = live_values(config)
    notes = [
        f"{key}: the database has {json.dumps(value)}, the config file "
        f"{json.dumps(file_values[key])}; using the database (aimeterix config set)"
        for key, value in saved.items()
        if value != file_values[key]
    ]
    return _with(config, saved), notes


def parse_value(key: str, text: str) -> Any:
    """A command-line value: JSON when it parses (numbers, ``null``, objects), else text."""
    if key not in LIVE:
        raise ValueError(f"unknown setting {key!r}; settings: {', '.join(LIVE)}")
    try:
        return json.loads(text)
    except ValueError:
        return text


def set_setting(storage: StorageWriter, config: Config, key: str, value: Any, *, by: str) -> Any:
    """Validate and save one live setting; returns the value as stored."""
    if key not in LIVE:
        raise ValueError(f"unknown setting {key!r}; settings: {', '.join(LIVE)}")
    current, _ = effective(config, storage)
    checked = live_values(_with(current, {key: value}))[key]
    storage.save_setting(key, checked, by=by)
    return checked


def reset_setting(storage: StorageWriter, config: Config, key: str, *, by: str) -> Any:
    """Put a live setting back to its built-in default."""
    if key not in LIVE:
        raise ValueError(f"unknown setting {key!r}; settings: {', '.join(LIVE)}")
    return set_setting(storage, config, key, live_values(Config())[key], by=by)


def settings_version(storage: StorageReader) -> tuple[int, ...]:
    """Changes whenever a live setting changes."""
    saved = storage.get_settings()
    return tuple(saved[k].version if k in saved else 0 for k in LIVE)


def export_yaml(config: Config) -> str:
    """The whole configuration as YAML, without passwords."""
    data = json.loads(config.model_dump_json())
    url = urlsplit(data["storage"]["url"])
    if url.password:
        netloc = f"{url.username}@{url.hostname}" + (f":{url.port}" if url.port else "")
        data["storage"]["url"] = urlunsplit(url._replace(netloc=netloc))
    head = (
        "# AI Meterix configuration, exported from the database.\n"
        "# Secrets are left out: set them in the environment (AIMETERIX_STORAGE_URL).\n"
    )
    return head + yaml.safe_dump(data, sort_keys=False)


class SettingsWatcher:
    """Re-reads the live settings every ``every_s`` and calls ``apply`` on a change.

    ``pinned`` keys were given on the command line for this run and are left
    alone; a failed read is logged and retried at the next check.
    """

    def __init__(
        self,
        open_reader: Callable[[], StorageReader],
        config: Config,
        apply: Callable[[Config, list[str]], None],
        *,
        version: tuple[int, ...] | None = None,
        every_s: float = RELOAD_S,
        pinned: Iterable[str] = (),
    ) -> None:
        self._open = open_reader
        self.config = config
        self._apply = apply
        self.version = version
        self.every_s = every_s
        self.pinned = set(pinned)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def check(self) -> list[str]:
        """Read the settings once; returns the keys that changed and were applied."""
        storage = self._open()
        try:
            version = settings_version(storage)
            if version == self.version:
                return []
            new, _ = effective(self.config, storage)
        finally:
            storage.close()
        self.version = version
        before, after = live_values(self.config), live_values(new)
        changed = [k for k in LIVE if before[k] != after[k] and k not in self.pinned]
        self.config = new
        for key in changed:
            log.info("setting %s is now %s", key, json.dumps(after[key]))
        if changed:
            self._apply(new, changed)
        return changed

    def _run(self) -> None:
        while not self._stop.wait(self.every_s):
            try:
                self.check()
            except Exception as exc:  # the database may be busy or away for a while
                log.warning("could not read the settings: %s", exc)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="aimeterix-settings", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


__all__ = [
    "LIVE",
    "SettingsWatcher",
    "effective",
    "export_yaml",
    "live_values",
    "parse_value",
    "reset_setting",
    "seed_settings",
    "set_setting",
    "settings_version",
]
