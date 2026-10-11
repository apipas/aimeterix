"""Canonical data model (PLAN.md 2.1; records 0010, 0011, 0012, 0014).

Raw events and configuration entities only. Costs, recommendations and
forecasts are derived elsewhere and never stored here.

Rules every entity follows:
- Timestamps are timezone-aware UTC; a sample's ``ts`` is the end of its interval.
- Units are fixed: seconds, watts, joules, bytes, tokens, and the profile's currency.
- A missing reading is ``None``, never zero.
- Money is ``Decimal``; floats are converted through ``str`` so 0.1 stays 0.1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from .errors import ValidationError
from .timeutil import ensure_utc

JsonDict = dict[str, Any]


class DeviceKind(str, Enum):
    CPU = "cpu"
    GPU = "gpu"
    NPU = "npu"


class PoolType(str, Enum):
    UNIFIED = "unified"  # Apple Silicon: CPU, GPU and Neural Engine share it
    SYSTEM = "system"
    VRAM = "vram"


class Source(str, Enum):
    MEASURED = "measured"
    ESTIMATED = "estimated"
    MOCK = "mock"


class MemoryPressure(str, Enum):
    NORMAL = "normal"
    WARNING = "warning"
    CRITICAL = "critical"


class CallType(str, Enum):
    NORMAL = "normal"
    BATCH = "batch"
    FILE = "file"


class FeeSpread(str, Enum):
    CALENDAR = "calendar"
    UPTIME = "uptime"


class PriceSource(str, Enum):
    LOCAL = "local"
    API = "api"


class Role(str, Enum):
    USER = "user"
    ADMIN = "admin"


# --- validation helpers ------------------------------------------------------


def _set(obj: object, name: str, value: object) -> None:
    object.__setattr__(obj, name, value)


def _require_id(obj: object, *names: str) -> None:
    for name in names:
        value = getattr(obj, name)
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"{type(obj).__name__}.{name} must be a non-empty string")


def _require_enum(obj: object, name: str, enum: type[Enum]) -> None:
    value = getattr(obj, name)
    try:
        _set(obj, name, enum(value))
    except ValueError:
        allowed = ", ".join(str(m.value) for m in enum)
        raise ValidationError(
            f"{type(obj).__name__}.{name} must be one of: {allowed}; got {value!r}"
        ) from None


def _require_utc(obj: object, *names: str) -> None:
    for name in names:
        value = getattr(obj, name)
        if value is not None:
            _set(obj, name, ensure_utc(value, f"{type(obj).__name__}.{name}"))


def _require_non_negative(obj: object, *names: str) -> None:
    for name in names:
        value = getattr(obj, name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            raise ValidationError(f"{type(obj).__name__}.{name} must be a number")
        if value < 0:
            raise ValidationError(f"{type(obj).__name__}.{name} must be >= 0, got {value}")


def _require_int(obj: object, *names: str) -> None:
    for name in names:
        value = getattr(obj, name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValidationError(f"{type(obj).__name__}.{name} must be an integer")


def _require_positive_interval(obj: object, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValidationError(f"{type(obj).__name__}.interval_s must be > 0, got {value!r}")


def _require_money(obj: object, *names: str) -> None:
    for name in names:
        value = getattr(obj, name)
        if isinstance(value, bool):
            raise ValidationError(f"{type(obj).__name__}.{name} must be a number")
        try:
            money = value if isinstance(value, Decimal) else Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise ValidationError(
                f"{type(obj).__name__}.{name} must be a decimal number, got {value!r}"
            ) from None
        if not money.is_finite() or money < 0:
            raise ValidationError(f"{type(obj).__name__}.{name} must be >= 0, got {value!r}")
        _set(obj, name, money)


# --- hardware ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Project:
    """A group of machines, e.g. a small data center (record 0014)."""

    id: str
    name: str

    def __post_init__(self) -> None:
        _require_id(self, "id", "name")


@dataclass(frozen=True, slots=True)
class Host:
    """One metered machine: one agent and one cost profile."""

    id: str
    project_id: str
    hostname: str
    os: str
    arch: str
    chip: str | None = None
    tags: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_id(self, "id", "project_id", "hostname", "os", "arch")


@dataclass(frozen=True, slots=True)
class MemoryPool:
    """Memory that devices draw from: one unified pool on a Mac, system + VRAM elsewhere."""

    id: str
    host_id: str
    type: PoolType
    total_bytes: int | None = None

    def __post_init__(self) -> None:
        _require_id(self, "id", "host_id")
        _require_enum(self, "type", PoolType)
        _require_int(self, "total_bytes")
        _require_non_negative(self, "total_bytes")


@dataclass(frozen=True, slots=True)
class Device:
    """A compute unit on a host."""

    id: str
    host_id: str
    kind: DeviceKind
    model: str | None = None
    memory_pool_id: str | None = None

    def __post_init__(self) -> None:
        _require_id(self, "id", "host_id")
        _require_enum(self, "kind", DeviceKind)


@dataclass(frozen=True, slots=True)
class DeviceSample:
    """One reading of a device over ``interval_s`` seconds ending at ``ts``."""

    ts: datetime
    device_id: str
    interval_s: float
    util_pct: float | None = None
    power_w: float | None = None
    source: Source = Source.MEASURED
    extra: JsonDict = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_utc(self, "ts")
        _require_id(self, "device_id")
        _require_positive_interval(self, self.interval_s)
        _require_non_negative(self, "util_pct", "power_w")
        if self.util_pct is not None and self.util_pct > 100:
            raise ValidationError(f"DeviceSample.util_pct must be <= 100, got {self.util_pct}")
        _require_enum(self, "source", Source)


@dataclass(frozen=True, slots=True)
class MemorySample:
    """One reading of a memory pool; never per device on unified memory."""

    ts: datetime
    pool_id: str
    interval_s: float
    used_bytes: int | None = None
    wired_bytes: int | None = None
    compressed_bytes: int | None = None
    swap_bytes: int | None = None
    gpu_allocated_bytes: int | None = None
    pressure: MemoryPressure | None = None
    source: Source = Source.MEASURED
    extra: JsonDict = field(default_factory=dict)

    def __post_init__(self) -> None:
        byte_fields = (
            "used_bytes",
            "wired_bytes",
            "compressed_bytes",
            "swap_bytes",
            "gpu_allocated_bytes",
        )
        _require_utc(self, "ts")
        _require_id(self, "pool_id")
        _require_positive_interval(self, self.interval_s)
        _require_int(self, *byte_fields)
        _require_non_negative(self, *byte_fields)
        if self.pressure is not None:
            _require_enum(self, "pressure", MemoryPressure)
        _require_enum(self, "source", Source)


@dataclass(frozen=True, slots=True)
class ProcessSample:
    """One app's share of a host in an interval (record 0011, D2.9).

    ``attributed_energy_j`` is always an estimate split from the host's energy.
    """

    ts: datetime
    host_id: str
    process_name: str
    pid: int
    interval_s: float
    cpu_time_s: float | None = None
    gpu_time_s: float | None = None
    attributed_energy_j: float | None = None
    local_model_id: str | None = None
    source: Source = Source.MEASURED

    def __post_init__(self) -> None:
        _require_utc(self, "ts")
        _require_id(self, "host_id", "process_name")
        _require_int(self, "pid")
        _require_positive_interval(self, self.interval_s)
        _require_non_negative(self, "pid", "cpu_time_s", "gpu_time_s", "attributed_energy_j")
        _require_enum(self, "source", Source)


@dataclass(frozen=True, slots=True)
class LocalModel:
    """A local LLM model loaded by a runtime such as Ollama or llama.cpp."""

    id: str
    host_id: str
    runtime: str
    model_name: str
    first_seen: datetime
    last_seen: datetime
    size_bytes: int | None = None
    pid: int | None = None

    def __post_init__(self) -> None:
        _require_id(self, "id", "host_id", "runtime", "model_name")
        _require_utc(self, "first_seen", "last_seen")
        if self.last_seen < self.first_seen:
            raise ValidationError("LocalModel.last_seen must not be before first_seen")
        _require_int(self, "size_bytes", "pid")
        _require_non_negative(self, "size_bytes", "pid")


# --- LLM calls ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Caller:
    """Who made an LLM call: app from the URL path or header, plus feature and tags."""

    id: str
    app: str
    feature: str | None = None
    tags: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_id(self, "id", "app")


@dataclass(frozen=True, slots=True)
class LlmCall:
    """One model API call. Counts and timings only; never prompts or responses.

    Token fields are already normalized (record 0012 item 6): ``input_tokens``
    excludes cached tokens, ``output_tokens`` includes reasoning tokens, and a
    runtime that reports no cache count leaves the cache fields ``None``.
    ``raw_usage`` keeps the provider's own usage fields for checking.
    """

    ts: datetime
    provider: str
    model: str
    caller_id: str
    call_type: CallType = CallType.NORMAL
    request_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    latency_s: float | None = None
    status: int | None = None
    file_bytes: int | None = None
    raw_usage: JsonDict = field(default_factory=dict)

    def __post_init__(self) -> None:
        token_fields = (
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        )
        _require_utc(self, "ts")
        _require_id(self, "provider", "model", "caller_id")
        _require_enum(self, "call_type", CallType)
        _require_int(self, *token_fields, "status", "file_bytes")
        _require_non_negative(self, *token_fields, "latency_s", "file_bytes")


# --- cost configuration ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CostProfile:
    """A host's operational cost parameters (record 0012 D3.2).

    ``version`` and ``saved_at`` are set by storage on save; days before a
    change keep the old values.
    """

    host_id: str
    name: str
    currency: str
    energy_rate_per_kwh: Decimal
    region: str | None = None
    pue: Decimal = Decimal("1.0")
    fixed_fees_per_month: Decimal = Decimal("0")
    fee_spread: FeeSpread = FeeSpread.CALENDAR
    version: int | None = None
    saved_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_id(self, "host_id", "name")
        if not (
            isinstance(self.currency, str) and len(self.currency) == 3 and self.currency.isalpha()
        ):
            raise ValidationError(
                f"CostProfile.currency must be a 3-letter code, got {self.currency!r}"
            )
        _set(self, "currency", self.currency.upper())
        _require_money(self, "energy_rate_per_kwh", "pue", "fixed_fees_per_month")
        if self.pue < 1:
            raise ValidationError(f"CostProfile.pue must be >= 1, got {self.pue}")
        _require_enum(self, "fee_spread", FeeSpread)
        _require_int(self, "version")
        _require_utc(self, "saved_at")


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """Price per 1M tokens for one model over a validity period (record 0012 D3.7).

    ``valid_to`` is exclusive; ``None`` means still valid. A new API model
    starts at 0 with ``price_set=False`` and is shown as "price not set".
    """

    model: str
    source: PriceSource
    valid_from: datetime
    input_per_m: Decimal = Decimal("0")
    output_per_m: Decimal = Decimal("0")
    cache_read_per_m: Decimal = Decimal("0")
    cache_write_per_m: Decimal = Decimal("0")
    valid_to: datetime | None = None
    price_set: bool = True

    def __post_init__(self) -> None:
        _require_id(self, "model")
        _require_enum(self, "source", PriceSource)
        _require_money(self, "input_per_m", "output_per_m", "cache_read_per_m", "cache_write_per_m")
        _require_utc(self, "valid_from", "valid_to")
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValidationError("ModelPrice.valid_to must be after valid_from")


# --- settings, audit and accounts --------------------------------------------


@dataclass(frozen=True, slots=True)
class Setting:
    """One live setting after ``init`` (record 0013); ``value`` is JSON-serializable."""

    key: str
    value: Any
    version: int = 1

    def __post_init__(self) -> None:
        _require_id(self, "key")
        _require_int(self, "version")
        if self.version < 1:
            raise ValidationError("Setting.version must be >= 1")


@dataclass(frozen=True, slots=True)
class PriceChange:
    """Audit entry for an edited model price: who, when, old -> new."""

    ts: datetime
    by: str
    model: str
    field_name: str
    old: str | None
    new: str | None

    def __post_init__(self) -> None:
        _require_utc(self, "ts")
        _require_id(self, "by", "model", "field_name")


@dataclass(frozen=True, slots=True)
class SettingChange:
    """Audit entry for an edited setting or cost profile."""

    ts: datetime
    by: str
    key: str
    old: str | None
    new: str | None

    def __post_init__(self) -> None:
        _require_utc(self, "ts")
        _require_id(self, "by", "key")


@dataclass(frozen=True, slots=True)
class User:
    """API account (record 0013). ``password_hash`` is a salted scrypt hash."""

    name: str
    role: Role
    password_hash: str
    must_change: bool = True

    def __post_init__(self) -> None:
        _require_id(self, "name", "password_hash")
        _require_enum(self, "role", Role)

    def __repr__(self) -> str:
        return f"User(name={self.name!r}, role={self.role.value!r}, must_change={self.must_change})"
