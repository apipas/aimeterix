import pytest

from ai_meterix.storage.rollup import HOUR_MS, calls_hourly, device_hourly, hour_of, p95


def test_hour_of() -> None:
    assert hour_of(HOUR_MS * 5 + 1) == HOUR_MS * 5
    assert hour_of(HOUR_MS * 5) == HOUR_MS * 5


def test_p95_nearest_rank() -> None:
    assert p95([]) is None
    assert p95([7.0]) == 7.0
    assert p95([float(v) for v in range(1, 101)]) == 95.0
    assert p95([float(v) for v in range(1, 21)]) == 19.0


def test_device_hourly_without_readings_stays_none() -> None:
    (row,) = device_hourly([(10, "d", 10.0, None, None)], active_util_pct=10)
    _, _, samples, seconds, energy, busy, active, util_s, *_ = row
    assert (samples, seconds) == (1, 10.0)
    assert (energy, busy, active, util_s) == (None, None, None, None)


def test_device_hourly_active_threshold() -> None:
    rows = [(10, "d", 10.0, 9.9, 1.0), (20, "d", 10.0, 10.0, 1.0), (30, "d", 10.0, 80.0, 1.0)]
    (row,) = device_hourly(rows, active_util_pct=10)
    assert row[6] == pytest.approx(20.0)


def test_calls_hourly_counts_errors_and_latency() -> None:
    rows = [
        (1, "openai", "gpt", "normal", "c", 10, 1, None, 3, None, 0.5, 200),
        (2, "openai", "gpt", "normal", "c", 20, 2, None, None, None, None, 500),
    ]
    (row,) = calls_hourly(rows)
    assert row[5:] == (2, 30, 3, None, 3, None, 0.5, 1, 1)
