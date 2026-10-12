"""PostgreSQL connector for teams (records 0005, 0010 D1.2, 0014).

Needs the optional extra: ``pip install ai-meterix[postgres]`` (psycopg 3).
TimescaleDB works too; the schema uses plain tables.

URL: ``postgresql://user@host:5432/dbname`` (``postgres://`` and
``postgresql+psycopg://`` are accepted). Keep the password out of the config
file: set ``AIMETERIX_STORAGE_URL`` in the environment (record 0006).

A reader's session is ``READ ONLY``, so the database refuses its writes. For
the API ``user`` role, also give it a database user with only SELECT rights.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any, ClassVar
from urllib.parse import urlsplit, urlunsplit

from ai_meterix.core.errors import PluginError

from .sqlstore import SQLStorage

try:
    import psycopg
except ImportError:  # pragma: no cover - depends on the installed extras
    psycopg = None  # type: ignore[assignment]


def to_libpq_url(url: str) -> str:
    """Drop a driver suffix so libpq accepts the URL: ``postgresql+psycopg`` -> ``postgresql``."""
    parts = urlsplit(url)
    return urlunsplit(parts._replace(scheme="postgresql"))


class PostgresStorage(SQLStorage):
    name = "postgres"
    types: ClassVar[dict[str, str]] = {
        "BIGINT": "BIGINT",
        "DOUBLE": "DOUBLE PRECISION",
        "TEXT": "TEXT",
    }
    placeholder = "%s"

    def __init__(self, url: str, *, read_only: bool = False, connect_timeout_s: int = 10) -> None:
        if psycopg is None:
            raise PluginError(
                "the postgres connector needs psycopg: pip install 'ai-meterix[postgres]'"
            )
        super().__init__()
        self.read_only = read_only
        self._conn = psycopg.connect(
            to_libpq_url(url), autocommit=True, connect_timeout=connect_timeout_s
        )
        if read_only:
            self._conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")

    def _driver_execute(self, sql: str, params: Sequence[Any]) -> list[tuple[Any, ...]]:
        cursor = self._conn.execute(sql, params)
        return cursor.fetchall() if cursor.description else []

    def _driver_executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        with self._conn.cursor() as cursor:
            cursor.executemany(sql, rows)

    @contextmanager
    def _driver_transaction(self) -> Iterator[None]:
        with self._conn.transaction():
            yield

    def _driver_close(self) -> None:
        self._conn.close()

    def _table_exists(self, table: str) -> bool:
        return bool(
            self._query(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = ?",
                (table,),
            )
        )

    def _is_retryable(self, exc: Exception) -> bool:
        return psycopg is not None and isinstance(
            exc, (psycopg.errors.SerializationFailure, psycopg.errors.DeadlockDetected)
        )
