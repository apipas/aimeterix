"""SQL shared by the relational connectors: the query builder for ``UsageQuery``.

Timestamps are integer epoch milliseconds in every table built here, so bucket
arithmetic is plain integer maths and identical across databases. The SQL uses
only portable functions (SUM, MIN, MAX, COUNT, NULLIF, CASE).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ai_meterix.core.timeutil import to_epoch_ms

from .base import MEASURES, Dataset, UsageQuery


@dataclass(frozen=True)
class _Spec:
    source: str  # FROM clause; the fact table is aliased "f"
    dims: Mapping[str, str]
    measures: Mapping[str, str]


_DEVICE_DIMS = {"device_id": "f.device_id", "host_id": "d.host_id", "kind": "d.kind"}
_MEMORY_DIMS = {"pool_id": "f.pool_id", "host_id": "p.host_id", "pool_type": "p.pool_type"}
_PROCESS_DIMS = {
    "host_id": "f.host_id",
    "process_name": "f.process_name",
    "local_model_id": "f.local_model_id",
}
_CALL_DIMS = {
    "provider": "f.provider",
    "model": "f.model",
    "call_type": "f.call_type",
    "caller_id": "f.caller_id",
    "app": "c.app",
    "feature": "c.feature",
}
_TOKENS = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)


def _weighted(value: str, weight: str = "f.interval_s") -> str:
    return (
        f"SUM({value} * {weight}) / NULLIF(SUM(CASE WHEN {value} IS NOT NULL THEN {weight} END), 0)"
    )


SPECS: Mapping[Dataset, _Spec] = {
    Dataset.DEVICE: _Spec(
        "device_samples f LEFT JOIN devices d ON d.id = f.device_id",
        {**_DEVICE_DIMS, "source": "f.source"},
        {
            "samples": "COUNT(*)",
            "seconds": "SUM(f.interval_s)",
            "energy_j": "SUM(f.power_w * f.interval_s)",
            "busy_s": "SUM(f.util_pct / 100.0 * f.interval_s)",
            "util_mean": _weighted("f.util_pct"),
            "util_max": "MAX(f.util_pct)",
            "power_mean": _weighted("f.power_w"),
            "power_max": "MAX(f.power_w)",
        },
    ),
    Dataset.MEMORY: _Spec(
        "memory_samples f LEFT JOIN memory_pools p ON p.id = f.pool_id",
        {**_MEMORY_DIMS, "source": "f.source"},
        {
            "samples": "COUNT(*)",
            "seconds": "SUM(f.interval_s)",
            "used_mean": _weighted("f.used_bytes"),
            "used_max": "MAX(f.used_bytes)",
        },
    ),
    Dataset.PROCESS: _Spec(
        "process_samples f",
        {**_PROCESS_DIMS, "source": "f.source"},
        {
            "samples": "COUNT(*)",
            "seconds": "SUM(f.interval_s)",
            "cpu_time_s": "SUM(f.cpu_time_s)",
            "gpu_time_s": "SUM(f.gpu_time_s)",
            "energy_j": "SUM(f.attributed_energy_j)",
        },
    ),
    Dataset.CALLS: _Spec(
        "llm_calls f LEFT JOIN callers c ON c.id = f.caller_id",
        _CALL_DIMS,
        {
            "requests": "COUNT(*)",
            **{name: f"SUM(f.{name})" for name in _TOKENS},
            "latency_mean": "AVG(f.latency_s)",
            "errors": "SUM(CASE WHEN f.status >= 400 THEN 1 ELSE 0 END)",
        },
    ),
    Dataset.DEVICE_HOURLY: _Spec(
        "device_hourly f LEFT JOIN devices d ON d.id = f.device_id",
        _DEVICE_DIMS,
        {
            "samples": "SUM(f.samples)",
            "seconds": "SUM(f.seconds)",
            "energy_j": "SUM(f.energy_j)",
            "busy_s": "SUM(f.busy_s)",
            "active_s": "SUM(f.active_s)",
            "util_mean": "SUM(f.busy_s) * 100.0 / NULLIF(SUM(f.util_s), 0)",
            "util_min": "MIN(f.util_min)",
            "util_max": "MAX(f.util_max)",
            "util_p95": "MAX(f.util_p95)",
            "power_mean": "SUM(f.energy_j) / NULLIF(SUM(f.power_s), 0)",
            "power_min": "MIN(f.power_min)",
            "power_max": "MAX(f.power_max)",
            "power_p95": "MAX(f.power_p95)",
        },
    ),
    Dataset.MEMORY_HOURLY: _Spec(
        "memory_hourly f LEFT JOIN memory_pools p ON p.id = f.pool_id",
        _MEMORY_DIMS,
        {
            "samples": "SUM(f.samples)",
            "seconds": "SUM(f.seconds)",
            "used_mean": "SUM(f.used_byte_s) / NULLIF(SUM(f.used_s), 0)",
            "used_min": "MIN(f.used_min)",
            "used_max": "MAX(f.used_max)",
            "used_p95": "MAX(f.used_p95)",
        },
    ),
    Dataset.PROCESS_HOURLY: _Spec(
        "process_hourly f",
        _PROCESS_DIMS,
        {
            "samples": "SUM(f.samples)",
            "seconds": "SUM(f.seconds)",
            "cpu_time_s": "SUM(f.cpu_time_s)",
            "gpu_time_s": "SUM(f.gpu_time_s)",
            "energy_j": "SUM(f.energy_j)",
        },
    ),
    Dataset.CALLS_HOURLY: _Spec(
        "calls_hourly f LEFT JOIN callers c ON c.id = f.caller_id",
        _CALL_DIMS,
        {
            "requests": "SUM(f.requests)",
            **{name: f"SUM(f.{name})" for name in _TOKENS},
            "latency_mean": "SUM(f.latency_sum) / NULLIF(SUM(f.latency_n), 0)",
            "errors": "SUM(f.errors)",
        },
    ),
}

for _dataset, _spec in SPECS.items():
    assert tuple(_spec.measures) == MEASURES[_dataset], _dataset


def build_query(q: UsageQuery, placeholder: str = "?") -> tuple[str, list[Any]]:
    """Return SQL and parameters for ``q``. Output columns follow ``q.columns``.

    Rows are ordered by bucket, then group-by columns, with NULL first, so every
    connector returns the same order.
    """
    spec = SPECS[q.dataset]
    params: list[Any] = []
    select: list[str] = []
    group: list[str] = []
    if q.bucket is not None:
        bucket_ms = int(q.bucket.total_seconds() * 1000)
        expr = f"(f.ts - (f.ts % {bucket_ms}))"
        select.append(f"{expr} AS bucket")
        group.append(expr)
    for name in q.group_by:
        select.append(f"{spec.dims[name]} AS {name}")
        group.append(spec.dims[name])
    select.extend(f"{sql} AS {name}" for name, sql in spec.measures.items())

    where = [f"f.ts >= {placeholder}", f"f.ts < {placeholder}"]
    params.extend([to_epoch_ms(q.start), to_epoch_ms(q.end)])
    for name, values in q.filters.items():
        if not values:
            where.append("1 = 0")
            continue
        marks = ", ".join(placeholder for _ in values)
        where.append(f"{spec.dims[name]} IN ({marks})")
        params.extend(str(value) for value in values)

    sql = f"SELECT {', '.join(select)} FROM {spec.source} WHERE {' AND '.join(where)}"
    if group:
        order = ", ".join(f"{expr} IS NOT NULL, {expr}" for expr in group)
        sql += f" GROUP BY {', '.join(group)} ORDER BY {order}"
    return sql, params
