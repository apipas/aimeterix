"""Configuration loader (records 0006 and 0013).

``aimeterix.yaml`` is the starting point and is safe to commit; it lives in the
config folder (``core/paths.py``, record 0019), wherever the command is run. Any setting can
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
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic import ValidationError as PydanticValidationError

from .errors import ConfigError
from .paths import paths

ENV_PREFIX = "AIMETERIX_"
CONFIG_ENV = "AIMETERIX_CONFIG"
_ROUTE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
_KEY_HASH = re.compile(r"[0-9a-f]{64}")


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StorageConfig(_Section):
    url: str = Field(default_factory=lambda: paths().storage_url)
    raw_retention_days: int = Field(default=30, ge=1)
    rollup_retention_days: int = Field(default=365, ge=1)
    busy_timeout_ms: int = Field(default=5000, ge=0)


class AgentConfig(_Section):
    interval_s: float = Field(default=10.0, ge=1.0, le=300.0)


class ProxyConfig(_Section):
    """The LLM proxy (record 0011 item 4).

    ``upstreams`` maps a route name to a base URL; on the first start these are
    added to the providers in the database (record 0018), which also hold the
    built-in ones, so this is only needed for providers of your own. A client
    uses ``http://127.0.0.1:8787/<route>/v1``. ``default_upstream`` serves paths with
    no route name, e.g. a local llama.cpp server. ``allowed_key_hashes`` holds
    SHA-256 hashes of the API keys the proxy may forward (``aimeterix proxy
    --hash-key``); empty means any key.
    """

    host: str = "127.0.0.1"
    port: int = Field(default=8787, ge=1, le=65535)
    upstreams: dict[str, str] = Field(default_factory=dict)
    default_upstream: str | None = None
    allowed_key_hashes: tuple[str, ...] = ()

    @field_validator("upstreams")
    @classmethod
    def _check_upstreams(cls, value: dict[str, str]) -> dict[str, str]:
        for name, url in value.items():
            if not _ROUTE_NAME.fullmatch(name) or name == "app":
                raise ValueError(f"route name {name!r}: use lowercase letters, digits, - or _")
            _check_url(url)
        return value

    @field_validator("default_upstream")
    @classmethod
    def _check_default(cls, value: str | None) -> str | None:
        return None if not value else _check_url(value)

    @field_validator("allowed_key_hashes", mode="before")
    @classmethod
    def _split_hashes(cls, value: Any) -> Any:
        if isinstance(value, str):  # from the environment: comma-separated
            value = [part.strip() for part in value.split(",") if part.strip()]
        for item in value or ():
            if not isinstance(item, str) or not _KEY_HASH.fullmatch(item):
                raise ValueError("each entry must be a SHA-256 hex digest, not a key")
        return value


def _check_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"upstream {url!r} must be an http:// or https:// URL")
    return url.rstrip("/")


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
    ``aimeterix.yaml`` in the config folder; a missing default file means built-in
    defaults, while a missing explicit file is an error.
    """
    environ = dict(os.environ if env is None else env)
    explicit = path is not None or CONFIG_ENV in environ
    config_path = config_file(path, environ)

    data: dict[str, Any] = {}
    if config_path.is_file():
        data = _read_yaml(config_path)
        _refuse_secrets_in_file(data, config_path)
    elif explicit:
        raise ConfigError(f"config file not found: {config_path}")

    storage = data.setdefault("storage", {})
    if isinstance(storage, dict):
        storage.setdefault("url", paths(environ).storage_url)
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


def config_file(
    path: str | os.PathLike[str] | None = None, env: Mapping[str, str] | None = None
) -> Path:
    """The config file a command uses: ``path``, ``$AIMETERIX_CONFIG`` or the default."""
    environ = os.environ if env is None else env
    chosen = path or environ.get(CONFIG_ENV)
    return Path(chosen).expanduser() if chosen else paths(environ).config_file


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
