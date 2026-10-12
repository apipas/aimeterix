"""Shared implementation for the relational connectors (SQLite, Postgres, DuckDB).

One schema, one set of statements and one rollup path; each connector only
supplies its driver, column types and a few dialect details. Conventions:

- Timestamps are integer epoch milliseconds (UTC) in every table, so bucket
  and hour arithmetic is plain integer maths and identical everywhere.
- Money is stored as text so decimals stay exact; JSON fields are text.
- Statements are written with ``?`` placeholders and translated per driver.
- Every raw write marks the hours it touched; ``rollup`` recomputes exactly
  those hours, however late the data arrived (schema v2).
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar, TypeVar

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

DAY_MS = 86_400_000

# Column types are placeholders: {BIGINT}, {DOUBLE}, {TEXT}.
MIGRATIONS: tuple[str, ...] = (
    # v1: initial schema
    """
    CREATE TABLE projects (id {TEXT} PRIMARY KEY, name {TEXT} NOT NULL);
    CREATE TABLE hosts (
        id {TEXT} PRIMARY KEY, project_id {TEXT} NOT NULL, hostname {TEXT} NOT NULL,
        os {TEXT} NOT NULL, arch {TEXT} NOT NULL, chip {TEXT}, tags {TEXT} NOT NULL
    );
    CREATE TABLE memory_pools (
        id {TEXT} PRIMARY KEY, host_id {TEXT} NOT NULL, pool_type {TEXT} NOT NULL,
        total_bytes {BIGINT}
    );
    CREATE TABLE devices (
        id {TEXT} PRIMARY KEY, host_id {TEXT} NOT NULL, kind {TEXT} NOT NULL, model {TEXT},
        memory_pool_id {TEXT}
    );
    CREATE TABLE device_samples (
        ts {BIGINT} NOT NULL, device_id {TEXT} NOT NULL, interval_s {DOUBLE} NOT NULL,
        util_pct {DOUBLE}, power_w {DOUBLE}, source {TEXT} NOT NULL, extra {TEXT} NOT NULL
    );
    CREATE INDEX device_samples_ts ON device_samples (ts, device_id);
    CREATE TABLE memory_samples (
        ts {BIGINT} NOT NULL, pool_id {TEXT} NOT NULL, interval_s {DOUBLE} NOT NULL,
        used_bytes {BIGINT}, wired_bytes {BIGINT}, compressed_bytes {BIGINT},
        swap_bytes {BIGINT}, gpu_allocated_bytes {BIGINT}, pressure {TEXT},
        source {TEXT} NOT NULL, extra {TEXT} NOT NULL
    );
    CREATE INDEX memory_samples_ts ON memory_samples (ts, pool_id);
    CREATE TABLE process_samples (
        ts {BIGINT} NOT NULL, host_id {TEXT} NOT NULL, process_name {TEXT} NOT NULL,
        pid {BIGINT} NOT NULL, interval_s {DOUBLE} NOT NULL, cpu_time_s {DOUBLE},
        gpu_time_s {DOUBLE}, attributed_energy_j {DOUBLE}, local_model_id {TEXT},
        source {TEXT} NOT NULL
    );
    CREATE INDEX process_samples_ts ON process_samples (ts, host_id);
    CREATE TABLE local_models (
        id {TEXT} PRIMARY KEY, host_id {TEXT} NOT NULL, runtime {TEXT} NOT NULL,
        model_name {TEXT} NOT NULL, first_seen {BIGINT} NOT NULL, last_seen {BIGINT} NOT NULL,
        size_bytes {BIGINT}, pid {BIGINT}
    );
    CREATE TABLE callers (
        id {TEXT} PRIMARY KEY, app {TEXT} NOT NULL, feature {TEXT}, tags {TEXT} NOT NULL
    );
    CREATE TABLE llm_calls (
        ts {BIGINT} NOT NULL, provider {TEXT} NOT NULL, model {TEXT} NOT NULL,
        caller_id {TEXT} NOT NULL, call_type {TEXT} NOT NULL, request_id {TEXT},
        input_tokens {BIGINT}, output_tokens {BIGINT}, reasoning_tokens {BIGINT},
        cache_read_tokens {BIGINT}, cache_write_tokens {BIGINT}, latency_s {DOUBLE},
        status {BIGINT}, file_bytes {BIGINT}, raw_usage {TEXT} NOT NULL
    );
    CREATE INDEX llm_calls_ts ON llm_calls (ts, model);
    CREATE TABLE cost_profiles (
        host_id {TEXT} NOT NULL, version {BIGINT} NOT NULL, saved_at {BIGINT} NOT NULL,
        saved_by {TEXT} NOT NULL, name {TEXT} NOT NULL, region {TEXT},
        currency {TEXT} NOT NULL, energy_rate_per_kwh {TEXT} NOT NULL, pue {TEXT} NOT NULL,
        fixed_fees_per_month {TEXT} NOT NULL, fee_spread {TEXT} NOT NULL,
        PRIMARY KEY (host_id, version)
    );
    CREATE TABLE model_prices (
        model {TEXT} NOT NULL, valid_from {BIGINT} NOT NULL, source {TEXT} NOT NULL,
        input_per_m {TEXT} NOT NULL, output_per_m {TEXT} NOT NULL,
        cache_read_per_m {TEXT} NOT NULL, cache_write_per_m {TEXT} NOT NULL,
        valid_to {BIGINT}, price_set {BIGINT} NOT NULL, PRIMARY KEY (model, valid_from)
    );
    CREATE TABLE price_changes (
        seq {BIGINT} NOT NULL, ts {BIGINT} NOT NULL, by_user {TEXT} NOT NULL,
        model {TEXT} NOT NULL, field_name {TEXT} NOT NULL, old_value {TEXT}, new_value {TEXT}
    );
    CREATE TABLE settings (
        setting_key {TEXT} PRIMARY KEY, setting_value {TEXT} NOT NULL, version {BIGINT} NOT NULL
    );
    CREATE TABLE setting_changes (
        seq {BIGINT} NOT NULL, ts {BIGINT} NOT NULL, by_user {TEXT} NOT NULL,
        setting_key {TEXT} NOT NULL, old_value {TEXT}, new_value {TEXT}
    );
    CREATE TABLE users (
        name {TEXT} PRIMARY KEY, role {TEXT} NOT NULL, password_hash {TEXT} NOT NULL,
        must_change {BIGINT} NOT NULL
    );
    CREATE TABLE device_hourly (
        ts {BIGINT} NOT NULL, device_id {TEXT} NOT NULL, samples {BIGINT} NOT NULL,
        seconds {DOUBLE} NOT NULL, energy_j {DOUBLE}, busy_s {DOUBLE}, active_s {DOUBLE},
        util_s {DOUBLE}, util_min {DOUBLE}, util_max {DOUBLE}, util_p95 {DOUBLE},
        power_s {DOUBLE}, power_min {DOUBLE}, power_max {DOUBLE}, power_p95 {DOUBLE},
        PRIMARY KEY (ts, device_id)
    );
    CREATE TABLE memory_hourly (
        ts {BIGINT} NOT NULL, pool_id {TEXT} NOT NULL, samples {BIGINT} NOT NULL,
        seconds {DOUBLE} NOT NULL, used_byte_s {DOUBLE}, used_s {DOUBLE}, used_min {BIGINT},
        used_max {BIGINT}, used_p95 {BIGINT}, PRIMARY KEY (ts, pool_id)
    );
    CREATE TABLE process_hourly (
        ts {BIGINT} NOT NULL, host_id {TEXT} NOT NULL, process_name {TEXT} NOT NULL,
        local_model_id {TEXT}, samples {BIGINT} NOT NULL, seconds {DOUBLE} NOT NULL,
        cpu_time_s {DOUBLE}, gpu_time_s {DOUBLE}, energy_j {DOUBLE}
    );
    CREATE INDEX process_hourly_ts ON process_hourly (ts);
    CREATE TABLE calls_hourly (
        ts {BIGINT} NOT NULL, provider {TEXT} NOT NULL, model {TEXT} NOT NULL,
        call_type {TEXT} NOT NULL, caller_id {TEXT} NOT NULL, requests {BIGINT} NOT NULL,
        input_tokens {BIGINT}, output_tokens {BIGINT}, reasoning_tokens {BIGINT},
        cache_read_tokens {BIGINT}, cache_write_tokens {BIGINT}, latency_sum {DOUBLE},
        latency_n {BIGINT} NOT NULL, errors {BIGINT} NOT NULL,
        PRIMARY KEY (ts, provider, model, call_type, caller_id)
    )
    """,
    # v2: hours waiting for a rollup; seeded with every hour that has raw data
    """
    CREATE TABLE rollup_dirty (hour {BIGINT} PRIMARY KEY);
    INSERT INTO rollup_dirty (hour)
        SELECT DISTINCT h FROM (
            SELECT ts - ts % 3600000 AS h FROM device_samples
            UNION SELECT ts - ts % 3600000 AS h FROM memory_samples
            UNION SELECT ts - ts % 3600000 AS h FROM process_samples
            UNION SELECT ts - ts % 3600000 AS h FROM llm_calls
        ) hours
    """,
)
LATEST_VERSION = len(MIGRATIONS)

RAW_TABLES = ("device_samples", "memory_samples", "process_samples", "llm_calls")
HOURLY_TABLES = ("device_hourly", "memory_hourly", "process_hourly", "calls_hourly")


class SchemaError(AiMeterixError):
    """The database schema is missing, or newer than this version understands."""


class ReadOnlyError(AiMeterixError):
    """A write was attempted on a read-only storage connection."""


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


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


def _plain(value: Any) -> Any:
    """Database numerics to plain Python numbers (Postgres SUM returns Decimal)."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


class SQLStorage:
    """``StorageWriter`` (or, with ``read_only=True``, a ``StorageReader``) over SQL.

    Subclasses set ``name`` and ``types`` and implement the ``_driver_*`` hooks.
    """

    name: ClassVar[str]
    types: ClassVar[dict[str, str]]
    placeholder: ClassVar[str] = "?"
    least: ClassVar[str] = "LEAST"
    greatest: ClassVar[str] = "GREATEST"
    max_attempts: ClassVar[int] = 5

    read_only: bool

    def __init__(self) -> None:
        self._lock = threading.RLock()

    # --- driver hooks ------------------------------------------------------------

    def _driver_execute(self, sql: str, params: Sequence[Any]) -> list[tuple[Any, ...]]:
        raise NotImplementedError

    def _driver_executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        raise NotImplementedError

    @contextmanager
    def _driver_transaction(self) -> Iterator[None]:
        raise NotImplementedError
        yield  # pragma: no cover

    def _driver_close(self) -> None:
        raise NotImplementedError

    def _table_exists(self, table: str) -> bool:
        rows = self._query("SELECT 1 FROM information_schema.tables WHERE table_name = ?", (table,))
        return bool(rows)

    def _is_retryable(self, exc: Exception) -> bool:
        return False

    # --- plumbing ----------------------------------------------------------------

    def _sql(self, sql: str) -> str:
        if self.placeholder == "?":
            return sql
        # pyformat drivers (psycopg): a literal % (modulo) must be doubled
        return sql.replace("%", "%%").replace("?", self.placeholder)

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        with self._lock:
            return self._driver_execute(self._sql(sql), tuple(params))

    def _run(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        """Execute inside the current write transaction."""
        return self._driver_execute(self._sql(sql), tuple(params))

    def _run_many(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        if rows:
            self._driver_executemany(self._sql(sql), rows)

    def _write(self, body: Callable[[], T]) -> T:
        """Run ``body`` in one write transaction, retrying conflicts the driver reports."""
        if self.read_only:
            raise ReadOnlyError("this storage connection is read only")
        for attempt in range(1, self.max_attempts + 1):
            try:
                with self._lock, self._driver_transaction():
                    return body()
            except Exception as exc:
                if attempt == self.max_attempts or not self._is_retryable(exc):
                    raise
                time.sleep(0.02 * attempt)
        raise AssertionError("unreachable")  # pragma: no cover

    def _ddl(self, script: str) -> list[str]:
        script = script.format(**self.types)
        return [statement.strip() for statement in script.split(";") if statement.strip()]

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
            self._driver_close()

    def __enter__(self: T) -> T:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- schema --------------------------------------------------------------------

    def schema_version(self) -> int | None:
        if not self._table_exists("schema_meta"):
            return None
        found = self._query("SELECT meta_value FROM schema_meta WHERE meta_key = 'schema_version'")
        return int(found[0][0]) if found else None

    def migrate(self, target: int | None = None) -> int:
        """Create or upgrade the schema, forward only. Returns the resulting version."""
        target = LATEST_VERSION if target is None else target
        if not 1 <= target <= LATEST_VERSION:
            raise ValidationError(f"unknown schema version {target}")

        def body() -> int:
            self._run(
                "CREATE TABLE IF NOT EXISTS schema_meta "
                f"(meta_key {self.types['TEXT']} PRIMARY KEY, meta_value {self.types['TEXT']})"
            )
            found = self._run(
                "SELECT meta_value FROM schema_meta WHERE meta_key = 'schema_version'"
            )
            current = int(found[0][0]) if found else 0
            if current > LATEST_VERSION:
                raise SchemaError(
                    f"database schema v{current} is newer than this AI Meterix (v{LATEST_VERSION})"
                )
            for version in range(current + 1, target + 1):
                for statement in self._ddl(MIGRATIONS[version - 1]):
                    self._run(statement)
                self._set_meta("schema_version", str(version))
            return max(current, target)

        return self._write(body)

    def _set_meta(self, key: str, value: str) -> None:
        self._run("DELETE FROM schema_meta WHERE meta_key = ?", (key,))
        self._run("INSERT INTO schema_meta (meta_key, meta_value) VALUES (?, ?)", (key, value))

    def _get_meta(self, key: str) -> str | None:
        found = self._run("SELECT meta_value FROM schema_meta WHERE meta_key = ?", (key,))
        return str(found[0][0]) if found else None

    def health(self) -> HealthStatus:
        try:
            version = self.schema_version()
        except Exception as exc:
            return HealthStatus(False, self.name, None, f"database error: {exc}")
        if version is None:
            return HealthStatus(False, self.name, None, "schema missing; run aimeterix init")
        if version != LATEST_VERSION:
            return HealthStatus(
                False, self.name, version, f"schema v{version}, expected v{LATEST_VERSION}"
            )
        return HealthStatus(True, self.name, version)

    # --- reads ---------------------------------------------------------------------

    def query(self, q: UsageQuery) -> Table:
        self._require_schema()
        sql, params = build_query(q)
        rows = self._query(sql, params)
        first_measure = (1 if q.bucket is not None else 0) + len(q.group_by)
        out: list[tuple[Any, ...]] = []
        for row in rows:
            if not row[first_measure]:  # no samples or calls in this group
                continue
            values = [_plain(value) for value in row]
            if q.bucket is not None:
                values[0] = from_epoch_ms(int(values[0]))
            out.append(tuple(values))
        return Table(q.columns, tuple(out))

    def get_projects(self) -> list[Project]:
        return [Project(*row) for row in self._query("SELECT id, name FROM projects ORDER BY id")]

    def get_hosts(self) -> list[HostInventory]:
        pools: dict[str, list[MemoryPool]] = {}
        for row in self._query(
            "SELECT id, host_id, pool_type, total_bytes FROM memory_pools ORDER BY id"
        ):
            pools.setdefault(row[1], []).append(MemoryPool(*row))
        devices: dict[str, list[Device]] = {}
        for row in self._query(
            "SELECT id, host_id, kind, model, memory_pool_id FROM devices ORDER BY id"
        ):
            devices.setdefault(row[1], []).append(Device(*row))
        hosts = []
        for hid, pid, hostname, os_, arch, chip, tags in self._query(
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
            for cid, app, feature, tags in self._query(
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
            LocalModel(mid, hid, rt, name, from_epoch_ms(first), from_epoch_ms(last), size, pid)
            for mid, hid, rt, name, first, last, size, pid in self._query(
                sql + " ORDER BY id", params
            )
        ]

    _PROFILE_COLUMNS = (
        "host_id, name, currency, energy_rate_per_kwh, region, pue, fixed_fees_per_month, "
        "fee_spread, version, saved_at"
    )

    @staticmethod
    def _profile(row: Sequence[Any]) -> CostProfile:
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
            int(version),
            from_epoch_ms(saved_at),
        )

    def get_profile(self, host_id: str, at: datetime | None = None) -> CostProfile | None:
        """The profile in force at ``at`` (default: now): the last version saved by then."""
        rows = self._query(
            f"SELECT {self._PROFILE_COLUMNS} FROM cost_profiles "
            "WHERE host_id = ? AND saved_at <= ? ORDER BY version DESC LIMIT 1",
            (host_id, to_epoch_ms(at or utc_now())),
        )
        return self._profile(rows[0]) if rows else None

    def get_profile_history(self, host_id: str) -> list[CostProfile]:
        return [
            self._profile(row)
            for row in self._query(
                f"SELECT {self._PROFILE_COLUMNS} FROM cost_profiles WHERE host_id = ? "
                "ORDER BY version",
                (host_id,),
            )
        ]

    _PRICE_COLUMNS = (
        "model, valid_from, source, input_per_m, output_per_m, cache_read_per_m, "
        "cache_write_per_m, valid_to, price_set"
    )

    @staticmethod
    def _price(row: Sequence[Any]) -> ModelPrice:
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
        sql = f"SELECT {self._PRICE_COLUMNS} FROM model_prices"
        params: tuple[Any, ...] = ()
        if model is not None:
            sql += " WHERE model = ?"
            params = (model,)
        return [
            self._price(row) for row in self._query(sql + " ORDER BY model, valid_from", params)
        ]

    def get_price_changes(self, model: str | None = None) -> list[PriceChange]:
        sql = "SELECT ts, by_user, model, field_name, old_value, new_value FROM price_changes"
        params: tuple[Any, ...] = ()
        if model is not None:
            sql += " WHERE model = ?"
            params = (model,)
        return [
            PriceChange(from_epoch_ms(ts), by, m, f, old, new)
            for ts, by, m, f, old, new in self._query(sql + " ORDER BY seq", params)
        ]

    def get_settings(self) -> dict[str, Setting]:
        return {
            key: Setting(key, json.loads(value), int(version))
            for key, value, version in self._query(
                "SELECT setting_key, setting_value, version FROM settings ORDER BY setting_key"
            )
        }

    def get_setting_changes(self, key: str | None = None) -> list[SettingChange]:
        sql = "SELECT ts, by_user, setting_key, old_value, new_value FROM setting_changes"
        params: tuple[Any, ...] = ()
        if key is not None:
            sql += " WHERE setting_key = ?"
            params = (key,)
        return [
            SettingChange(from_epoch_ms(ts), by, k, old, new)
            for ts, by, k, old, new in self._query(sql + " ORDER BY seq", params)
        ]

    def get_user(self, name: str) -> User | None:
        rows = self._query(
            "SELECT name, role, password_hash, must_change FROM users WHERE name = ?", (name,)
        )
        if not rows:
            return None
        uname, role, password_hash, must_change = rows[0]
        return User(uname, role, password_hash, bool(must_change))

    # --- inventory writes ----------------------------------------------------------

    def _upsert(self, table: str, columns: Sequence[str], keys: Sequence[str]) -> str:
        marks = ", ".join("?" for _ in columns)
        updates = ", ".join(f"{c} = excluded.{c}" for c in columns if c not in keys)
        return (
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({marks}) "
            f"ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {updates}"
        )

    def upsert_projects(self, projects: Iterable[Project]) -> None:
        rows = [(p.id, p.name) for p in projects]
        if rows:
            sql = self._upsert("projects", ("id", "name"), ("id",))
            self._write(lambda: self._run_many(sql, rows))

    def upsert_hosts(self, hosts: Iterable[HostInventory]) -> None:
        inventories = list(hosts)
        if not inventories:
            return
        host_rows = [
            (h.id, h.project_id, h.hostname, h.os, h.arch, h.chip, _json(h.tags))
            for h in (inv.host for inv in inventories)
        ]
        pool_rows = [
            (p.id, p.host_id, p.type.value, p.total_bytes) for inv in inventories for p in inv.pools
        ]
        device_rows = [
            (d.id, d.host_id, d.kind.value, d.model, d.memory_pool_id)
            for inv in inventories
            for d in inv.devices
        ]

        def body() -> None:
            self._run_many(
                self._upsert(
                    "hosts", ("id", "project_id", "hostname", "os", "arch", "chip", "tags"), ("id",)
                ),
                host_rows,
            )
            self._run_many(
                self._upsert(
                    "memory_pools", ("id", "host_id", "pool_type", "total_bytes"), ("id",)
                ),
                pool_rows,
            )
            self._run_many(
                self._upsert(
                    "devices", ("id", "host_id", "kind", "model", "memory_pool_id"), ("id",)
                ),
                device_rows,
            )

        self._write(body)

    def upsert_callers(self, callers: Iterable[Caller]) -> None:
        rows = [(c.id, c.app, c.feature, _json(c.tags)) for c in callers]
        if rows:
            sql = self._upsert("callers", ("id", "app", "feature", "tags"), ("id",))
            self._write(lambda: self._run_many(sql, rows))

    def upsert_local_models(self, models: Iterable[LocalModel]) -> None:
        rows = [
            (
                m.id,
                m.host_id,
                m.runtime,
                m.model_name,
                to_epoch_ms(m.first_seen),
                to_epoch_ms(m.last_seen),
                m.size_bytes,
                m.pid,
            )
            for m in models
        ]
        if not rows:
            return
        sql = (
            "INSERT INTO local_models "
            "(id, host_id, runtime, model_name, first_seen, last_seen, size_bytes, pid) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET "
            f"first_seen = {self.least}(local_models.first_seen, excluded.first_seen), "
            f"last_seen = {self.greatest}(local_models.last_seen, excluded.last_seen), "
            "size_bytes = COALESCE(excluded.size_bytes, local_models.size_bytes), "
            "pid = excluded.pid"
        )
        self._write(lambda: self._run_many(sql, rows))

    # --- raw writes ----------------------------------------------------------------

    def _write_raw(self, table: str, columns: Sequence[str], rows: list[tuple[Any, ...]]) -> int:
        """Insert raw rows (``ts`` first) and mark their hours for the next rollup."""
        if not rows:
            return 0
        sql = (
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})"
        )
        hours = sorted({(ru.hour_of(row[0]),) for row in rows})

        def body() -> int:
            self._run_many(sql, rows)
            self._run_many(
                "INSERT INTO rollup_dirty (hour) VALUES (?) ON CONFLICT (hour) DO NOTHING", hours
            )
            return len(rows)

        return self._write(body)

    def write_device_samples(self, samples: Iterable[DeviceSample]) -> int:
        return self._write_raw(
            "device_samples",
            ("ts", "device_id", "interval_s", "util_pct", "power_w", "source", "extra"),
            [
                (
                    to_epoch_ms(s.ts),
                    s.device_id,
                    s.interval_s,
                    s.util_pct,
                    s.power_w,
                    s.source.value,
                    _json(s.extra),
                )
                for s in samples
            ],
        )

    def write_memory_samples(self, samples: Iterable[MemorySample]) -> int:
        return self._write_raw(
            "memory_samples",
            (
                "ts",
                "pool_id",
                "interval_s",
                "used_bytes",
                "wired_bytes",
                "compressed_bytes",
                "swap_bytes",
                "gpu_allocated_bytes",
                "pressure",
                "source",
                "extra",
            ),
            [
                (
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
                )
                for s in samples
            ],
        )

    def write_process_samples(self, samples: Iterable[ProcessSample]) -> int:
        return self._write_raw(
            "process_samples",
            (
                "ts",
                "host_id",
                "process_name",
                "pid",
                "interval_s",
                "cpu_time_s",
                "gpu_time_s",
                "attributed_energy_j",
                "local_model_id",
                "source",
            ),
            [
                (
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
                )
                for s in samples
            ],
        )

    def write_calls(self, calls: Iterable[LlmCall]) -> int:
        return self._write_raw(
            "llm_calls",
            (
                "ts",
                "provider",
                "model",
                "caller_id",
                "call_type",
                "request_id",
                "input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "latency_s",
                "status",
                "file_bytes",
                "raw_usage",
            ),
            [
                (
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
                )
                for c in calls
            ],
        )

    # --- configuration writes ------------------------------------------------------

    def _next_seq(self, table: str) -> int:
        return int(self._run(f"SELECT COALESCE(MAX(seq), 0) + 1 FROM {table}")[0][0])

    def _log_setting(self, ts: int, by: str, key: str, old: str | None, new: str | None) -> None:
        self._run(
            "INSERT INTO setting_changes (seq, ts, by_user, setting_key, old_value, new_value) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (self._next_seq("setting_changes"), ts, by, key, old, new),
        )

    def save_profile(self, profile: CostProfile, by: str) -> CostProfile:
        """Save a new version; the change log records each changed field."""
        fields = (
            "name",
            "currency",
            "energy_rate_per_kwh",
            "region",
            "pue",
            "fixed_fees_per_month",
            "fee_spread",
        )

        def body() -> CostProfile:
            now = utc_now()
            row = self._run(
                f"SELECT {self._PROFILE_COLUMNS} FROM cost_profiles WHERE host_id = ? "
                "ORDER BY version DESC LIMIT 1",
                (profile.host_id,),
            )
            previous = self._profile(row[0]) if row else None
            version = 1 if previous is None else (previous.version or 0) + 1
            saved_at = max(now, previous.saved_at) if previous and previous.saved_at else now
            self._run(
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
                    key = f"profile.{profile.host_id}.{name}"
                    self._log_setting(to_epoch_ms(now), by, key, old, new)
            saved = self._run(
                f"SELECT {self._PROFILE_COLUMNS} FROM cost_profiles "
                "WHERE host_id = ? AND version = ?",
                (profile.host_id, version),
            )
            return self._profile(saved[0])

        return self._write(body)

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
        columns = (
            "model",
            "valid_from",
            "source",
            "input_per_m",
            "output_per_m",
            "cache_read_per_m",
            "cache_write_per_m",
            "valid_to",
            "price_set",
        )
        values = (
            price.model,
            to_epoch_ms(price.valid_from),
            price.source.value,
            str(price.input_per_m),
            str(price.output_per_m),
            str(price.cache_read_per_m),
            str(price.cache_write_per_m),
            None if price.valid_to is None else to_epoch_ms(price.valid_to),
            int(price.price_set),
        )

        def body() -> None:
            now = to_epoch_ms(utc_now())
            row = self._run(
                f"SELECT {self._PRICE_COLUMNS} FROM model_prices "
                "WHERE model = ? AND valid_from = ?",
                (price.model, to_epoch_ms(price.valid_from)),
            )
            previous = self._price(row[0]) if row else None
            self._run(self._upsert("model_prices", columns, ("model", "valid_from")), values)
            for name in fields:
                old = None if previous is None else _text(getattr(previous, name))
                new = _text(getattr(price, name))
                if old != new:
                    self._run(
                        "INSERT INTO price_changes "
                        "(seq, ts, by_user, model, field_name, old_value, new_value) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (self._next_seq("price_changes"), now, by, price.model, name, old, new),
                    )

        self._write(body)

    def save_setting(self, key: str, value: Any, by: str) -> Setting:
        new = _json(value)

        def body() -> Setting:
            row = self._run(
                "SELECT setting_value, version FROM settings WHERE setting_key = ?", (key,)
            )
            if row and row[0][0] == new:
                return Setting(key, json.loads(new), int(row[0][1]))
            old, version = (row[0][0], int(row[0][1]) + 1) if row else (None, 1)
            self._run(
                self._upsert(
                    "settings", ("setting_key", "setting_value", "version"), ("setting_key",)
                ),
                (key, new, version),
            )
            self._log_setting(to_epoch_ms(utc_now()), by, key, old, new)
            return Setting(key, json.loads(new), version)

        return self._write(body)

    def save_user(self, user: User) -> None:
        sql = self._upsert("users", ("name", "role", "password_hash", "must_change"), ("name",))
        row = (user.name, user.role.value, user.password_hash, int(user.must_change))
        self._write(lambda: self._run(sql, row))

    # --- rollups and retention -----------------------------------------------------

    def rollup(self, until: datetime, policy: RetentionPolicy | None = None) -> int:
        """Recompute hourly rollups for every changed hour that ended by ``until``.

        Hours whose raw data was already purged by retention are left as they
        are: a late sample cannot be merged into a rollup whose raw data is gone.
        Returns the number of rollup rows written.
        """
        self._require_schema()
        policy = policy or RetentionPolicy()
        until_hour = ru.hour_of(to_epoch_ms(until))

        def body() -> int:
            purged_before = int(self._get_meta("raw_purged_before_ms") or 0)
            hours = [
                int(row[0])
                for row in self._run(
                    "SELECT hour FROM rollup_dirty WHERE hour < ? ORDER BY hour", (until_hour,)
                )
            ]
            written = 0
            for hour in hours:
                self._run("DELETE FROM rollup_dirty WHERE hour = ?", (hour,))
                if hour < purged_before:
                    continue
                written += self._rollup_hour(hour, policy)
            return written

        return self._write(body)

    def _rollup_hour(self, hour: int, policy: RetentionPolicy) -> int:
        window = (hour, hour + ru.HOUR_MS)
        for table in HOURLY_TABLES:
            self._run(f"DELETE FROM {table} WHERE ts >= ? AND ts < ?", window)

        def raw(sql: str) -> list[tuple[Any, ...]]:
            return self._run(sql + " WHERE ts >= ? AND ts < ?", window)

        batches = (
            (
                "device_hourly",
                ru.DEVICE_HOURLY_COLUMNS,
                ru.device_hourly(
                    raw("SELECT ts, device_id, interval_s, util_pct, power_w FROM device_samples"),
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
        written = 0
        for table, columns, rows in batches:
            marks = ", ".join("?" for _ in columns)
            self._run_many(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({marks})", rows)
            written += len(rows)
        return written

    def retention(self, policy: RetentionPolicy, now: datetime | None = None) -> int:
        """Roll up, then delete raw rows older than ``raw_days`` and rollups older
        than ``rollup_days``. Returns the number of rows deleted."""
        now = now or utc_now()
        self.rollup(now, policy)
        now_ms = to_epoch_ms(now)
        raw_cutoff = now_ms - policy.raw_days * DAY_MS
        rollup_cutoff = now_ms - policy.rollup_days * DAY_MS

        def body() -> int:
            deleted = 0
            for table, cutoff in [(t, raw_cutoff) for t in RAW_TABLES] + [
                (t, rollup_cutoff) for t in HOURLY_TABLES
            ]:
                count = self._run(f"SELECT COUNT(*) FROM {table} WHERE ts < ?", (cutoff,))
                deleted += int(count[0][0])
                self._run(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
            previous = int(self._get_meta("raw_purged_before_ms") or 0)
            self._set_meta("raw_purged_before_ms", str(max(previous, raw_cutoff)))
            return deleted

        return self._write(body)
