"""AI providers: the catalog of where calls go and how they are priced (record 0018).

A provider is a route on the proxy. Apps call
``http://127.0.0.1:<proxy port>/<name><app_path>`` instead of the provider, and
the proxy forwards to ``<base_url><app_path>`` and records the model and token
counts. Each model's price per 1M tokens lives in ``model_prices`` and names
its provider; a model with no price costs 0.

The built-in catalog (``providers.json``) is copied into the database on the
first start of the proxy or API, together with any ``proxy.upstreams`` in the
config file. After that the database wins: the admin adds, edits, disables and
enables providers with ``aimeterix providers`` or the API, and the proxy picks
up a change within about a minute. Every change is logged with who and when.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import replace
from importlib import resources
from typing import Any
from urllib.parse import urlsplit

from ai_meterix.core.errors import ValidationError
from ai_meterix.core.model import Provider, ProviderKind
from ai_meterix.storage.base import StorageReader, StorageWriter

EDITABLE = ("base_url", "kind", "label", "app_path", "api_format", "enabled")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def catalog(text: str | None = None) -> list[Provider]:
    """The built-in providers shipped with AI Meterix, or ones given as JSON text."""
    if text is None:
        text = (
            resources.files("ai_meterix.pricing")
            .joinpath("providers.json")
            .read_text(encoding="utf-8")
        )
    data: dict[str, Any] = json.loads(text)
    return [
        Provider(
            str(p["name"]),
            str(p["base_url"]),
            ProviderKind(p.get("kind", "cloud")),
            str(p.get("label", "")),
            str(p.get("app_path", "")),
            str(p.get("format", "openai")),
            built_in=True,
        )
        for p in data["providers"]
    ]


def _kind_of(url: str) -> ProviderKind:
    host = urlsplit(url).hostname or ""
    return ProviderKind.LOCAL if host in LOCAL_HOSTS else ProviderKind.CLOUD


def seed_providers(
    storage: StorageWriter, upstreams: Mapping[str, str] | None = None, *, by: str = "init"
) -> list[str]:
    """Add the built-in providers and the config file's routes that the
    database does not have yet; returns their names. Existing ones are kept."""
    have = {p.name for p in storage.get_providers()}
    added = []
    for provider in catalog():
        if provider.name not in have:
            storage.save_provider(provider, by)
            added.append(provider.name)
            have.add(provider.name)
    for name, url in (upstreams or {}).items():
        if name not in have:
            storage.save_provider(Provider(name, url, _kind_of(url)), by)
            added.append(name)
            have.add(name)
    return added


def routes(providers: Iterable[Provider]) -> dict[str, str]:
    """Route name -> base URL for the enabled providers."""
    return {p.name: p.base_url for p in providers if p.enabled}


def local_names(providers: Iterable[Provider]) -> set[str]:
    """Providers whose models run on this machine (cost = their energy share)."""
    return {p.name for p in providers if p.kind is ProviderKind.LOCAL}


def app_url(provider: Provider, proxy: str = "http://127.0.0.1:8787") -> str:
    """The base URL an app uses to call this provider through the proxy."""
    return f"{proxy.rstrip('/')}/{provider.name}{provider.app_path}"


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ValidationError(f"enabled must be true or false, not {value!r}")


def add_provider(
    storage: StorageWriter, name: str, values: Mapping[str, Any], *, by: str
) -> Provider:
    """Add a provider of your own (a cloud API, or a model server on this or another machine)."""
    if any(p.name == name for p in storage.get_providers()):
        raise ValidationError(f"provider {name!r} exists; change it with `providers set`")
    url = values.get("base_url")
    if not url:
        raise ValidationError("a new provider needs its base URL (--url)")
    fields = {k: v for k, v in values.items() if v is not None and k in EDITABLE}
    fields.setdefault("kind", _kind_of(str(url)))
    if "enabled" in fields:
        fields["enabled"] = _flag(fields["enabled"])
    return storage.save_provider(Provider(name, **fields), by)


def set_provider(
    storage: StorageWriter, name: str, values: Mapping[str, Any], *, by: str
) -> Provider:
    """Change some fields of a provider; the others stay as they are."""
    current = {p.name: p for p in storage.get_providers()}.get(name)
    if current is None:
        raise ValidationError(f"no provider {name!r}; add it with `providers add`")
    fields = {k: v for k, v in values.items() if v is not None and k in EDITABLE}
    if "enabled" in fields:
        fields["enabled"] = _flag(fields["enabled"])
    if "kind" in fields:
        fields["kind"] = ProviderKind(fields["kind"])
    updated = replace(current, **fields)  # validates again
    return storage.save_provider(updated, by)


def providers_version(storage: StorageReader) -> tuple[tuple[str, int], ...]:
    """Changes whenever a provider is added or changed."""
    return tuple((p.name, p.version) for p in storage.get_providers())


__all__ = [
    "EDITABLE",
    "add_provider",
    "app_url",
    "catalog",
    "local_names",
    "providers_version",
    "routes",
    "seed_providers",
    "set_provider",
]
