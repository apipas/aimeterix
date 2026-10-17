"""SQLite connector: the zero-install default (records 0008, 0010, 0014).

- WAL mode so readers never block the writer, plus a busy timeout so the agent,
  proxy and reports can share one file and take turns writing.
- Each write call is one batched transaction (``BEGIN IMMEDIATE``).
- A reader opens the file with ``mode=ro`` and cannot change it.

URLs: ``sqlite:///relative/path.db``, ``sqlite:////absolute/path.db``,
``sqlite:///~/data/meter.db`` and ``sqlite:///:memory:``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import quote, urlsplit

from ai_meterix.core.errors import AiMeterixError, ValidationError

from .base import HealthStatus
from .sqlstore import SQLStorage


def parse_url(url: str) -> str:
    """Return the database path (or ``:memory:``) from a ``sqlite://`` URL."""
    parts = urlsplit(url)
    if parts.scheme.split("+", 1)[0].lower() != "sqlite":
        raise ValidationError(f"not a sqlite URL: {url!r}")
    if parts.netloc:
        raise ValidationError(
            f"sqlite URL must have no host, got {url!r} (use sqlite:///path or sqlite:////abs/path)"
        )
    path = parts.path[1:] if parts.path.startswith("/") else parts.path
    if not path:
        raise ValidationError(f"sqlite URL has no path: {url!r}")
    return path if path == ":memory:" else str(Path(path).expanduser())


class SQLiteStorage(SQLStorage):
    name = "sqlite"
    types: ClassVar[dict[str, str]] = {"BIGINT": "INTEGER", "DOUBLE": "REAL", "TEXT": "TEXT"}
    least = "MIN"
    greatest = "MAX"

    def __init__(self, url: str, *, read_only: bool = False, busy_timeout_ms: int = 5000) -> None:
        super().__init__()
        self.url = url
        self.read_only = read_only
        self.path = parse_url(url)
        if read_only:
            if self.path == ":memory:":
                raise ValidationError("a read-only connection needs a database file")
            if not Path(self.path).is_file():
                raise AiMeterixError(f"database not found: {self.path}")
            uri = f"file:{quote(str(Path(self.path).resolve()))}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        else:
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.isolation_level = None  # explicit transactions only
        self._conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        if not read_only:
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")

    def _driver_execute(self, sql: str, params: Sequence[Any]) -> list[tuple[Any, ...]]:
        return self._conn.execute(sql, params).fetchall()

    def _driver_executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        self._conn.executemany(sql, rows)

    @contextmanager
    def _driver_transaction(self) -> Iterator[None]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def _driver_close(self) -> None:
        self._conn.close()

    def _table_exists(self, table: str) -> bool:
        return bool(
            self._query("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,))
        )

    def health(self) -> HealthStatus:
        status = super().health()
        return HealthStatus(
            status.ok, status.backend, status.schema_version, status.detail or self.path
        )
