from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ai_meterix.core.errors import ValidationError
from ai_meterix.core.model import (
    CallType,
    CostProfile,
    Device,
    DeviceKind,
    DeviceSample,
    FeeSpread,
    Host,
    LlmCall,
    LocalModel,
    MemoryPool,
    MemoryPressure,
    MemorySample,
    ModelPrice,
    PoolType,
    PriceSource,
    ProcessSample,
    Role,
    Setting,
    Source,
    User,
)

T0 = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


def test_mac_host_has_one_unified_pool_shared_by_devices() -> None:
    host = Host("h1", "p1", "studio", "macos", "arm64", chip="Apple M4 Max")
    pool = MemoryPool("h1:mem", host.id, PoolType.UNIFIED, total_bytes=64 * 2**30)
    devices = [
        Device(f"h1:{kind.value}", host.id, kind, memory_pool_id=pool.id) for kind in DeviceKind
    ]
    assert {d.memory_pool_id for d in devices} == {pool.id}


def test_enums_accept_their_string_values() -> None:
    assert MemoryPool("m", "h", "vram").type is PoolType.VRAM  # type: ignore[arg-type]
    assert Device("d", "h", "gpu").kind is DeviceKind.GPU  # type: ignore[arg-type]


def test_unknown_enum_value_is_refused() -> None:
    with pytest.raises(ValidationError, match="kind must be one of: cpu, gpu, npu"):
        Device("d", "h", "tpu")  # type: ignore[arg-type]


def test_entities_are_frozen() -> None:
    sample = DeviceSample(T0, "d", 10)
    with pytest.raises(FrozenInstanceError):
        sample.util_pct = 5  # type: ignore[misc]


def test_missing_readings_stay_none_not_zero() -> None:
    sample = DeviceSample(T0, "d", 10)
    assert sample.util_pct is None
    assert sample.power_w is None
    assert sample.source is Source.MEASURED
    mem = MemorySample(T0, "pool", 10)
    assert mem.used_bytes is None
    assert mem.pressure is None


def test_sample_timestamp_must_be_aware_and_is_stored_in_utc() -> None:
    with pytest.raises(ValidationError):
        DeviceSample(datetime(2026, 1, 1), "d", 10)
    local = T0.astimezone(timezone(timedelta(hours=-5)))
    assert DeviceSample(local, "d", 10).ts.tzinfo == timezone.utc


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"interval_s": 0}, "interval_s must be > 0"),
        ({"util_pct": 101.0}, "util_pct must be <= 100"),
        ({"util_pct": -1.0}, "util_pct must be >= 0"),
        ({"power_w": -0.1}, "power_w must be >= 0"),
        ({"device_id": ""}, "device_id must be a non-empty string"),
    ],
)
def test_device_sample_rejects_bad_values(kwargs: dict[str, object], message: str) -> None:
    base: dict[str, object] = {"ts": T0, "device_id": "d", "interval_s": 10}
    with pytest.raises(ValidationError, match=message):
        DeviceSample(**{**base, **kwargs})  # type: ignore[arg-type]


def test_memory_sample_bytes_must_be_whole_numbers() -> None:
    with pytest.raises(ValidationError, match="used_bytes must be an integer"):
        MemorySample(T0, "pool", 10, used_bytes=1.5)  # type: ignore[arg-type]
    sample = MemorySample(T0, "pool", 10, used_bytes=10, pressure="warning")  # type: ignore[arg-type]
    assert sample.pressure is MemoryPressure.WARNING


def test_process_sample_energy_is_optional_estimate() -> None:
    sample = ProcessSample(T0, "h", "ollama", 4242, 10, cpu_time_s=3.2)
    assert sample.attributed_energy_j is None
    with pytest.raises(ValidationError):
        ProcessSample(T0, "h", "ollama", -1, 10)


def test_local_model_seen_window_must_be_ordered() -> None:
    LocalModel("m", "h", "ollama", "llama3:8b", T0, T0)
    with pytest.raises(ValidationError, match="last_seen"):
        LocalModel("m", "h", "ollama", "llama3:8b", T0, T0 - timedelta(seconds=1))


def test_llm_call_counts_only() -> None:
    call = LlmCall(
        T0,
        "anthropic",
        "claude-sonnet",
        "unknown",
        request_id="req_1",
        input_tokens=100,
        output_tokens=20,
        cache_read_tokens=None,
        raw_usage={"input_tokens": 100, "output_tokens": 20},
    )
    assert call.call_type is CallType.NORMAL
    assert call.cache_read_tokens is None
    with pytest.raises(ValidationError, match="output_tokens must be >= 0"):
        LlmCall(T0, "openai", "gpt", "unknown", output_tokens=-1)
    with pytest.raises(ValidationError, match="input_tokens must be an integer"):
        LlmCall(T0, "openai", "gpt", "unknown", input_tokens=True)  # type: ignore[arg-type]


def test_cost_profile_uses_exact_decimals() -> None:
    profile = CostProfile(
        "h1",
        "home office",
        "eur",
        energy_rate_per_kwh=0.1,  # type: ignore[arg-type]
        fixed_fees_per_month="12.50",  # type: ignore[arg-type]
    )
    assert profile.energy_rate_per_kwh == Decimal("0.1")
    assert profile.fixed_fees_per_month == Decimal("12.50")
    assert profile.currency == "EUR"
    assert profile.pue == Decimal("1.0")
    assert profile.fee_spread is FeeSpread.CALENDAR


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"currency": "EURO"}, "3-letter code"),
        ({"pue": Decimal("0.9")}, "pue must be >= 1"),
        ({"energy_rate_per_kwh": Decimal("-1")}, "must be >= 0"),
        ({"energy_rate_per_kwh": "abc"}, "must be a decimal number"),
        ({"energy_rate_per_kwh": float("nan")}, "must be >= 0"),
    ],
)
def test_cost_profile_rejects_bad_values(kwargs: dict[str, object], message: str) -> None:
    base: dict[str, object] = {
        "host_id": "h1",
        "name": "p",
        "currency": "USD",
        "energy_rate_per_kwh": Decimal("0.2"),
    }
    with pytest.raises(ValidationError, match=message):
        CostProfile(**{**base, **kwargs})  # type: ignore[arg-type]


def test_model_price_period_and_unset_default() -> None:
    price = ModelPrice("gpt-x", PriceSource.API, T0, price_set=False)
    assert price.input_per_m == Decimal("0")
    assert price.valid_to is None
    with pytest.raises(ValidationError, match="valid_to must be after valid_from"):
        ModelPrice("gpt-x", PriceSource.API, T0, valid_to=T0)


def test_setting_version_starts_at_one() -> None:
    assert Setting("agent.interval_s", 10).version == 1
    with pytest.raises(ValidationError):
        Setting("agent.interval_s", 10, version=0)


def test_user_repr_hides_password_hash() -> None:
    user = User("admin", Role.ADMIN, "scrypt$salt$hash")
    assert "scrypt" not in repr(user)
    assert user.must_change is True
