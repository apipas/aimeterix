"""SQLite connector: the zero-install default (records 0008, 0010, 0014).

- WAL mode so readers never block the writer, plus a busy timeout so the agent,
  proxy and reports can share one file and take turns writing.
- Each write call is one batched transaction.
- Timestamps are integer epoch milliseconds (UTC); money is stored as text so
  decimals stay exact; JSON fields are text.
- A reader opens the file with ``mode=ro`` and cannot change it.

URLs: ``sqlite:///relative/path.db``, ``sqlite:////absolute/path.db``,
``sqlite:///~/.aimeterix/meter.db`` and ``sqlite:///:memory:``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import quote, urlsplit

from ai_meterix.core.errors import AiMeterixError, ValidationError
from ai_meterix.core.model import (
    Caller,
    CostProfile,
    Device,
    DeviceSample,
    Host,
    LlmCall,
    LocalModel,
    MemoryPool,
    MemorySample,
    ModelPrice,
    PriceChange,
    ProcessSample,
    Project,
    Setting,
    SettingChange,
    User,
)
from ai_meterix.core.timeutil import from_epoch_ms, to_epoch_ms, utc_now

from . import rollup as ru
from .base import HealthStatus, HostInventory, RetentionPolicy, Table, UsageQuery
from .sql import build_query

T = TypeVar("T")

ROLLUP_LOOKBACK_MS = 2 * ru.HOUR_MS  # late samples within this window still reach rollups

MIGRATIONS: tuple[str, ...] = (
    # 1: initial schema
    """
    CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL);
    CREATE TABLE hosts (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL, hostname TEXT NOT NULL,
        os TEXT NOT NULL, arch TEXT NOT NULL, chip TEXT, tags TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE memory_pools (
        id TEXT PRIMARY KEY, host_id TEXT NOT NULL, type TEXT NOT NULL, total_bytes INTEGER
    );
    CREATE TABLE devices (
        id TEXT PRIMARY KEY, host_id TEXT NOT NULL, kind TEXT NOT NULL, model TEXT,
        memory_pool_id TEXT
    );
    CREATE TABLE device_samples (
        ts INTEGER NOT NULL, device_id TEXT NOT NULL, interval_s REAL NOT NULL,
        util_pct REAL, power_w REAL, source TEXT NOT NULL, extra TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX device_samples_ts ON device_samples (ts, device_id);
    CREATE TABLE memory_samples (
        ts INTEGER NOT NULL, pool_id TEXT NOT NULL, interval_s REAL NOT NULL,
        used_bytes INTEGER, wired_bytes INTEGER, compressed_bytes INTEGER, swap_bytes INTEGER,
        gpu_allocated_bytes INTEGER, pressure TEXT, source TEXT NOT NULL,
        extra TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX memory_samples_ts ON memory_samples (ts, pool_id);
    CREATE TABLE process_samples (
        ts INTEGER NOT NULL, host_id TEXT NOT NULL, process_name TEXT NOT NULL,
        pid INTEGER NOT NULL, interval_s REAL NOT NULL, cpu_time_s REAL, gpu_time_s REAL,
        attributed_energy_j REAL, local_model_id TEXT, source TEXT NOT NULL
    );
    CREATE INDEX process_samples_ts ON process_samples (ts, host_id);
    CREATE TABLE local_models (
        id TEXT PRIMARY KEY, host_id TEXT NOT NULL, runtime TEXT NOT NULL,
        model_name TEXT NOT NULL, first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL,
        size_bytes INTEGER, pid INTEGER
    );
    CREATE TABLE callers (
        id TEXT PRIMARY KEY, app TEXT NOT NULL, feature TEXT, tags TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE llm_calls (
        ts INTEGER NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
        caller_id TEXT NOT NULL, call_type TEXT NOT NULL, request_id TEXT,
        input_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER,
        cache_read_tokens INTEGER, cache_write_tokens INTEGER, latency_s REAL,
        status INTEGER, file_bytes INTEGER, raw_usage TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX llm_calls_ts ON llm_calls (ts, model);
    CREATE TABLE cost_profiles (
        host_id TEXT NOT NULL, version INTEGER NOT NULL, saved_at INTEGER NOT NULL,
        saved_by TEXT NOT NULL, name TEXT NOT NULL, region TEXT, currency TEXT NOT NULL,
        energy_rate_per_kwh TEXT NOT NULL, pue TEXT NOT NULL,
        fixed_fees_per_month TEXT NOT NULL, fee_spread TEXT NOT NULL,
        PRIMARY KEY (host_id, version)
    );
    CREATE TABLE model_prices (
        model TEXT NOT NULL, valid_from INTEGER NOT NULL, source TEXT NOT NULL,
        input_per_m TEXT NOT NULL, output_per_m TEXT NOT NULL, cache_read_per_m TEXT NOT NULL,
        cache_write_per_m TEXT NOT NULL, valid_to INTEGER, price_set INTEGER NOT NULL,
        PRIMARY KEY (model, valid_from)
    );
    CREATE TABLE price_changes (
        ts INTEGER NOT NULL, by_user TEXT NOT NULL, model TEXT NOT NULL,
        field_name TEXT NOT NULL, old TEXT, new TEXT
    );
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, version INTEGER NOT NULL);
    CREATE TABLE setting_changes (
        ts INTEGER NOT NULL, by_user TEXT NOT NULL, key TEXT NOT NULL, old TEXT, new TEXT
    );
    CREATE TABLE users (
        name TEXT PRIMARY KEY, role TEXT NOT NULL, password_hash TEXT NOT NULL,
        must_change INTEGER NOT NULL
    );
    CREATE TABLE device_hourly (
        ts INTEGER NOT NULL, device_id TEXT NOT NULL, samples INTEGER NOT NULL,
        seconds REAL NOT NULL, energy_j REAL, busy_s REAL, active_s REAL, util_s REAL,
        util_min REAL, util_max REAL, util_p95 REAL, power_s REAL, power_min REAL,
        power_max REAL, power_p95 REAL, PRIMARY KEY (ts, device_id)
    );
    CREATE TABLE memory_hourly (
        ts INTEGER NOT NULL, pool_id TEXT NOT NULL, samples INTEGER NOT NULL,
        seconds REAL NOT NULL, used_byte_s REAL, used_s REAL, used_min INTEGER,
        used_max INTEGER, used_p95 INTEGER, PRIMARY KEY (ts, pool_id)
    );
    CREATE TABLE process_hourly (
        ts INTEGER NOT NULL, host_id TEXT NOT NULL, process_name TEXT NOT NULL,
        local_model_id TEXT, samples INTEGER NOT NULL, seconds REAL NOT NULL,
        cpu_time_s REAL, gpu_time_s REAL, energy_j REAL
    );
    CREATE INDEX process_hourly_ts ON process_hourly (ts);
    CREATE TABLE calls_hourly (
        ts INTEGER NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
        call_type TEXT NOT NULL, caller_id TEXT NOT NULL, requests INTEGER NOT NULL,
        input_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER,
        cache_read_tokens INTEGER, cache_write_tokens INTEGER, latency_sum REAL,
        latency_n INTEGER NOT NULL, errors INTEGER NOT NULL,
        PRIMARY KEY (ts, provider, model, call_type, caller_id)
    );
    """,
)
LATEST_VERSION = len(MIGRATIONS)

_RAW_TABLES = ("device_samples", "memory_samples", "process_samples", "llm_calls")
_HOURLY_TABLES = ("device_hourly", "memory_hourly", "process_hourly", "calls_hourly")


class SchemaError(AiMeterixError):
    """The database schema is missing, or newer than this version understands."""


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _ms(ts: datetime | None) -> int | None:
    return None if ts is None else to_epoch_ms(ts)


def _dt(ms: int | None) -> datetime | None:
    return None if ms is None else from_epoch_ms(ms)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


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


class SQLiteStorage:
    """``StorageWriter`` (or, with ``read_only=True``, ``StorageReader``) on one SQLite file."""

    name = "sqlite"

    def __init__(self, url: str, *, read_only: bool = False, busy_timeout_ms: int = 5000) -> None:
        self.url = url
        self.read_only = read_only
        self.path = parse_url(url)
        self._lock = threading.RLock()
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

    # --- plumbing --------------------------------------------------------------

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        if self.read_only:
            raise AiMeterixError("this storage connection is read only")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _rows(self, sql: str, params: Iterable[Any] = ()) -> list[tuple[Any, ...]]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)))

    def _require_schema(self) -> None:
        version = self.schema_version()
        if version is None:
            raise SchemaError("database has no AI Meterix schema; run migrate() (aimeterix init)")
        if version > LATEST_VERSION:
            raise SchemaError(
                f"database schema v{version} is newer than this AI Meterix (v{LATEST_VERSION})"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> SQLiteStorage:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- schema ----------------------------------------------------------------

    def schema_version(self) -> int | None:
        rows = self._rows(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_meta'"
        )
        if not rows:
            return None
        found = self._rows("SELECT value FROM schema_meta WHERE key = 'schema_version'")
        return int(found[0][0]) if found else None

    def migrate(self, target: int | None = None) -> int:
        """Create or upgrade the schema, forward only. Returns the resulting version."""
        target = LATEST_VERSION if target is None else target
        if not 1 <= target <= LATEST_VERSION:
            raise ValidationError(f"unknown schema version {target}")
        with self._write() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT)"
            )
            found = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            current = int(found[0]) if found else 0
            if current > LATEST_VERSION:
                raise SchemaError(
                    f"database schema v{current} is newer than this AI Meterix (v{LATEST_VERSION})"
                )
            for version in range(current + 1, target + 1):
                for statement in MIGRATIONS[version - 1].split(";"):
                    if statement.strip():
                        conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                    (str(version),),
                )
            return max(current, target)

    def health(self) -> HealthStatus:
        try:
            version = self.schema_version()
            self._rows("SELECT 1")
        except sqlite3.Error as exc:
            return HealthStatus(False, self.name, None, f"database error: {exc}")
        if version is None:
            return HealthStatus(False, self.name, None, "schema missing; run aimeterix init")
        if version != LATEST_VERSION:
            return HealthStatus(
                False, self.name, version, f"schema v{version}, expected v{LATEST_VERSION}"
            )
        return HealthStatus(True, self.name, version, self.path)

    # --- reads -----------------------------------------------------------------

    def query(self, q: UsageQuery) -> Table:
        self._require_schema()
        sql, params = build_query(q)
        rows = self._rows(sql, params)
        first_measure = (1 if q.bucket is not None else 0) + len(q.group_by)
        out: list[tuple[Any, ...]] = []
        for row in rows:
            if not row[first_measure]:  # no samples or calls in this group
                continue
            if q.bucket is not None:
                row = (from_epoch_ms(row[0]), *row[1:])
            out.append(tuple(row))
        return Table(q.columns, tuple(out))

    def get_projects(self) -> list[Project]:
        return [Project(*row) for row in self._rows("SELECT id, name FROM projects ORDER BY id")]

    def get_hosts(self) -> list[HostInventory]:
        pools: dict[str, list[MemoryPool]] = {}
        for row in self._rows(
            "SELECT id, host_id, type, total_bytes FROM memory_pools ORDER BY id"
        ):
            pools.setdefault(row[1], []).append(MemoryPool(*row))
        devices: dict[str, list[Device]] = {}
        for row in self._rows(
            "SELECT id, host_id, kind, model, memory_pool_id FROM devices ORDER BY id"
        ):
            devices.setdefault(row[1], []).append(Device(*row))
        hosts = []
        for hid, pid, hostname, os_, arch, chip, tags in self._rows(
            "SELECT id, project_id, hostname, os, arch, chip, tags FROM hosts ORDER BY id"
        ):
            host = Host(hid, pid, hostname, os_, arch, chip, json.loads(tags))
            hosts.append(
                HostInventory(host, tuple(pools.get(hid, ())), tuple(devices.get(hid, ())))
            )
        return hosts

    def get_callers(self) -> list[Caller]:
        return [
            Caller(cid, app, feature, json.loads(tags))
            for cid, app, feature, tags in self._rows(
                "SELECT id, app, feature, tags FROM callers ORDER BY id"
            )
        ]

    def get_local_models(self, host_id: str | None = None) -> list[LocalModel]:
        sql = (
            "SELECT id, host_id, runtime, model_name, first_seen, last_seen, size_bytes, pid "
            "FROM local_models"
        )
        params: tuple[Any, ...] = ()
        if host_id is not None:
            sql += " WHERE host_id = ?"
            params = (host_id,)
        return [
            LocalModel(
                mid, hid, runtime, name, from_epoch_ms(first), from_epoch_ms(last), size, pid
            )
            for mid, hid, runtime, name, first, last, size, pid in self._rows(
                sql + " ORDER BY id", params
            )
        ]

    _PROFILE_COLUMNS = (
        "host_id, name, currency, energy_rate_per_kwh, region, pue, fixed_fees_per_month, "
        "fee_spread, version, saved_at"
    )

    @staticmethod
    def _profile(row: tuple[Any, ...]) -> CostProfile:
        host_id, name, currency, rate, region, pue, fees, spread, version, saved_at = row
        return CostProfile(
            host_id,
            name,
            currency,
            Decimal(rate),
            region,
            Decimal(pue),
            Decimal(fees),
            spread,
            version,
            from_epoch_ms(saved_at),
        )

    def get_profile(self, host_id: str, at: datetime | None = None) -> CostProfile | None:
        """The profile in force at ``at`` (default: now): the last version saved by then."""
        rows = self._rows(
            f"SELECT {self._PROFILE_COLUMNS} FROM cost_profiles "
            "WHERE host_id = ? AND saved_at <= ? ORDER BY version DESC LIMIT 1",
            (host_id, to_epoch_ms(at or utc_now())),
        )
        return self._profile(rows[0]) if rows else None

    def get_profile_history(self, host_id: str) -> list[CostProfile]:
        return [
            self._profile(row)
            for row in self._rows(
                f"SELECT {self._PROFILE_COLUMNS} FROM cost_profiles WHERE host_id = ? "
                "ORDER BY version",
                (host_id,),
            )
        ]

    @staticmethod
    def _price(row: tuple[Any, ...]) -> ModelPrice:
        model, valid_from, source, inp, out, cread, cwrite, valid_to, price_set = row
        return ModelPrice(
            model,
            source,
            from_epoch_ms(valid_from),
            Decimal(inp),
            Decimal(out),
            Decimal(cread),
            Decimal(cwrite),
            _dt(valid_to),
            bool(price_set),
        )

    def get_prices(self, model: str | None = None) -> list[ModelPrice]:
        sql = (
            "SELECT model, valid_from, source, input_per_m, output_per_m, cache_read_per_m, "
            "cache_write_per_m, valid_to, price_set FROM model_prices"
        )
        params: tuple[Any, ...] = ()
        if model is not None:
            sql += " WHERE model = ?"
            params = (model,)
        return [self._price(row) for row in self._rows(sql + " ORDER BY model, valid_from", params)]

    def get_price_changes(self, model: str | None = None) -> list[PriceChange]:
        sql = "SELECT ts, by_user, model, field_name, old, new FROM price_changes"
        params: tuple[Any, ...] = ()
        if model is not None:
            sql += " WHERE model = ?"
            params = (model,)
        return [
            PriceChange(from_epoch_ms(ts), by, m, f, old, new)
            for ts, by, m, f, old, new in self._rows(sql + " ORDER BY rowid", params)
        ]

    def get_settings(self) -> dict[str, Setting]:
        return {
            key: Setting(key, json.loads(value), version)
            for key, value, version in self._rows(
                "SELECT key, value, version FROM settings ORDER BY key"
            )
        }

    def get_setting_changes(self, key: str | None = None) -> list[SettingChange]:
        sql = "SELECT ts, by_user, key, old, new FROM setting_changes"
        params: tuple[Any, ...] = ()
        if key is not None:
            sql += " WHERE key = ?"
            params = (key,)
        return [
            SettingChange(from_epoch_ms(ts), by, k, old, new)
            for ts, by, k, old, new in self._rows(sql + " ORDER BY rowid", params)
        ]

    def get_user(self, name: str) -> User | None:
        rows = self._rows(
            "SELECT name, role, password_hash, must_change FROM users WHERE name = ?", (name,)
        )
        if not rows:
            return None
        uname, role, password_hash, must_change = rows[0]
        return User(uname, role, password_hash, bool(must_change))

    # --- writes ----------------------------------------------------------------

    def _write_many(
        self, sql: str, items: Iterable[T], to_row: Callable[[T], tuple[Any, ...]]
    ) -> int:
        rows = [to_row(item) for item in items]
        if not rows:
            return 0
        with self._write() as conn:
            conn.executemany(sql, rows)
        return len(rows)

    def upsert_projects(self, projects: Iterable[Project]) -> None:
        self._write_many(
            "INSERT INTO projects (id, name) VALUES (?, ?) "
            "ON CONFLICT (id) DO UPDATE SET name = excluded.name",
            projects,
            lambda p: (p.id, p.name),
        )

    def upsert_hosts(self, hosts: Iterable[HostInventory]) -> None:
        inventories = list(hosts)
        if not inventories:
            return
        with self._write() as conn:
            for inv in inventories:
                h = inv.host
                conn.execute(
                    "INSERT INTO hosts (id, project_id, hostname, os, arch, chip, tags) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET "
                    "project_id = excluded.project_id, hostname = excluded.hostname, "
                    "os = excluded.os, arch = excluded.arch, chip = excluded.chip, "
                    "tags = excluded.tags",
                    (h.id, h.project_id, h.hostname, h.os, h.arch, h.chip, _json(h.tags)),
                )
                conn.executemany(
                    "INSERT INTO memory_pools (id, host_id, type, total_bytes) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (id) DO UPDATE SET host_id = excluded.host_id, "
                    "type = excluded.type, total_bytes = excluded.total_bytes",
                    [(p.id, p.host_id, p.type.value, p.total_bytes) for p in inv.pools],
                )
                conn.executemany(
                    "INSERT INTO devices (id, host_id, kind, model, memory_pool_id) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET "
                    "host_id = excluded.host_id, kind = excluded.kind, model = excluded.model, "
                    "memory_pool_id = excluded.memory_pool_id",
                    [
                        (d.id, d.host_id, d.kind.value, d.model, d.memory_pool_id)
                        for d in inv.devices
                    ],
                )

    def upsert_callers(self, callers: Iterable[Caller]) -> None:
        self._write_many(
            "INSERT INTO callers (id, app, feature, tags) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET app = excluded.app, feature = excluded.feature, "
            "tags = excluded.tags",
            callers,
            lambda c: (c.id, c.app, c.feature, _json(c.tags)),
        )

    def upsert_local_models(self, models: Iterable[LocalModel]) -> None:
        self._write_many(
            "INSERT INTO local_models "
            "(id, host_id, runtime, model_name, first_seen, last_seen, size_bytes, pid) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET "
            "first_seen = MIN(first_seen, excluded.first_seen), "
            "last_seen = MAX(last_seen, excluded.last_seen), "
            "size_bytes = COALESCE(excluded.size_bytes, size_bytes), pid = excluded.pid",
            models,
            lambda m: (
                m.id,
                m.host_id,
                m.runtime,
                m.model_name,
                to_epoch_ms(m.first_seen),
                to_epoch_ms(m.last_seen),
                m.size_bytes,
                m.pid,
            ),
        )

    def write_device_samples(self, samples: Iterable[DeviceSample]) -> int:
        return self._write_many(
            "INSERT INTO device_samples (ts, device_id, interval_s, util_pct, power_w, source, "
            "extra) VALUES (?, ?, ?, ?, ?, ?, ?)",
            samples,
            lambda s: (
                to_epoch_ms(s.ts),
                s.device_id,
                s.interval_s,
                s.util_pct,
                s.power_w,
                s.source.value,
                _json(s.extra),
            ),
        )

    def write_memory_samples(self, samples: Iterable[MemorySample]) -> int:
        return self._write_many(
            "INSERT INTO memory_samples (ts, pool_id, interval_s, used_bytes, wired_bytes, "
            "compressed_bytes, swap_bytes, gpu_allocated_bytes, pressure, source, extra) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            samples,
            lambda s: (
                to_epoch_ms(s.ts),
                s.pool_id,
                s.interval_s,
                s.used_bytes,
                s.wired_bytes,
                s.compressed_bytes,
                s.swap_bytes,
                s.gpu_allocated_bytes,
                None if s.pressure is None else s.pressure.value,
                s.source.value,
                _json(s.extra),
            ),
        )

    def write_process_samples(self, samples: Iterable[ProcessSample]) -> int:
        return self._write_many(
            "INSERT INTO process_samples (ts, host_id, process_name, pid, interval_s, cpu_time_s, "
            "gpu_time_s, attributed_energy_j, local_model_id, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            samples,
            lambda s: (
                to_epoch_ms(s.ts),
                s.host_id,
                s.process_name,
                s.pid,
                s.interval_s,
                s.cpu_time_s,
                s.gpu_time_s,
                s.attributed_energy_j,
                s.local_model_id,
                s.source.value,
            ),
        )

    def write_calls(self, calls: Iterable[LlmCall]) -> int:
        return self._write_many(
            "INSERT INTO llm_calls (ts, provider, model, caller_id, call_type, request_id, "
            "input_tokens, output_tokens, reasoning_tokens, cache_read_tokens, "
            "cache_write_tokens, latency_s, status, file_bytes, raw_usage) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            calls,
            lambda c: (
                to_epoch_ms(c.ts),
                c.provider,
                c.model,
                c.caller_id,
                c.call_type.value,
                c.request_id,
                c.input_tokens,
                c.output_tokens,
                c.reasoning_tokens,
                c.cache_read_tokens,
                c.cache_write_tokens,
                c.latency_s,
                c.status,
                c.file_bytes,
                _json(c.raw_usage),
            ),
        )

    def save_profile(self, profile: CostProfile, by: str) -> CostProfile:
        """Save a new version; the change log records each changed field."""
        now = utc_now()
        fields = (
            "name",
            "currency",
            "energy_rate_per_kwh",
            "region",
            "pue",
            "fixed_fees_per_month",
            "fee_spread",
        )
        with self._write() as conn:
            row = conn.execute(
                f"SELECT {self._PROFILE_COLUMNS} FROM cost_profiles WHERE host_id = ? "
                "ORDER BY version DESC LIMIT 1",
                (profile.host_id,),
            ).fetchone()
            previous = self._profile(row) if row else None
            version = 1 if previous is None else (previous.version or 0) + 1
            saved_at = max(now, previous.saved_at) if previous and previous.saved_at else now
            conn.execute(
                "INSERT INTO cost_profiles (host_id, version, saved_at, saved_by, name, region, "
                "currency, energy_rate_per_kwh, pue, fixed_fees_per_month, fee_spread) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    profile.host_id,
                    version,
                    to_epoch_ms(saved_at),
                    by,
                    profile.name,
                    profile.region,
                    profile.currency,
                    str(profile.energy_rate_per_kwh),
                    str(profile.pue),
                    str(profile.fixed_fees_per_month),
                    profile.fee_spread.value,
                ),
            )
            for name in fields:
                old = None if previous is None else _text(getattr(previous, name))
                new = _text(getattr(profile, name))
                if old != new:
                    conn.execute(
                        "INSERT INTO setting_changes (ts, by_user, key, old, new) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (to_epoch_ms(now), by, f"profile.{profile.host_id}.{name}", old, new),
                    )
        saved = self.get_profile_history(profile.host_id)[-1]
        return saved

    def save_price(self, price: ModelPrice, by: str) -> None:
        """Insert or edit the price for (model, valid_from); every changed field is logged."""
        fields = (
            "source",
            "input_per_m",
            "output_per_m",
            "cache_read_per_m",
            "cache_write_per_m",
            "valid_to",
            "price_set",
        )
        now = to_epoch_ms(utc_now())
        with self._write() as conn:
            row = conn.execute(
                "SELECT model, valid_from, source, input_per_m, output_per_m, cache_read_per_m, "
                "cache_write_per_m, valid_to, price_set FROM model_prices "
                "WHERE model = ? AND valid_from = ?",
                (price.model, to_epoch_ms(price.valid_from)),
            ).fetchone()
            previous = self._price(row) if row else None
            conn.execute(
                "INSERT INTO model_prices (model, valid_from, source, input_per_m, output_per_m, "
                "cache_read_per_m, cache_write_per_m, valid_to, price_set) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (model, valid_from) DO UPDATE SET "
                "source = excluded.source, input_per_m = excluded.input_per_m, "
                "output_per_m = excluded.output_per_m, "
                "cache_read_per_m = excluded.cache_read_per_m, "
                "cache_write_per_m = excluded.cache_write_per_m, valid_to = excluded.valid_to, "
                "price_set = excluded.price_set",
                (
                    price.model,
                    to_epoch_ms(price.valid_from),
                    price.source.value,
                    str(price.input_per_m),
                    str(price.output_per_m),
                    str(price.cache_read_per_m),
                    str(price.cache_write_per_m),
                    _ms(price.valid_to),
                    int(price.price_set),
                ),
            )
            for name in fields:
                old = None if previous is None else _text(getattr(previous, name))
                new = _text(getattr(price, name))
                if old != new:
                    conn.execute(
                        "INSERT INTO price_changes (ts, by_user, model, field_name, old, new) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (now, by, price.model, name, old, new),
                    )

    def save_setting(self, key: str, value: Any, by: str) -> Setting:
        new = _json(value)
        with self._write() as conn:
            row = conn.execute(
                "SELECT value, version FROM settings WHERE key = ?", (key,)
            ).fetchone()
            old, version = (row[0], row[1] + 1) if row else (None, 1)
            if old == new:
                return Setting(key, value, row[1])
            conn.execute(
                "INSERT INTO settings (key, value, version) VALUES (?, ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
                "version = excluded.version",
                (key, new, version),
            )
            conn.execute(
                "INSERT INTO setting_changes (ts, by_user, key, old, new) VALUES (?, ?, ?, ?, ?)",
                (to_epoch_ms(utc_now()), by, key, old, new),
            )
        return Setting(key, json.loads(new), version)

    def save_user(self, user: User) -> None:
        self._write_many(
            "INSERT INTO users (name, role, password_hash, must_change) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (name) DO UPDATE SET role = excluded.role, "
            "password_hash = excluded.password_hash, must_change = excluded.must_change",
            [user],
            lambda u: (u.name, u.role.value, u.password_hash, int(u.must_change)),
        )

    # --- rollups and retention ---------------------------------------------------

    def rollup(self, until: datetime, policy: RetentionPolicy | None = None) -> int:
        """Build hourly rollups for every complete hour before ``until``.

        Recomputes from the last rollup point minus a short lookback, so samples
        that arrive a little late are still counted. Returns rollup rows written.
        """
        self._require_schema()
        policy = policy or RetentionPolicy()
        until_hour = ru.hour_of(to_epoch_ms(until))
        written = 0
        with self._write() as conn:
            found = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'rollup_watermark_ms'"
            ).fetchone()
            if found:
                start = int(found[0]) - ROLLUP_LOOKBACK_MS
            else:
                firsts = [
                    conn.execute(f"SELECT MIN(ts) FROM {table}").fetchone()[0]
                    for table in _RAW_TABLES
                ]
                present = [ts for ts in firsts if ts is not None]
                if not present:
                    return 0
                start = ru.hour_of(min(present))
            if start >= until_hour:
                return 0
            window = (start, until_hour)
            for table in _HOURLY_TABLES:
                conn.execute(f"DELETE FROM {table} WHERE ts >= ? AND ts < ?", window)

            def raw(sql: str) -> list[tuple[Any, ...]]:
                return conn.execute(sql + " WHERE ts >= ? AND ts < ?", window).fetchall()

            batches = (
                (
                    "device_hourly",
                    ru.DEVICE_HOURLY_COLUMNS,
                    ru.device_hourly(
                        raw(
                            "SELECT ts, device_id, interval_s, util_pct, power_w "
                            "FROM device_samples"
                        ),
                        policy.active_util_pct,
                    ),
                ),
                (
                    "memory_hourly",
                    ru.MEMORY_HOURLY_COLUMNS,
                    ru.memory_hourly(
                        raw("SELECT ts, pool_id, interval_s, used_bytes FROM memory_samples")
                    ),
                ),
                (
                    "process_hourly",
                    ru.PROCESS_HOURLY_COLUMNS,
                    ru.process_hourly(
                        raw(
                            "SELECT ts, host_id, process_name, local_model_id, interval_s, "
                            "cpu_time_s, gpu_time_s, attributed_energy_j FROM process_samples"
                        )
                    ),
                ),
                (
                    "calls_hourly",
                    ru.CALLS_HOURLY_COLUMNS,
                    ru.calls_hourly(
                        raw(
                            "SELECT ts, provider, model, call_type, caller_id, input_tokens, "
                            "output_tokens, reasoning_tokens, cache_read_tokens, "
                            "cache_write_tokens, latency_s, status FROM llm_calls"
                        )
                    ),
                ),
            )
            for table, columns, rows in batches:
                if rows:
                    marks = ", ".join("?" for _ in columns)
                    conn.executemany(
                        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({marks})", rows
                    )
                    written += len(rows)
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('rollup_watermark_ms', ?) "
                "ON CONFLICT (key) DO UPDATE SET value = MAX(CAST(value AS INTEGER), "
                "CAST(excluded.value AS INTEGER))",
                (str(until_hour),),
            )
        return written

    def retention(self, policy: RetentionPolicy, now: datetime | None = None) -> int:
        """Roll up, then delete raw rows older than ``raw_days`` and rollups older
        than ``rollup_days``. Returns the number of rows deleted."""
        now = now or utc_now()
        self.rollup(now, policy)
        now_ms = to_epoch_ms(now)
        raw_cutoff = now_ms - policy.raw_days * 86_400_000
        rollup_cutoff = now_ms - policy.rollup_days * 86_400_000
        deleted = 0
        with self._write() as conn:
            for table in _RAW_TABLES:
                deleted += conn.execute(f"DELETE FROM {table} WHERE ts < ?", (raw_cutoff,)).rowcount
            for table in _HOURLY_TABLES:
                deleted += conn.execute(
                    f"DELETE FROM {table} WHERE ts < ?", (rollup_cutoff,)
                ).rowcount
        return deleted
