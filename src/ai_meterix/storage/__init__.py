"""Pluggable storage. Everything reads and writes through these two entry points.

Connectors are looked up by URL scheme in the ``ai_meterix.storage`` plugin
registry; built-in ones are registered here, third-party ones through entry
points (see ``ai_meterix.core.plugins``).
"""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from typing import Any

from ai_meterix.core.errors import PluginError
from ai_meterix.core.plugins import scheme_of, storage_connectors

from .base import (
    DIMENSIONS,
    MEASURES,
    Dataset,
    HealthStatus,
    HostInventory,
    RetentionPolicy,
    StorageReader,
    StorageWriter,
    Table,
    UsageQuery,
)
from .sqlite import SQLiteStorage

__all__ = [
    "DIMENSIONS",
    "MEASURES",
    "Dataset",
    "HealthStatus",
    "HostInventory",
    "RetentionPolicy",
    "StorageReader",
    "StorageWriter",
    "Table",
    "UsageQuery",
    "open_reader",
    "open_writer",
]

storage_connectors.register("sqlite", SQLiteStorage)
storage_connectors.register("postgresql", "ai_meterix.storage.postgres:PostgresStorage")
storage_connectors.register("postgres", "ai_meterix.storage.postgres:PostgresStorage")
storage_connectors.register("duckdb", "ai_meterix.storage.duckdb:DuckDBStorage")


def _factory(url: str) -> Callable[..., Any]:
    factory = storage_connectors.get(scheme_of(url))
    if isinstance(factory, str):  # built-in connector imported on first use
        module, _, attr = factory.partition(":")
        factory = getattr(import_module(module), attr)
    if not callable(factory):
        raise PluginError(f"storage plugin for {scheme_of(url)!r} is not callable")
    return factory


def open_reader(url: str, **options: Any) -> StorageReader:
    """Open a read-only connection (reports, the API ``user`` role)."""
    storage = _factory(url)(url, read_only=True, **options)
    if not isinstance(storage, StorageReader):
        raise PluginError(f"{type(storage).__name__} does not implement StorageReader")
    return storage


def open_writer(url: str, **options: Any) -> StorageWriter:
    """Open a read-write connection (agent, proxy, the API ``admin`` role)."""
    storage = _factory(url)(url, read_only=False, **options)
    if not isinstance(storage, StorageWriter):
        raise PluginError(f"{type(storage).__name__} does not implement StorageWriter")
    return storage
