"""Per-app CPU and GPU time and attributed energy on a Mac (record 0011 item 3, D2.9).

Every tick reads the apps holding a GPU connection (IOKit) and the CPU time of
the apps worth watching: GPU users, the busiest apps of the last full scan and
local model runtimes. A full scan of all processes runs every ``full_scan_s``
(60 s by default), so the time of every readable app is still counted exactly,
just over a longer interval.

Energy is **attributed**, never measured per app:

- CPU energy is split by each app's share of the busy CPU seconds of the
  window since that app was last read. Energy of idle cores and of processes
  that need admin rights to read stays unattributed.
- GPU energy is split by each app's share of GPU time, against the larger of
  the apps' summed GPU time and the GPU's own busy time.
- Neural Engine and DRAM energy are not split; macOS shows no per-app share.

Apps with little use in an interval are added up into one ``(other apps)`` row
so totals stay exact without a row per idle process. The agent's own process
is always kept, named ``aimeterix agent``, so its tracking cost is visible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ai_meterix.core.model import LocalModel, ProcessSample

from ..runtimes import DetectedModel, Fetch, detect_models, local_fetch, runtime_of
from .probe import MacReaders

OTHER = "(other apps)"
SELF = "aimeterix agent"


@dataclass(frozen=True)
class _Mark:
    """Running sums at one moment: CPU energy and busy CPU seconds, both summed
    only over ticks where the energy was known."""

    t: float
    cpu_j: float = 0.0
    busy_s: float = 0.0

    def joules_per_busy_s(self, since: _Mark) -> float | None:
        busy = self.busy_s - since.busy_s
        return (self.cpu_j - since.cpu_j) / busy if busy > 0 else None


@dataclass
class _Seen:
    """What we knew about a process when we last read it."""

    start: int
    cpu_s: float
    mark: _Mark
    name: str


@dataclass
class _Row:
    pid: int
    name: str
    interval_s: float
    cpu_s: float | None = None
    gpu_s: float | None = None
    energy_j: float | None = None
    model_id: str | None = None


class AppTracker:
    def __init__(
        self,
        host_id: str,
        readers: MacReaders,
        *,
        full_scan_s: float = 60.0,
        top_n: int = 10,
        min_share: float = 0.005,
        fetch: Fetch = local_fetch,
        self_pid: int | None = None,
    ) -> None:
        self.host_id = host_id
        self.self_pid = self_pid
        self._r = readers
        self.full_scan_s = full_scan_s
        self.top_n = top_n
        self.min_share = min_share
        self._fetch = fetch
        self._seen: dict[int, _Seen] = {}
        self._gpu_prev: dict[int, int] | None = None
        self._mark = _Mark(0.0)
        self._full_mark: _Mark | None = None
        self._full_due = 0.0
        self._top: list[int] = []
        self._runtime_pids: set[int] = set()
        self._model_of: dict[int, str] = {}
        self._models: dict[str, LocalModel] = {}
        self._names: dict[tuple[int, int], str] = {}

    # --- helpers ---------------------------------------------------------------

    def _name(self, pid: int, start: int) -> str:
        if pid == self.self_pid:
            return SELF
        key = (pid, start)
        if key not in self._names:
            try:
                name = self._r.process_name(pid)
            except OSError:
                name = None
            self._names[key] = name or f"pid {pid}"
        return self._names[key]

    def _model_id(self, model: DetectedModel) -> str:
        return f"{self.host_id}:{model.runtime}:{model.model_name}"

    def _gpu_times(self, problems: list[str]) -> dict[int, int] | None:
        try:
            clients = self._r.gpu_clients()
        except Exception as exc:
            problems.append(f"apps.gpu: {exc or type(exc).__name__}")
            return None
        per_pid: dict[int, int] = {}
        for client in clients:
            pid, ns = client.get("pid"), client.get("gpu_ns")
            if isinstance(pid, int) and isinstance(ns, int):
                per_pid[pid] = per_pid.get(pid, 0) + ns
        return per_pid

    # --- one tick ----------------------------------------------------------------

    def sample(
        self,
        ts: datetime,
        t: float,
        interval_s: float,
        *,
        cpu_j: float | None,
        busy_cpu_s: float | None,
        gpu_j: float | None,
        gpu_busy_s: float | None,
        problems: list[str],
    ) -> tuple[list[ProcessSample], list[LocalModel]]:
        """Process samples for the tick that ended at monotonic time ``t``.

        ``cpu_j`` and ``gpu_j`` are the chip energy of the tick, ``busy_cpu_s``
        the busy CPU seconds of all cores, ``gpu_busy_s`` the GPU's busy time.
        The first call only records starting values."""
        first = self._full_mark is None
        prev_mark = self._mark
        if cpu_j is not None and busy_cpu_s is not None:
            mark = _Mark(t, prev_mark.cpu_j + cpu_j, prev_mark.busy_s + busy_cpu_s)
        else:
            mark = _Mark(t, prev_mark.cpu_j, prev_mark.busy_s)
        self._mark = mark

        gpu_now = self._gpu_times(problems)
        gpu_delta: dict[int, float] = {}
        if gpu_now is not None and self._gpu_prev is not None:
            for pid, ns in gpu_now.items():
                before = self._gpu_prev.get(pid, 0)
                if ns > before:
                    gpu_delta[pid] = (ns - before) / 1e9
        if gpu_now is not None:
            self._gpu_prev = gpu_now

        full_mark = self._full_mark
        full = first or t >= self._full_due
        always = {self.self_pid} if self.self_pid is not None else set()
        watch = (
            None if full else sorted(set(gpu_delta) | set(self._top) | self._runtime_pids | always)
        )
        try:
            usage = self._r.process_usage(watch)
        except Exception as exc:
            problems.append(f"apps.cpu: {exc or type(exc).__name__}")
            usage = []

        rows: dict[int, _Row] = {}
        cpu_deltas: dict[int, float] = {}
        alive: set[int] = set()
        for pid, cpu_s, start in usage:
            alive.add(pid)
            name = self._name(pid, start)
            prev = self._seen.get(pid)
            self._seen[pid] = _Seen(start, cpu_s, mark, name)
            if first:
                continue
            if prev is not None and prev.start == start:
                delta, since = max(cpu_s - prev.cpu_s, 0.0), prev.mark
            else:  # started (or became readable) since the last look
                delta = cpu_s
                since = full_mark if full and full_mark is not None else prev_mark
            cpu_deltas[pid] = delta
            ratio = mark.joules_per_busy_s(since)
            rows[pid] = _Row(
                pid,
                name,
                max(t - since.t, interval_s),
                cpu_s=delta,
                energy_j=None if ratio is None else delta * ratio,
            )

        models: list[LocalModel] = []
        if full:
            # After the first call the next tick is a full scan too, so the
            # busiest apps are known after one interval instead of a minute.
            self._full_mark = mark
            self._full_due = t if first else t + self.full_scan_s
            for pid in set(self._seen) - alive:
                del self._seen[pid]
            self._names = {k: v for k, v in self._names.items() if k[0] in alive}
            self._top = sorted(cpu_deltas, key=lambda p: -cpu_deltas[p])[: self.top_n]
            models = self._detect(ts, {pid: self._seen[pid].name for pid in alive}, problems)

        if not first:
            self._add_gpu(rows, gpu_delta, gpu_j, gpu_busy_s, interval_s)
        samples = self._samples(ts, rows, interval_s) if not first else []
        return samples, models

    def _add_gpu(
        self,
        rows: dict[int, _Row],
        gpu_delta: dict[int, float],
        gpu_j: float | None,
        gpu_busy_s: float | None,
        interval_s: float,
    ) -> None:
        summed = sum(gpu_delta.values())
        denominator = max(summed, gpu_busy_s or 0.0)
        for pid, gpu_s in gpu_delta.items():
            row = rows.get(pid)
            if row is None:  # a GPU user we cannot read the CPU time of (needs admin)
                seen = self._seen.get(pid)
                row = rows[pid] = _Row(pid, seen.name if seen else self._name(pid, 0), interval_s)
            row.gpu_s = gpu_s
            if gpu_j is not None and denominator > 0:
                share = gpu_j * gpu_s / denominator
                row.energy_j = share + (row.energy_j or 0.0)

    def _samples(
        self, ts: datetime, rows: dict[int, _Row], interval_s: float
    ) -> list[ProcessSample]:
        samples: list[ProcessSample] = []
        other: _Row | None = None
        for pid in sorted(rows):
            row = rows[pid]
            row.model_id = self._model_of.get(pid)
            small = (row.cpu_s or 0.0) < self.min_share * row.interval_s and not row.gpu_s
            if small and row.model_id is None and pid != self.self_pid:
                if not row.cpu_s:
                    continue
                if other is None:
                    other = _Row(0, OTHER, row.interval_s, cpu_s=0.0, energy_j=0.0)
                other.interval_s = max(other.interval_s, row.interval_s)
                other.cpu_s = (other.cpu_s or 0.0) + row.cpu_s
                if row.energy_j is None:
                    other.energy_j = None
                elif other.energy_j is not None:
                    other.energy_j += row.energy_j
                continue
            samples.append(self._sample(ts, row))
        if other is not None:
            samples.append(self._sample(ts, other))
        return samples

    def _sample(self, ts: datetime, row: _Row) -> ProcessSample:
        return ProcessSample(
            ts,
            self.host_id,
            row.name,
            row.pid,
            row.interval_s,
            cpu_time_s=row.cpu_s,
            gpu_time_s=row.gpu_s,
            attributed_energy_j=row.energy_j,
            local_model_id=row.model_id,
        )

    def _detect(
        self, ts: datetime, processes: dict[int, str], problems: list[str]
    ) -> list[LocalModel]:
        candidates = {pid: name for pid, name in processes.items() if runtime_of(name)}
        self._runtime_pids = set(candidates)

        def read_args(pid: int) -> list[str] | None:
            try:
                return self._r.process_args(pid)
            except OSError:
                return None

        found = detect_models(candidates, read_args, self._fetch, problems) if candidates else []
        self._model_of = {}
        current: list[LocalModel] = []
        for model in found:
            model_id = self._model_id(model)
            known = self._models.get(model_id)
            local = LocalModel(
                model_id,
                self.host_id,
                model.runtime,
                model.model_name,
                first_seen=known.first_seen if known else ts,
                last_seen=ts,
                size_bytes=model.size_bytes,
                pid=model.pid,
            )
            self._models[model_id] = local
            current.append(local)
            if model.pid is not None:
                self._model_of[model.pid] = model_id
        return current
