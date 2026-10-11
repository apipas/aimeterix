"""Configuration loader (records 0006 and 0013).

``aimeterix.yaml`` is the starting point and is safe to commit. Any setting can
be overridden by an environment variable named ``AIMETERIX_<SECTION>_<FIELD>``,
e.g. ``AIMETERIX_STORAGE_URL`` or ``AIMETERIX_AGENT_INTERVAL_S``. Secrets come
only from the environment or a never-committed ``.env`` file next to the YAML
file; a password written into the YAML file is refused.

After ``aimeterix init`` the database holds the live settings (record 0013);
this loader then only supplies the bootstrap values: storage URL, network
addresses and secrets.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from .errors import ConfigError

ENV_PREFIX = "AIMETERIX_"
CONFIG_ENV = "AIMETERIX_CONFIG"
DEFAULT_CONFIG_FILE = "aimeterix.yaml"
DEFAULT_STORAGE_URL = "sqlite:///~/.aimeterix/meter.db"


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StorageConfig(_Section):
    url: str = DEFAULT_STORAGE_URL
    raw_retention_days: int = Field(default=30, ge=1)
    rollup_retention_days: int = Field(default=365, ge=1)
    busy_timeout_ms: int = Field(default=5000, ge=0)


class AgentConfig(_Section):
    interval_s: float = Field(default=10.0, ge=1.0, le=300.0)


class ProxyConfig(_Section):
    host: str = "127.0.0.1"
    port: int = Field(default=8787, ge=1, le=65535)


class ApiConfig(_Section):
    host: str = "127.0.0.1"
    port: int = Field(default=8788, ge=1, le=65535)


class Config(_Section):
    storage: StorageConfig = StorageConfig()
    agent: AgentConfig = AgentConfig()
    proxy: ProxyConfig = ProxyConfig()
    api: ApiConfig = ApiConfig()


def load_config(
    path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> Config:
    """Load configuration from YAML, then ``.env``, then the environment.

    Later sources win. ``path`` defaults to ``$AIMETERIX_CONFIG`` or
    ``./aimeterix.yaml``; a missing default file means built-in defaults, while a
    missing explicit file is an error.
    """
    environ = dict(os.environ if env is None else env)
    explicit = path is not None or CONFIG_ENV in environ
    config_path = Path(path or environ.get(CONFIG_ENV) or DEFAULT_CONFIG_FILE).expanduser()

    data: dict[str, Any] = {}
    if config_path.is_file():
        data = _read_yaml(config_path)
        _refuse_secrets_in_file(data, config_path)
    elif explicit:
        raise ConfigError(f"config file not found: {config_path}")

    dotenv = read_dotenv(config_path.parent / ".env")
    overrides = _env_overrides({**dotenv, **environ})
    for section, values in overrides.items():
        current = data.get(section) or {}
        if not isinstance(current, dict):
            raise ConfigError(f"{config_path}: section '{section}' must be a mapping")
        data[section] = {**current, **values}

    try:
        return Config.model_validate(data)
    except PydanticValidationError as exc:
        raise ConfigError(_describe(exc)) from None


def read_dotenv(path: Path) -> dict[str, str]:
    """Parse a simple ``.env`` file: ``KEY=VALUE`` lines, ``#`` comments, optional quotes."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ConfigError(f"{path}:{number}: expected KEY=VALUE")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from None
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return loaded


def _refuse_secrets_in_file(data: Mapping[str, Any], path: Path) -> None:
    storage = data.get("storage")
    url = storage.get("url") if isinstance(storage, dict) else None
    if isinstance(url, str) and urlsplit(url).password:
        raise ConfigError(
            f"{path}: storage.url contains a password. Keep secrets out of the config file: "
            f"set {ENV_PREFIX}STORAGE_URL in the environment or in a .env file instead."
        )


def _env_overrides(environ: Mapping[str, str]) -> dict[str, dict[str, str]]:
    overrides: dict[str, dict[str, str]] = {}
    for section, info in Config.model_fields.items():
        model = info.annotation
        assert isinstance(model, type) and issubclass(model, BaseModel)
        for name in model.model_fields:
            key = f"{ENV_PREFIX}{section}_{name}".upper()
            if key in environ:
                overrides.setdefault(section, {})[name] = environ[key]
    return overrides


def _describe(exc: PydanticValidationError) -> str:
    lines = ["invalid configuration:"]
    for error in exc.errors():
        where = ".".join(str(part) for part in error["loc"])
        lines.append(f"  {where}: {error['msg']}")
    return "\n".join(lines)
