"""DuckDB connector for local analytics (records 0005, 0008).

Needs the optional extra: ``pip install ai-meterix[duckdb]``.

DuckDB lets only one process open a file for writing, so it suits reports and
single-process setups; the agent and proxy together should use SQLite or
Postgres (record 0008). Within one process, several connections share the
database and concurrent writes are retried when DuckDB reports a conflict.

URL: ``duckdb:///relative/path.duckdb``, ``duckdb:////absolute/path.duckdb`` or
``duckdb:///:memory:``. A reader is refused writes by this connector; DuckDB
cannot mix read-only and read-write connections to one file in one process.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit

from ai_meterix.core.errors import AiMeterixError, PluginError, ValidationError

from .sqlstore import SQLStorage

_VALUES = re.compile(r"VALUES \(\?(?:, \?)*\)")
_CHUNK = 500

try:
    import duckdb as _duckdb
except ImportError:  # pragma: no cover - depends on the installed extras
    _duckdb = None  # type: ignore[assignment]


def parse_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme.lower() != "duckdb" or parts.netloc:
        raise ValidationError(f"expected duckdb:///path, got {url!r}")
    path = parts.path[1:] if parts.path.startswith("/") else parts.path
    if not path:
        raise ValidationError(f"duckdb URL has no path: {url!r}")
    return path if path == ":memory:" else str(Path(path).expanduser())


class DuckDBStorage(SQLStorage):
    name = "duckdb"
    types: ClassVar[dict[str, str]] = {"BIGINT": "BIGINT", "DOUBLE": "DOUBLE", "TEXT": "VARCHAR"}

    def __init__(self, url: str, *, read_only: bool = False) -> None:
        if _duckdb is None:
            raise PluginError("the duckdb connector needs duckdb: pip install 'ai-meterix[duckdb]'")
        super().__init__()
        self.read_only = read_only
        self.path = parse_url(url)
        if self.path != ":memory:":
            if read_only and not Path(self.path).is_file():
                raise AiMeterixError(f"database not found: {self.path}")
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = _duckdb.connect(self.path)

    def _driver_execute(self, sql: str, params: Sequence[Any]) -> list[tuple[Any, ...]]:
        result = self._conn.execute(sql, list(params))
        return result.fetchall() if result.description else []

    def _driver_executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        # DuckDB's executemany runs row by row; one multi-row VALUES list per chunk
        # is much faster for batched sample writes.
        match = _VALUES.search(sql)
        if match is None:
            self._conn.executemany(sql, [list(row) for row in rows])
            return
        group = match.group(0)[len("VALUES ") :]
        for start in range(0, len(rows), _CHUNK):
            chunk = rows[start : start + _CHUNK]
            values = "VALUES " + ", ".join(group for _ in chunk)
            statement = sql[: match.start()] + values + sql[match.end() :]
            self._conn.execute(statement, [value for row in chunk for value in row])

    @contextmanager
    def _driver_transaction(self) -> Iterator[None]:
        self._conn.begin()
        try:
            yield
        except BaseException:
            self._conn.rollback()
            raise
        self._conn.commit()

    def _driver_close(self) -> None:
        self._conn.close()

    def _is_retryable(self, exc: Exception) -> bool:
        # Concurrent transactions touching the same key fail at commit in DuckDB
        # (optimistic concurrency); the whole transaction is safe to run again.
        return _duckdb is not None and isinstance(
            exc, (_duckdb.TransactionException, _duckdb.ConstraintException)
        )
