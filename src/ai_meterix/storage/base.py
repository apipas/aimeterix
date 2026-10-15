"""Storage contract every connector implements (PLAN.md 2.2; records 0010, 0014).

Collectors, engines and interfaces only ever talk to storage through these
two protocols. ``StorageReader`` is read only (reports, the API ``user`` role);
``StorageWriter`` adds writes (agent, proxy, API ``admin`` role).

A connector is "supported" when it passes ``tests/conformance``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from ai_meterix.core.errors import ValidationError
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
    Provider,
    Setting,
    SettingChange,
    User,
)
from ai_meterix.core.timeutil import ensure_utc

HOUR = timedelta(hours=1)


class Dataset(str, Enum):
    """What a ``UsageQuery`` reads."""

    DEVICE = "device"  # raw device samples
    MEMORY = "memory"  # raw memory samples
    PROCESS = "process"  # raw per-app samples
    CALLS = "calls"  # raw LLM calls
    DEVICE_HOURLY = "device_hourly"  # hourly rollups, kept after raw data expires
    MEMORY_HOURLY = "memory_hourly"
    PROCESS_HOURLY = "process_hourly"
    CALLS_HOURLY = "calls_hourly"


# Columns each dataset can be grouped and filtered by.
DIMENSIONS: Mapping[Dataset, tuple[str, ...]] = {
    Dataset.DEVICE: ("device_id", "host_id", "kind", "source"),
    Dataset.MEMORY: ("pool_id", "host_id", "pool_type", "source"),
    Dataset.PROCESS: ("host_id", "process_name", "local_model_id", "source"),
    Dataset.CALLS: ("provider", "model", "call_type", "caller_id", "app", "feature"),
    Dataset.DEVICE_HOURLY: ("device_id", "host_id", "kind"),
    Dataset.MEMORY_HOURLY: ("pool_id", "host_id", "pool_type"),
    Dataset.PROCESS_HOURLY: ("host_id", "process_name", "local_model_id"),
    Dataset.CALLS_HOURLY: ("provider", "model", "call_type", "caller_id", "app", "feature"),
}

# Aggregated columns each dataset returns, after the bucket and group-by columns.
# Sums of a column with no readings are None ("not measured"), never 0.
MEASURES: Mapping[Dataset, tuple[str, ...]] = {
    Dataset.DEVICE: (
        "samples",
        "seconds",
        "energy_j",
        "busy_s",
        "util_mean",
        "util_max",
        "power_mean",
        "power_max",
    ),
    Dataset.MEMORY: ("samples", "seconds", "used_mean", "used_max"),
    Dataset.PROCESS: ("samples", "seconds", "cpu_time_s", "gpu_time_s", "energy_j"),
    Dataset.CALLS: (
        "requests",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "latency_mean",
        "errors",
    ),
    Dataset.DEVICE_HOURLY: (
        "samples",
        "seconds",
        "energy_j",
        "busy_s",
        "active_s",
        "util_mean",
        "util_min",
        "util_max",
        "util_p95",
        "power_mean",
        "power_min",
        "power_max",
        "power_p95",
    ),
    Dataset.MEMORY_HOURLY: (
        "samples",
        "seconds",
        "used_mean",
        "used_min",
        "used_max",
        "used_p95",
    ),
    Dataset.PROCESS_HOURLY: ("samples", "seconds", "cpu_time_s", "gpu_time_s", "energy_j"),
    Dataset.CALLS_HOURLY: (
        "requests",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "latency_mean",
        "errors",
    ),
}

# Hourly datasets keep min, mean, max and p95 per hour. When a query merges
# several hours into one bucket, sums and means stay exact, min and max are the
# extremes, and p95 is the highest hourly p95 (an upper bound, not a recomputed
# percentile).


@dataclass(frozen=True, slots=True)
class UsageQuery:
    """A database-independent question: range, buckets, group-by and filters.

    ``start`` is inclusive and ``end`` exclusive, matched against each row's
    ``ts`` (the end of a sample's interval, or the start of an hourly rollup).
    ``bucket`` splits the range into equal steps aligned to the Unix epoch, so
    hourly and daily buckets fall on UTC boundaries. Without a bucket the whole
    range is one row per group.
    """

    dataset: Dataset
    start: datetime
    end: datetime
    bucket: timedelta | None = None
    group_by: tuple[str, ...] = ()
    filters: Mapping[str, Sequence[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "dataset", Dataset(self.dataset))
        except ValueError:
            raise ValidationError(f"unknown dataset {self.dataset!r}") from None
        object.__setattr__(self, "start", ensure_utc(self.start, "UsageQuery.start"))
        object.__setattr__(self, "end", ensure_utc(self.end, "UsageQuery.end"))
        if self.end <= self.start:
            raise ValidationError("UsageQuery.end must be after start")
        if self.bucket is not None:
            if self.bucket.total_seconds() < 1 or self.bucket % timedelta(seconds=1):
                raise ValidationError("UsageQuery.bucket must be a whole number of seconds >= 1")
            if self.dataset.value.endswith("_hourly") and self.bucket % HOUR:
                raise ValidationError("hourly datasets need a bucket that is a multiple of 1 hour")
        allowed = DIMENSIONS[self.dataset]
        for name in (*self.group_by, *self.filters):
            if name not in allowed:
                raise ValidationError(
                    f"cannot group or filter {self.dataset.value} by {name!r}; "
                    f"allowed: {', '.join(allowed)}"
                )
        object.__setattr__(self, "group_by", tuple(self.group_by))
        object.__setattr__(
            self, "filters", {key: tuple(values) for key, values in self.filters.items()}
        )

    @property
    def columns(self) -> tuple[str, ...]:
        bucket = ("bucket",) if self.bucket is not None else ()
        return (*bucket, *self.group_by, *MEASURES[self.dataset])


@dataclass(frozen=True, slots=True)
class Table:
    """Query result: column names and rows, sorted by bucket then group-by columns."""

    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]

    def dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row, strict=True)) for row in self.rows]


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """How long data is kept (record 0010 D1.5). Hourly rollups keep sums for costs."""

    raw_days: int = 30
    rollup_days: int = 365
    active_util_pct: float = 10.0  # a device counts as active at or above this utilization

    def __post_init__(self) -> None:
        if self.raw_days < 1 or self.rollup_days < 1:
            raise ValidationError("retention must keep at least 1 day")
        if self.rollup_days < self.raw_days:
            raise ValidationError("rollups must be kept at least as long as raw data")
        if not 0 <= self.active_util_pct <= 100:
            raise ValidationError("active_util_pct must be between 0 and 100")


@dataclass(frozen=True, slots=True)
class HostInventory:
    """A host with the memory pools and devices it owns, upserted together."""

    host: Host
    pools: tuple[MemoryPool, ...] = ()
    devices: tuple[Device, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "pools", tuple(self.pools))
        object.__setattr__(self, "devices", tuple(self.devices))
        pool_ids = {pool.id for pool in self.pools}
        for owner_id, item_id in [(p.host_id, p.id) for p in self.pools] + [
            (d.host_id, d.id) for d in self.devices
        ]:
            if owner_id != self.host.id:
                raise ValidationError(f"{item_id} belongs to {owner_id}, not {self.host.id}")
        for device in self.devices:
            if device.memory_pool_id is not None and device.memory_pool_id not in pool_ids:
                raise ValidationError(
                    f"device {device.id} uses unknown memory pool {device.memory_pool_id}"
                )


@dataclass(frozen=True, slots=True)
class HealthStatus:
    ok: bool
    backend: str
    schema_version: int | None
    detail: str = ""


@runtime_checkable
class StorageReader(Protocol):
    """Read-only access. SQLite opens with ``mode=ro``; Postgres uses a read-only user."""

    name: str

    def health(self) -> HealthStatus: ...
    def schema_version(self) -> int | None: ...
    def query(self, q: UsageQuery) -> Table: ...
    def get_projects(self) -> list[Project]: ...
    def get_hosts(self) -> list[HostInventory]: ...
    def get_callers(self) -> list[Caller]: ...
    def get_local_models(self, host_id: str | None = None) -> list[LocalModel]: ...
    def get_profile(self, host_id: str, at: datetime | None = None) -> CostProfile | None: ...
    def get_profile_history(self, host_id: str) -> list[CostProfile]: ...
    def get_prices(self, model: str | None = None) -> list[ModelPrice]: ...
    def get_price_changes(self, model: str | None = None) -> list[PriceChange]: ...
    def get_providers(self) -> list[Provider]: ...
    def get_settings(self) -> dict[str, Setting]: ...
    def get_setting_changes(self, key: str | None = None) -> list[SettingChange]: ...
    def get_user(self, name: str) -> User | None: ...
    def close(self) -> None: ...


@runtime_checkable
class StorageWriter(StorageReader, Protocol):
    """Read and write access: agent, proxy, profile loader and the API ``admin`` role."""

    def migrate(self) -> int: ...
    def upsert_projects(self, projects: Iterable[Project]) -> None: ...
    def upsert_hosts(self, hosts: Iterable[HostInventory]) -> None: ...
    def upsert_callers(self, callers: Iterable[Caller]) -> None: ...
    def upsert_local_models(self, models: Iterable[LocalModel]) -> None: ...
    def write_device_samples(self, samples: Iterable[DeviceSample]) -> int: ...
    def write_memory_samples(self, samples: Iterable[MemorySample]) -> int: ...
    def write_process_samples(self, samples: Iterable[ProcessSample]) -> int: ...
    def write_calls(self, calls: Iterable[LlmCall]) -> int: ...
    def save_profile(self, profile: CostProfile, by: str) -> CostProfile: ...
    def save_price(self, price: ModelPrice, by: str) -> None: ...
    def save_setting(self, key: str, value: Any, by: str) -> Setting: ...
    def save_provider(self, provider: Provider, by: str) -> Provider: ...
    def save_user(self, user: User) -> None: ...
    def rollup(self, until: datetime, policy: RetentionPolicy | None = None) -> int: ...
    def retention(self, policy: RetentionPolicy, now: datetime | None = None) -> int: ...
