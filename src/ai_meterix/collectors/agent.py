"""The hardware agent loop (PLAN.md Phase 2 steps 1d and 1e, records 0011 and 0017).

Every ``interval_s`` the agent asks its backend for one ``SampleBatch`` and keeps
it in memory. Every ``flush_s`` it writes what it holds through ``StorageWriter``,
one kind of row at a time, so a failure never writes a row twice.

When the database cannot be reached, the rows stay in memory and a copy goes to
a buffer file in the data folder (``agent-buffer.jsonl``, ``core/paths.py``), so a
restart does not lose them. The next successful flush writes the buffer first
and deletes the file. The buffer is capped; when full, the oldest rows go first
and the loss is counted.

The agent measures its own footprint (CPU, peak memory, attributed energy)
against the budget of record 0017 and writes it to ``agent-status.json``. When
its CPU use goes over budget it doubles its interval, up to 300 s, and returns
towards the configured interval once it is well under. Burst mode (1 s for a
limited time) is exempt, since the user asked for it.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import os
import resource
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ai_meterix.core.model import (
    Caller,
    DeviceSample,
    LlmCall,
    LocalModel,
    MemorySample,
    ProcessSample,
    Project,
)
from ai_meterix.core.paths import paths
from ai_meterix.core.timeutil import utc_now
from ai_meterix.storage.base import StorageWriter

from .base import HardwareBackend, SampleBatch

log = logging.getLogger("ai_meterix.agent")

BUFFER_FILE = "agent-buffer.jsonl"
STATUS_FILE = "agent-status.json"
MAX_INTERVAL_S = 300.0
BURST_INTERVAL_S = 1.0

# Record 0017 budget for the agent.
CPU_BUDGET_PCT = 1.0
MEMORY_BUDGET_BYTES = 100 * 2**20
ENERGY_BUDGET_W = 0.1
BUDGET_WINDOW_S = 60.0

_KINDS: dict[str, type[Any]] = {
    "model": LocalModel,
    "device": DeviceSample,
    "memory": MemorySample,
    "process": ProcessSample,
    "caller": Caller,  # the proxy's buffer
    "call": LlmCall,
}
_DATETIMES = ("ts", "first_seen", "last_seen")


# --- buffer file ----------------------------------------------------------------


def to_record(kind: str, row: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"kind": kind}
    for f in dataclasses.fields(row):
        value = getattr(row, f.name)
        if isinstance(value, datetime):
            value = value.isoformat()
        elif hasattr(value, "value"):  # enums
            value = value.value
        data[f.name] = value
    return data


def from_record(data: dict[str, Any]) -> tuple[str, Any]:
    data = dict(data)
    kind = data.pop("kind")
    for name in _DATETIMES:
        if isinstance(data.get(name), str):
            data[name] = datetime.fromisoformat(data[name])
    return kind, _KINDS[kind](**data)


@dataclass
class Pending:
    """Rows waiting to be written, oldest first."""

    models: dict[str, LocalModel] = field(default_factory=dict)
    device: list[DeviceSample] = field(default_factory=list)
    memory: list[MemorySample] = field(default_factory=list)
    process: list[ProcessSample] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.models) + len(self.device) + len(self.memory) + len(self.process)

    def add(self, batch: SampleBatch) -> None:
        for model in batch.local_models:
            self.models[model.id] = model  # the latest sighting wins
        self.device.extend(batch.device_samples)
        self.memory.extend(batch.memory_samples)
        self.process.extend(batch.process_samples)

    def trim(self, limit: int) -> int:
        """Drop the oldest samples beyond ``limit`` rows; returns how many were dropped."""
        dropped = 0
        while len(self) > limit:
            lists: list[list[Any]] = [
                rows for rows in (self.device, self.memory, self.process) if rows
            ]
            if not lists:
                break
            del min(lists, key=lambda rows: rows[0].ts)[0]
            dropped += 1
        return dropped

    def records(self) -> Iterable[dict[str, Any]]:
        for model in self.models.values():
            yield to_record("model", model)
        for kind in ("device", "memory", "process"):
            for row in getattr(self, kind):
                yield to_record(kind, row)

    def load(self, records: Iterable[dict[str, Any]]) -> None:
        for record in records:
            kind, row = from_record(record)
            if kind == "model":
                self.models[row.id] = row
            else:
                getattr(self, kind).append(row)


# --- footprint ------------------------------------------------------------------


def _peak_rss_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)


@dataclass(frozen=True)
class Footprint:
    """The agent's own use since it started (record 0017 item 6)."""

    cpu_pct: float
    window_cpu_pct: float | None
    peak_rss_bytes: int
    energy_w: float | None

    def problems(self) -> list[str]:
        found = []
        if self.window_cpu_pct is not None and self.window_cpu_pct > CPU_BUDGET_PCT:
            found.append(f"agent CPU {self.window_cpu_pct:.2f}% is over {CPU_BUDGET_PCT}%")
        if self.peak_rss_bytes > MEMORY_BUDGET_BYTES:
            found.append(f"agent memory {self.peak_rss_bytes / 2**20:.0f} MB is over 100 MB")
        if self.energy_w is not None and self.energy_w > ENERGY_BUDGET_W:
            found.append(f"agent energy {self.energy_w:.3f} W is over {ENERGY_BUDGET_W} W")
        return found


class SelfMeter:
    """Tracks the agent's CPU time, memory and the energy attributed to it."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        cpu: Callable[[], float] = time.process_time,
        rss: Callable[[], int] = _peak_rss_bytes,
    ) -> None:
        self._clock, self._cpu, self._rss = clock, cpu, rss
        self._start = (clock(), cpu())
        self._window: list[tuple[float, float]] = [self._start]
        self._energy_j = 0.0
        self._energy_s = 0.0

    def add_energy(self, joules: float, interval_s: float) -> None:
        self._energy_j += joules
        self._energy_s += interval_s

    def footprint(self) -> Footprint:
        now, cpu = self._clock(), self._cpu()
        self._window.append((now, cpu))
        while len(self._window) > 2 and now - self._window[1][0] >= BUDGET_WINDOW_S:
            self._window.pop(0)
        t0, c0 = self._window[0]
        window = 100.0 * (cpu - c0) / (now - t0) if now - t0 >= BUDGET_WINDOW_S else None
        elapsed = now - self._start[0]
        total = 100.0 * (cpu - self._start[1]) / elapsed if elapsed > 0 else 0.0
        energy = self._energy_j / self._energy_s if self._energy_s > 0 else None
        return Footprint(total, window, self._rss(), energy)


# --- choosing a backend ------------------------------------------------------------


def open_backend(
    project_id: str, *, mock: bool = False, interval_s: float = 10.0
) -> HardwareBackend:
    """The backend for this machine, or the mock one. Raises ``RuntimeError``
    when this machine has no backend yet."""
    if mock:
        from .mock import MockBackend

        return MockBackend(project_id, interval_s=interval_s)
    if sys.platform == "darwin" and os.uname().machine == "arm64":
        from .macos.backend import AppleSiliconBackend

        return AppleSiliconBackend(project_id, self_pid=os.getpid())
    raise RuntimeError(
        "no hardware backend for this machine yet (Apple Silicon only, record 0009); "
        "use --mock for a demo"
    )


# --- the loop ---------------------------------------------------------------------


class Agent:
    def __init__(
        self,
        backend: HardwareBackend,
        open_storage: Callable[[], StorageWriter],
        *,
        interval_s: float = 10.0,
        flush_s: float = 30.0,
        data_dir: Path | None = None,
        max_buffer_rows: int = 200_000,
        burst_s: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
        meter: SelfMeter | None = None,
    ) -> None:
        if not 1.0 <= interval_s <= MAX_INTERVAL_S:
            raise ValueError("interval_s must be between 1 and 300 seconds")
        self.backend = backend
        self._open_storage = open_storage
        self._storage: StorageWriter | None = None
        self.base_interval_s = interval_s
        self.interval_s = interval_s
        self.flush_s = max(flush_s, interval_s)
        self.data_dir = (data_dir or paths().data_dir).expanduser()
        self.max_buffer_rows = max_buffer_rows
        self._clock = clock
        self._burst_until = clock() + burst_s if burst_s > 0 else None
        self.meter = meter or SelfMeter(clock)
        self.pending = Pending()
        self.dropped = 0
        self.written = 0
        self.last_error: str | None = None
        self._logged: set[str] = set()
        self._registered = False
        self._stop = threading.Event()
        self._load_buffer()

    @property
    def buffer_path(self) -> Path:
        return self.data_dir / BUFFER_FILE

    @property
    def status_path(self) -> Path:
        return self.data_dir / STATUS_FILE

    def current_interval(self) -> float:
        if self._burst_until is not None:
            if self._clock() < self._burst_until:
                return BURST_INTERVAL_S
            self._burst_until = None
            log.info("burst mode over; back to %.0f s", self.interval_s)
        return self.interval_s

    def set_interval(self, interval_s: float) -> None:
        """Change the configured interval while running (a live setting, record 0013)."""
        if not 1.0 <= interval_s <= MAX_INTERVAL_S:
            raise ValueError("interval_s must be between 1 and 300 seconds")
        self.base_interval_s = self.interval_s = interval_s
        self.flush_s = max(self.flush_s, interval_s)
        if hasattr(self.backend, "interval_s"):  # the mock backend steps by it
            self.backend.interval_s = interval_s
        log.info("sampling every %.0f s", interval_s)

    # --- one tick and one flush ---------------------------------------------------

    def tick(self) -> SampleBatch:
        batch = self.backend.sample()
        self.pending.add(batch)
        own = os.getpid()
        for sample in batch.process_samples:
            if sample.pid == own and sample.attributed_energy_j is not None:
                self.meter.add_energy(sample.attributed_energy_j, sample.interval_s)
        for problem in batch.problems:
            if problem not in self._logged:  # each problem is logged once per run
                log.warning("reading problem: %s", problem)
                self._logged.add(problem)
        self.dropped += self.pending.trim(self.max_buffer_rows)
        return batch

    def _connect(self) -> StorageWriter:
        if self._storage is None:
            storage = self._open_storage()
            storage.migrate()
            self._storage = storage
        if not self._registered:
            inventory = self.backend.inventory()
            project = inventory.host.project_id
            if all(p.id != project for p in self._storage.get_projects()):
                self._storage.upsert_projects([Project(project, project)])
            self._storage.upsert_hosts([inventory])
            self._registered = True
        return self._storage

    def flush(self) -> bool:
        """Write everything pending. Returns False when the database failed."""
        if not len(self.pending) and not self.buffer_path.exists():
            self._write_status()
            return True
        try:
            storage = self._connect()
            pending = self.pending
            if pending.models:
                storage.upsert_local_models(pending.models.values())
                pending.models.clear()
            for kind, write in (
                ("device", storage.write_device_samples),
                ("memory", storage.write_memory_samples),
                ("process", storage.write_process_samples),
            ):
                rows = getattr(pending, kind)
                if rows:
                    self.written += write(rows)
                    rows.clear()
        except Exception as exc:
            self._fail(exc)
            return False
        if self.last_error is not None:
            log.info("database reachable again; buffered rows written")
        self.last_error = None
        self.buffer_path.unlink(missing_ok=True)
        self._write_status()
        return True

    def _fail(self, exc: Exception) -> None:
        reason = f"{type(exc).__name__}: {exc}"
        if reason != self.last_error:
            log.warning("cannot write to the database, keeping rows in a buffer: %s", reason)
        self.last_error = reason
        if self._storage is not None:
            with contextlib.suppress(Exception):
                self._storage.close()
            self._storage = None
            self._registered = False
        self._save_buffer()
        self._write_status()

    # --- files in the data directory ---------------------------------------------

    def _save_buffer(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.buffer_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as out:
            for record in self.pending.records():
                out.write(json.dumps(record, separators=(",", ":")) + "\n")
        tmp.replace(self.buffer_path)

    def _load_buffer(self) -> None:
        if not self.buffer_path.exists():
            return
        try:
            lines = self.buffer_path.read_text(encoding="utf-8").splitlines()
            self.pending.load(json.loads(line) for line in lines if line.strip())
        except Exception as exc:
            log.warning("ignoring an unreadable buffer file %s: %s", self.buffer_path, exc)
            return
        log.info("loaded %d buffered rows from %s", len(self.pending), self.buffer_path)

    def _write_status(self) -> None:
        footprint = self.meter.footprint()
        status = {
            "updated": utc_now().isoformat(),
            "pid": os.getpid(),
            "backend": self.backend.name,
            "interval_s": self.current_interval(),
            "configured_interval_s": self.base_interval_s,
            "rows_written": self.written,
            "rows_pending": len(self.pending),
            "rows_dropped": self.dropped,
            "database_error": self.last_error,
            "footprint": dataclasses.asdict(footprint),
            "over_budget": footprint.problems(),
        }
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.status_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(status, indent=2), encoding="utf-8")
        tmp.replace(self.status_path)
        self._adjust_interval(footprint)

    def _adjust_interval(self, footprint: Footprint) -> None:
        if self._burst_until is not None or footprint.window_cpu_pct is None:
            return
        if footprint.window_cpu_pct > CPU_BUDGET_PCT and self.interval_s < MAX_INTERVAL_S:
            self.interval_s = min(self.interval_s * 2, MAX_INTERVAL_S)
            log.warning(
                "agent CPU %.2f%% is over budget; sampling every %.0f s",
                footprint.window_cpu_pct,
                self.interval_s,
            )
        elif (
            footprint.window_cpu_pct < CPU_BUDGET_PCT / 4 and self.interval_s > self.base_interval_s
        ):
            self.interval_s = max(self.interval_s / 2, self.base_interval_s)
            log.info("agent back under budget; sampling every %.0f s", self.interval_s)

    # --- running ------------------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    def run(self, duration_s: float | None = None) -> None:
        """Sample until ``stop`` is called (or ``duration_s`` has passed), then flush."""
        start = self._clock()
        next_tick = start
        last_flush = start
        try:
            while not self._stop.is_set():
                now = self._clock()
                if duration_s is not None and now - start >= duration_s:
                    break
                if now >= next_tick:
                    self.tick()
                    next_tick += self.current_interval()
                    if next_tick < now:  # we fell behind (sleep, suspend): do not catch up
                        next_tick = now + self.current_interval()
                if now - last_flush >= self.flush_s:
                    self.flush()
                    last_flush = now
                wait = next_tick - self._clock()
                if duration_s is not None:
                    wait = min(wait, start + duration_s - self._clock())
                if wait > 0:
                    self._stop.wait(wait)
        finally:
            self.close()

    def close(self) -> None:
        try:
            self.flush()
        finally:
            if self._storage is not None:
                self._storage.close()
                self._storage = None
            self.backend.close()
