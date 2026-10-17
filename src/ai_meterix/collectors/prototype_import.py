"""Import data recorded by the earlier ``computemeter`` prototype.

The prototype stored two tables in SQLite: ``hw_samples`` (one row per device
per interval, devices named ``cpu`` or ``gpu:<index>``, memory in MiB) and
``llm_calls`` (with a free-text ``project`` tag). They map onto the current
model like this (record 0010):

- each prototype host becomes a ``Host`` in one project; its OS and
  architecture were not recorded, so they are ``unknown``;
- ``cpu`` becomes a CPU device drawing from a ``system`` memory pool, and
  ``gpu:N`` a GPU device with its own ``vram`` pool;
- memory is converted from MiB to bytes and stored as ``MemorySample`` rows;
- rows the prototype marked as mocked keep ``source = mock``;
- the old ``project`` tag becomes the caller's ``feature`` (record 0014);
  cache token counts were never recorded and stay empty.

The prototype file is opened read only. Importing the same file twice is
refused unless ``force=True``, because rows would be counted twice.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import quote

from ai_meterix.core.errors import AiMeterixError
from ai_meterix.core.model import (
    Caller,
    Device,
    DeviceKind,
    DeviceSample,
    Host,
    LlmCall,
    MemoryPool,
    MemorySample,
    PoolType,
    Project,
    Source,
)
from ai_meterix.storage.base import HostInventory, StorageWriter

T = TypeVar("T")

MIB = 2**20
BATCH = 1000
IMPORTS_SETTING = "imports.computemeter"
_EXTRA = {"imported_from": "computemeter"}


@dataclass
class ImportReport:
    hosts: int = 0
    device_samples: int = 0
    memory_samples: int = 0
    calls: int = 0
    skipped: Counter[str] = field(default_factory=Counter)


def _ts(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def _batches(items: Iterable[T], size: int = BATCH) -> Iterator[list[T]]:
    batch: list[T] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _device_ids(host: str, device: str) -> tuple[str, DeviceKind, str, PoolType] | None:
    if device == "cpu":
        return f"{host}:cpu", DeviceKind.CPU, f"{host}:system", PoolType.SYSTEM
    if device.startswith("gpu:") and device[4:].isdigit():
        index = device[4:]
        return f"{host}:gpu{index}", DeviceKind.GPU, f"{host}:vram{index}", PoolType.VRAM
    return None


def _open(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise AiMeterixError(f"prototype database not found: {path}")
    conn = sqlite3.connect(f"file:{quote(str(path.resolve()))}?mode=ro", uri=True)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not tables & {"hw_samples", "llm_calls"}:
        conn.close()
        raise AiMeterixError(f"{path} is not a computemeter database (no hw_samples/llm_calls)")
    conn.row_factory = sqlite3.Row
    return conn


def _fingerprint(conn: sqlite3.Connection) -> str:
    parts = []
    for table in ("hw_samples", "llm_calls"):
        try:
            count, first, last = conn.execute(
                f"SELECT COUNT(*), MIN(ts), MAX(ts) FROM {table}"
            ).fetchone()
        except sqlite3.OperationalError:
            count, first, last = 0, None, None
        parts.append(f"{table}:{count}:{first}:{last}")
    return "|".join(parts)


def import_prototype(
    path: str | Path,
    storage: StorageWriter,
    *,
    project: Project | None = None,
    by: str = "import",
    force: bool = False,
) -> ImportReport:
    """Copy a prototype database into ``storage``. Returns what was imported or skipped."""
    project = project or Project("default", "Default project")
    report = ImportReport()
    conn = _open(Path(path).expanduser())
    try:
        fingerprint = _fingerprint(conn)
        done = storage.get_settings().get(IMPORTS_SETTING)
        previous: list[str] = list(done.value) if done else []
        if fingerprint in previous and not force:
            raise AiMeterixError(
                "this prototype database was already imported; use force=True to import it again"
            )
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        storage.upsert_projects([project])
        if "hw_samples" in tables:
            _import_hardware(conn, storage, project, report)
        if "llm_calls" in tables:
            _import_calls(conn, storage, report)
        storage.save_setting(IMPORTS_SETTING, [*previous, fingerprint], by=by)
    finally:
        conn.close()
    return report


def _import_hardware(
    conn: sqlite3.Connection, storage: StorageWriter, project: Project, report: ImportReport
) -> None:
    inventory: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT host, device, MAX(model) AS model, MAX(mem_total_mb) AS total "
        "FROM hw_samples GROUP BY host, device ORDER BY host, device"
    ):
        ids = _device_ids(row["host"], row["device"])
        if ids is None:
            continue
        device_id, kind, pool_id, pool_type = ids
        host = inventory.setdefault(row["host"], {"pools": [], "devices": []})
        total = None if row["total"] is None else round(row["total"] * MIB)
        host["pools"].append(MemoryPool(pool_id, row["host"], pool_type, total))
        host["devices"].append(Device(device_id, row["host"], kind, row["model"], pool_id))
    storage.upsert_hosts(
        HostInventory(
            Host(
                name, project.id, name, "unknown", "unknown", tags={"imported_from": "computemeter"}
            ),
            tuple(parts["pools"]),
            tuple(parts["devices"]),
        )
        for name, parts in inventory.items()
    )
    report.hosts = len(inventory)

    def samples() -> Iterator[tuple[DeviceSample, MemorySample | None]]:
        for row in conn.execute("SELECT * FROM hw_samples ORDER BY ts"):
            ids = _device_ids(row["host"], row["device"])
            if ids is None:
                report.skipped[f"unknown device {row['device']!r}"] += 1
                continue
            device_id, _, pool_id, _ = ids
            source = Source.MOCK if row["mocked"] else Source.MEASURED
            try:
                ts = _ts(row["ts"])
                device = DeviceSample(
                    ts,
                    device_id,
                    row["interval_s"],
                    row["util_pct"],
                    row["power_w"],
                    source,
                    _EXTRA,
                )
                memory = None
                if row["mem_used_mb"] is not None:
                    memory = MemorySample(
                        ts,
                        pool_id,
                        row["interval_s"],
                        used_bytes=round(row["mem_used_mb"] * MIB),
                        source=source,
                        extra=_EXTRA,
                    )
            except (ValueError, TypeError, OverflowError) as exc:
                report.skipped[f"invalid hardware row: {exc}"] += 1
                continue
            yield device, memory

    for batch in _batches(samples()):
        report.device_samples += storage.write_device_samples(d for d, _ in batch)
        report.memory_samples += storage.write_memory_samples(m for _, m in batch if m)


def _import_calls(conn: sqlite3.Connection, storage: StorageWriter, report: ImportReport) -> None:
    callers: dict[str, Caller] = {}

    def calls() -> Iterator[LlmCall]:
        for row in conn.execute("SELECT * FROM llm_calls ORDER BY ts"):
            feature = row["project"] or None
            caller_id = f"prototype:{feature}" if feature else "prototype"
            if caller_id not in callers:
                callers[caller_id] = Caller(caller_id, "prototype", feature)
                storage.upsert_callers([callers[caller_id]])
            latency = row["latency_ms"]
            try:
                yield LlmCall(
                    _ts(row["ts"]),
                    row["provider"] or "unknown",
                    row["model"] or "unknown",
                    caller_id,
                    input_tokens=row["input_tokens"],
                    output_tokens=row["output_tokens"],
                    latency_s=None if latency is None else latency / 1000.0,
                    status=row["status"],
                    raw_usage={"imported_from": "computemeter"},
                )
            except (ValueError, TypeError, OverflowError) as exc:
                report.skipped[f"invalid call row: {exc}"] += 1

    for batch in _batches(calls()):
        report.calls += storage.write_calls(batch)
