"""Apple Silicon sampling backend (PLAN.md Phase 2 step 1b, records 0009 and 0011).

Each ``sample`` call reads the counters once and turns the change since the
previous call into samples:

- one device per CPU cluster (``cpu-p``, ``cpu-e``) with use and power,
- the GPU with use and power, and the Neural Engine with power only,
- one unified memory pool with used, wired, compressed and swap bytes, the
  GPU's share and the memory pressure,
- per-app CPU and GPU time with attributed energy, and the local models
  loaded by Ollama or llama.cpp (``AppTracker``).

Counters that macOS keeps as running totals (CPU ticks, energy) are turned into
deltas, so totals stay exact at any interval (0011 item 1). A reader that fails
leaves its values missing and is named in ``SampleBatch.problems``; a reader
that cannot start (IOReport refused, no GPU) is not retried every tick.

Run ``python -m ai_meterix.collectors.macos.backend`` on a Mac to print a few samples.
"""

from __future__ import annotations

import re
import socket
import sys
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from ai_meterix.core.model import (
    Device,
    DeviceKind,
    DeviceSample,
    Host,
    LocalModel,
    MemoryPool,
    MemoryPressure,
    MemorySample,
    PoolType,
    ProcessSample,
)
from ai_meterix.core.timeutil import utc_now
from ai_meterix.storage.base import HostInventory

from ..base import SampleBatch
from ..runtimes import Fetch, local_fetch
from .apps import AppTracker
from .probe import BACKEND, THERMAL_LEVELS, MacReaders, platform_info

PRESSURE = {1: MemoryPressure.NORMAL, 2: MemoryPressure.WARNING, 4: MemoryPressure.CRITICAL}
_P_CLUSTER = re.compile(r"^PACC\d*_CPU$")


def cluster_energy(channels: dict[str, float]) -> dict[str, float]:
    """Joules per part: ``P`` and ``E`` clusters, ``cpu`` total, ``gpu``, ``npu``, ``dram``."""
    parts: dict[str, float] = {}
    p = [value for name, value in channels.items() if _P_CLUSTER.match(name)]
    if p:
        parts["P"] = sum(p)
    if "EACC_CPU" in channels:
        parts["E"] = channels["EACC_CPU"]
    if "CPU Energy" in channels:
        parts["cpu"] = channels["CPU Energy"]
    elif "P" in parts or "E" in parts:
        parts["cpu"] = parts.get("P", 0.0) + parts.get("E", 0.0)
    gpu = channels.get("GPU Energy", channels.get("GPU"))
    if gpu is not None:
        parts["gpu"] = gpu
    ane = [value for name, value in channels.items() if name.startswith("ANE")]
    if ane:
        parts["npu"] = sum(ane)
    if "DRAM" in channels:
        parts["dram"] = channels["DRAM"]
    return parts


def _busy(before: list[int], after: list[int]) -> tuple[int, int]:
    delta = [max(b - a, 0) for a, b in zip(before, after, strict=True)]
    total = sum(delta)
    return total - delta[2], total


def _pct(busy: int, total: int) -> float | None:
    return min(100.0, max(0.0, 100.0 * busy / total)) if total else None


class AppleSiliconBackend:
    name = BACKEND

    def __init__(
        self,
        project_id: str,
        *,
        host_id: str | None = None,
        readers: MacReaders | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = utc_now,
        apps: bool = True,
        full_scan_s: float = 60.0,
        fetch: Fetch = local_fetch,
    ) -> None:
        if readers is None:
            from .native import NativeReaders

            readers = NativeReaders()
        self._r = readers
        self._clock = clock
        self._now = now
        self.info = platform_info(readers, socket.gethostname())
        self.host_id = host_id or self.info.hostname
        self.project_id = project_id
        try:
            layout = readers.cpu_layout()
        except OSError:
            layout = []
        # cluster type ("P", "E") per logical CPU; empty when the layout is unknown
        self._clusters: dict[int, str] = {int(cpu): str(kind) for cpu, kind in layout}
        self._page = readers.sysctl_int("hw.pagesize")
        self._disabled: dict[str, str] = {}
        self._prev_ticks: list[list[int]] | None = None
        self._prev_t: float | None = None
        self.apps = (
            AppTracker(self.host_id, readers, full_scan_s=full_scan_s, fetch=fetch)
            if apps
            else None
        )

    # --- ids -----------------------------------------------------------------

    def _id(self, part: str) -> str:
        return f"{self.host_id}:{part}"

    @property
    def pool_id(self) -> str:
        return self._id("unified")

    def _cpu_parts(self) -> list[str]:
        kinds = sorted(set(self._clusters.values()), key=lambda k: k != "P")
        return kinds or ["cpu"]

    def _cpu_device_id(self, part: str) -> str:
        return self._id("cpu" if part == "cpu" else f"cpu-{part.lower()}")

    def inventory(self) -> HostInventory:
        chip = self.info.chip
        names = {"P": "performance cores", "E": "efficiency cores"}
        devices = [
            Device(
                self._cpu_device_id(part),
                self.host_id,
                DeviceKind.CPU,
                f"{chip} {names[part]}" if chip and part in names else chip,
                self.pool_id,
            )
            for part in self._cpu_parts()
        ]
        devices.append(
            Device(self._id("gpu"), self.host_id, DeviceKind.GPU, self.info.gpu, self.pool_id)
        )
        devices.append(
            Device(
                self._id("npu"),
                self.host_id,
                DeviceKind.NPU,
                f"{chip} Neural Engine" if chip else None,
                self.pool_id,
            )
        )
        host = Host(
            self.host_id,
            self.project_id,
            self.info.hostname,
            "macos",
            "arm64",
            chip,
            tags={"model": self.info.model} if self.info.model else {},
        )
        pool = MemoryPool(self.pool_id, self.host_id, PoolType.UNIFIED, self.info.memory_bytes)
        return HostInventory(host, (pool,), tuple(devices))

    # --- sampling --------------------------------------------------------------

    def _read(self, name: str, fn: Callable[[], Any], problems: list[str]) -> Any:
        """Call a reader; on failure record the problem and return None."""
        if name in self._disabled:
            return None
        try:
            return fn()
        except Exception as exc:
            problems.append(f"{name}: {exc or type(exc).__name__}")
            return None

    def _start_once(self, name: str, fn: Callable[[], Any], problems: list[str]) -> Any:
        """Like ``_read`` but a failure disables the reader until restart."""
        if name in self._disabled:
            return None
        try:
            return fn()
        except Exception as exc:
            reason = str(exc) or type(exc).__name__
            self._disabled[name] = reason
            problems.append(f"{name}: {reason} (disabled until restart)")
            return None

    def sample(self) -> SampleBatch:
        problems: list[str] = []
        ticks = self._read("cpu", self._r.cpu_ticks, problems)
        energy = self._start_once("energy", self._r.energy_delta, problems)
        t = self._clock()
        prev_ticks, prev_t = self._prev_ticks, self._prev_t
        self._prev_ticks, self._prev_t = ticks, t
        if prev_t is None:
            models: list[LocalModel] = []
            if self.apps is not None:
                _, models = self.apps.sample(
                    self._now(),
                    t,
                    0.0,
                    cpu_j=None,
                    busy_cpu_s=None,
                    gpu_j=None,
                    gpu_busy_s=None,
                    problems=problems,
                )
            return SampleBatch(local_models=tuple(models), problems=tuple(problems))
        interval = t - prev_t
        if interval <= 0:
            return SampleBatch(problems=(*problems, "clock did not advance; tick skipped"))

        ts = self._now()
        gpu = self._start_once("gpu", self._r.gpu_stats, problems)
        vm = self._read("memory", self._r.vm_stats, problems)
        swap = self._read("swap", self._r.swap, problems)
        level = self._read(
            "pressure",
            lambda: self._r.sysctl_int("kern.memorystatus_vm_pressure_level"),
            problems,
        )
        thermal = self._read("thermal", self._r.thermal_level, problems)

        parts = cluster_energy(energy) if energy else {}
        extra: dict[str, Any] = {}
        if thermal is not None:
            extra["thermal"] = THERMAL_LEVELS.get(int(thermal), f"level {thermal}")

        def watts(part: str) -> float | None:
            return parts[part] / interval if part in parts else None

        devices: list[DeviceSample] = []
        pairs: list[tuple[list[int], list[int]]] = []
        if ticks is not None and prev_ticks is not None and len(ticks) == len(prev_ticks):
            pairs = list(zip(prev_ticks, ticks, strict=True))
        busy_cpu_s = None
        if pairs:
            busy_all, total_all = map(sum, zip(*(_busy(a, b) for a, b in pairs), strict=True))
            if total_all:
                busy_cpu_s = busy_all / total_all * len(pairs) * interval
        for part in self._cpu_parts():
            util = None
            if pairs:
                busy = total = 0
                for cpu, (before, after) in enumerate(pairs):
                    if part == "cpu" or self._clusters.get(cpu) == part:
                        b, n = _busy(before, after)
                        busy, total = busy + b, total + n
                util = _pct(busy, total)
            devices.append(
                DeviceSample(
                    ts, self._cpu_device_id(part), interval, util, watts(part), extra=dict(extra)
                )
            )

        gpu_util = gpu.get("Device Utilization %") if gpu else None
        devices.append(
            DeviceSample(
                ts,
                self._id("gpu"),
                interval,
                None if gpu_util is None else min(100.0, max(0.0, float(gpu_util))),
                watts("gpu"),
                extra=dict(extra),
            )
        )
        devices.append(DeviceSample(ts, self._id("npu"), interval, None, watts("npu")))

        memory = self._memory_sample(ts, interval, vm, swap, level, gpu, watts("dram"), problems)
        processes: list[ProcessSample] = []
        models = []
        if self.apps is not None:
            processes, models = self.apps.sample(
                ts,
                t,
                interval,
                cpu_j=parts.get("cpu"),
                busy_cpu_s=busy_cpu_s,
                gpu_j=parts.get("gpu"),
                gpu_busy_s=None if gpu_util is None else float(gpu_util) / 100 * interval,
                problems=problems,
            )
        return SampleBatch(
            tuple(devices), (memory,), tuple(processes), tuple(models), tuple(problems)
        )

    def _memory_sample(
        self,
        ts: datetime,
        interval: float,
        vm: dict[str, int] | None,
        swap: dict[str, int] | None,
        level: int | None,
        gpu: dict[str, float] | None,
        dram_w: float | None,
        problems: list[str],
    ) -> MemorySample:
        page = self._page
        used = wired = compressed = None
        if vm is not None and page:
            used = (
                vm["internal_page_count"]
                - vm["purgeable_count"]
                + vm["wire_count"]
                + vm["compressor_page_count"]
            ) * page
            wired = vm["wire_count"] * page
            compressed = vm["compressor_page_count"] * page
        pressure = PRESSURE.get(level) if level is not None else None
        if level is not None and pressure is None:
            problems.append(f"pressure: unknown level {level}")
        in_use = gpu.get("In use system memory") if gpu else None
        return MemorySample(
            ts,
            self.pool_id,
            interval,
            used_bytes=None if used is None else max(used, 0),
            wired_bytes=wired,
            compressed_bytes=compressed,
            swap_bytes=None if swap is None else int(swap["used"]),
            gpu_allocated_bytes=None if in_use is None else int(in_use),
            pressure=pressure,
            extra={} if dram_w is None else {"dram_w": round(dram_w, 4)},
        )

    def close(self) -> None:
        self._r.close()


def main(count: int = 3, interval_s: float = 2.0) -> None:
    backend = AppleSiliconBackend("default")
    try:
        for device in backend.inventory().devices:
            print(f"device {device.id}: {device.kind.value} {device.model or ''}")
        backend.sample()
        for _ in range(count):
            time.sleep(interval_s)
            batch = backend.sample()
            for s in batch.device_samples:
                util = "-" if s.util_pct is None else f"{s.util_pct:.0f}%"
                power = "-" if s.power_w is None else f"{s.power_w:.2f} W"
                print(f"{s.device_id:<28} use {util:>5}  power {power:>8}  {s.extra}")
            for m in batch.memory_samples:

                def gb(v: int | None) -> str:
                    return "-" if v is None else f"{v / 2**30:.1f} GB"

                print(
                    f"{m.pool_id:<28} used {gb(m.used_bytes)}, wired {gb(m.wired_bytes)}, "
                    f"compressed {gb(m.compressed_bytes)}, swap {gb(m.swap_bytes)}, "
                    f"gpu {gb(m.gpu_allocated_bytes)}, pressure "
                    f"{m.pressure.value if m.pressure else '-'} {m.extra}"
                )
            top = sorted(batch.process_samples, key=lambda p: -(p.attributed_energy_j or 0))
            for p in top[:8]:
                energy = "-" if p.attributed_energy_j is None else f"{p.attributed_energy_j:.2f} J"
                gpu_s = "-" if p.gpu_time_s is None else f"{p.gpu_time_s:.2f} s"
                cpu_s = "-" if p.cpu_time_s is None else f"{p.cpu_time_s:.2f} s"
                print(
                    f"app {p.process_name[:26]:<26} cpu {cpu_s:>8}  gpu {gpu_s:>8}"
                    f"  energy {energy:>9} (est.)  {p.local_model_id or ''}"
                )
            for model in batch.local_models:
                print(f"model {model.id} pid {model.pid} size {model.size_bytes}")
            for problem in batch.problems:
                print(f"problem: {problem}")
            print()
    finally:
        backend.close()


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 3)
