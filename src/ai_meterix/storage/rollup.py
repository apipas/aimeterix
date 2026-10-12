"""Hourly rollups computed from raw rows, independent of the database.

Each function takes raw rows sorted by nothing in particular and returns one
tuple per (hour, key) in the column order of the matching ``*_hourly`` table.
A row belongs to the hour that contains its ``ts`` (epoch milliseconds).
Sums of readings that were never taken stay ``None``.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any

HOUR_MS = 3_600_000

DEVICE_HOURLY_COLUMNS = (
    "ts",
    "device_id",
    "samples",
    "seconds",
    "energy_j",
    "busy_s",
    "active_s",
    "util_s",
    "util_min",
    "util_max",
    "util_p95",
    "power_s",
    "power_min",
    "power_max",
    "power_p95",
)
MEMORY_HOURLY_COLUMNS = (
    "ts",
    "pool_id",
    "samples",
    "seconds",
    "used_byte_s",
    "used_s",
    "used_min",
    "used_max",
    "used_p95",
)
PROCESS_HOURLY_COLUMNS = (
    "ts",
    "host_id",
    "process_name",
    "local_model_id",
    "samples",
    "seconds",
    "cpu_time_s",
    "gpu_time_s",
    "energy_j",
)
CALLS_HOURLY_COLUMNS = (
    "ts",
    "provider",
    "model",
    "call_type",
    "caller_id",
    "requests",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "latency_sum",
    "latency_n",
    "errors",
)


def hour_of(ts_ms: int) -> int:
    return ts_ms - ts_ms % HOUR_MS


def p95(values: Sequence[float]) -> float | None:
    """95th percentile by nearest rank (no interpolation)."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _sum(values: Iterable[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def device_hourly(
    rows: Iterable[tuple[int, str, float, float | None, float | None]],
    active_util_pct: float,
) -> list[tuple[Any, ...]]:
    """Rows are (ts, device_id, interval_s, util_pct, power_w)."""
    groups: dict[tuple[int, str], list[tuple[float, float | None, float | None]]] = defaultdict(
        list
    )
    for ts, device_id, interval_s, util, power in rows:
        groups[(hour_of(ts), device_id)].append((interval_s, util, power))
    out: list[tuple[Any, ...]] = []
    for (hour, device_id), items in sorted(groups.items()):
        utils = [(i, u) for i, u, _ in items if u is not None]
        powers = [(i, p) for i, _, p in items if p is not None]
        util_values = [u for _, u in utils]
        power_values = [p for _, p in powers]
        out.append(
            (
                hour,
                device_id,
                len(items),
                sum(i for i, _, _ in items),
                _sum(i * p for i, p in powers),
                _sum(i * u / 100.0 for i, u in utils),
                _sum(i for i, u in utils if u >= active_util_pct) if utils else None,
                _sum(i for i, _ in utils),
                min(util_values, default=None),
                max(util_values, default=None),
                p95(util_values),
                _sum(i for i, _ in powers),
                min(power_values, default=None),
                max(power_values, default=None),
                p95(power_values),
            )
        )
    return out


def memory_hourly(rows: Iterable[tuple[int, str, float, int | None]]) -> list[tuple[Any, ...]]:
    """Rows are (ts, pool_id, interval_s, used_bytes)."""
    groups: dict[tuple[int, str], list[tuple[float, int | None]]] = defaultdict(list)
    for ts, pool_id, interval_s, used_bytes in rows:
        groups[(hour_of(ts), pool_id)].append((interval_s, used_bytes))
    out: list[tuple[Any, ...]] = []
    for (hour, pool_id), items in sorted(groups.items()):
        used = [(i, u) for i, u in items if u is not None]
        values = [u for _, u in used]
        out.append(
            (
                hour,
                pool_id,
                len(items),
                sum(i for i, _ in items),
                _sum(i * u for i, u in used),
                _sum(i for i, _ in used),
                min(values, default=None),
                max(values, default=None),
                p95(values),
            )
        )
    return out


def process_hourly(
    rows: Iterable[
        tuple[int, str, str, str | None, float, float | None, float | None, float | None]
    ],
) -> list[tuple[Any, ...]]:
    """Rows are (ts, host_id, process_name, local_model_id, interval_s, cpu, gpu, energy_j)."""
    groups: dict[tuple[int, str, str, str | None], list[tuple[Any, ...]]] = defaultdict(list)
    for ts, host_id, name, model_id, interval_s, cpu, gpu, energy in rows:
        groups[(hour_of(ts), host_id, name, model_id)].append((interval_s, cpu, gpu, energy))
    out: list[tuple[Any, ...]] = []
    for key, items in sorted(groups.items(), key=lambda kv: (*kv[0][:3], kv[0][3] or "")):
        out.append(
            (
                *key,
                len(items),
                sum(item[0] for item in items),
                _sum(item[1] for item in items),
                _sum(item[2] for item in items),
                _sum(item[3] for item in items),
            )
        )
    return out


def calls_hourly(rows: Iterable[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    """Rows are (ts, provider, model, call_type, caller_id, input, output, reasoning,
    cache_read, cache_write, latency_s, status)."""
    groups: dict[tuple[int, str, str, str, str], list[tuple[Any, ...]]] = defaultdict(list)
    for ts, provider, model, call_type, caller_id, *rest in rows:
        groups[(hour_of(ts), provider, model, call_type, caller_id)].append(tuple(rest))
    out: list[tuple[Any, ...]] = []
    for key, items in sorted(groups.items()):
        latencies = [item[5] for item in items if item[5] is not None]
        out.append(
            (
                *key,
                len(items),
                *(_sum(item[n] for item in items) for n in range(5)),
                sum(latencies) if latencies else None,
                len(latencies),
                sum(1 for item in items if item[6] is not None and item[6] >= 400),
            )
        )
    return out
