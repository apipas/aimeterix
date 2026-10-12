"""Probe for Apple Silicon Macs: detect the machine and check every metric.

The probe reads through a ``MacReaders`` object. ``NativeReaders`` calls the
system; ``RecordingReaders`` keeps what it read (``ProbeReport.readings``) so a
run on a real Mac can be saved and replayed in tests with ``ReplayReaders``.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Callable
from typing import Any, Protocol

from ai_meterix.core.model import PoolType

from ..probe import (
    CpuCluster,
    MetricCheck,
    PlatformInfo,
    ProbeReport,
    Support,
    run_check,
)

BACKEND = "macos-apple-silicon"
IOREPORT = "IOReport energy counters (undocumented macOS API)"
PRESSURE_LEVELS = {1: "normal", 2: "warning", 4: "critical"}
THERMAL_LEVELS = {0: "nominal", 1: "moderate", 2: "heavy", 3: "trapping", 4: "sleeping"}
CLUSTER_NAMES = {"P": "Performance", "E": "Efficiency"}


class MacReaders(Protocol):
    def sysctl_str(self, name: str) -> str | None: ...
    def sysctl_int(self, name: str) -> int | None: ...
    def swap(self) -> dict[str, int] | None: ...
    def cpu_ticks(self) -> list[list[int]]: ...
    def cpu_layout(self) -> list[list[Any]]: ...
    def vm_stats(self) -> dict[str, int]: ...
    def gpu(self) -> list[dict[str, Any]]: ...
    def gpu_clients(self) -> list[dict[str, Any]]: ...
    def energy(self, interval_s: float) -> dict[str, float]: ...
    def thermal_level(self) -> int: ...
    def processes(self) -> dict[str, Any]: ...
    def gpu_stats(self) -> dict[str, float]: ...
    def energy_delta(self) -> dict[str, float]: ...
    def process_usage(self, pids: list[int] | None = None) -> list[list[Any]]: ...
    def process_name(self, pid: int) -> str | None: ...
    def process_args(self, pid: int) -> list[str] | None: ...
    def close(self) -> None: ...


_KEYED = ("sysctl_str", "sysctl_int", "process_name", "process_args")
_CALLS = (
    "swap",
    "cpu_ticks",
    "cpu_layout",
    "vm_stats",
    "gpu",
    "gpu_clients",
    "energy",
    "thermal_level",
    "processes",
    "gpu_stats",
    "energy_delta",
    "process_usage",
)


class RecordingReaders:
    """Wraps readers and records every result (or error) in ``readings``."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.readings: dict[str, Any] = {}

    def __getattr__(self, name: str) -> Callable[..., Any]:
        method = getattr(self._inner, name)
        if name not in _KEYED + _CALLS:
            return method  # type: ignore[no-any-return]

        def call(*args: Any) -> Any:
            try:
                result = method(*args)
                entry: Any = result
            except Exception as exc:
                entry = {"error": str(exc) or type(exc).__name__}
                result = exc
            if name in _KEYED:
                self.readings.setdefault(name, {})[str(args[0])] = entry
            else:
                self.readings.setdefault(name, []).append(entry)
            if isinstance(result, Exception):
                raise result
            return result

        return call


class ReplayReaders:
    """Plays back ``readings`` recorded on a Mac. Repeated calls take the next
    recorded result, then repeat the last one."""

    def __init__(self, readings: dict[str, Any]) -> None:
        self._readings = readings
        self._next: dict[str, int] = {}

    def _keyed(self, kind: str, name: object) -> Any:
        return self._unwrap(self._readings.get(kind, {}).get(str(name)))

    def _call(self, kind: str) -> Any:
        results = self._readings.get(kind)
        if not results:
            raise OSError(f"no recorded {kind} reading")
        index = min(self._next.get(kind, 0), len(results) - 1)
        self._next[kind] = index + 1
        return self._unwrap(results[index])

    @staticmethod
    def _unwrap(entry: Any) -> Any:
        if isinstance(entry, dict) and set(entry) == {"error"}:
            raise OSError(entry["error"])
        return entry

    def sysctl_str(self, name: str) -> str | None:
        value = self._keyed("sysctl_str", name)
        return None if value is None else str(value)

    def sysctl_int(self, name: str) -> int | None:
        value = self._keyed("sysctl_int", name)
        return None if value is None else int(value)

    def swap(self) -> dict[str, int] | None:
        return self._call("swap")  # type: ignore[no-any-return]

    def cpu_ticks(self) -> list[list[int]]:
        return self._call("cpu_ticks")  # type: ignore[no-any-return]

    def cpu_layout(self) -> list[list[Any]]:
        return self._call("cpu_layout")  # type: ignore[no-any-return]

    def vm_stats(self) -> dict[str, int]:
        return self._call("vm_stats")  # type: ignore[no-any-return]

    def gpu(self) -> list[dict[str, Any]]:
        return self._call("gpu")  # type: ignore[no-any-return]

    def gpu_clients(self) -> list[dict[str, Any]]:
        return self._call("gpu_clients")  # type: ignore[no-any-return]

    def energy(self, interval_s: float) -> dict[str, float]:
        return self._call("energy")  # type: ignore[no-any-return]

    def thermal_level(self) -> int:
        return int(self._call("thermal_level"))

    def processes(self) -> dict[str, Any]:
        return self._call("processes")  # type: ignore[no-any-return]

    def gpu_stats(self) -> dict[str, float]:
        return self._call("gpu_stats")  # type: ignore[no-any-return]

    def energy_delta(self) -> dict[str, float]:
        return self._call("energy_delta")  # type: ignore[no-any-return]

    def process_usage(self, pids: list[int] | None = None) -> list[list[Any]]:
        rows: list[list[Any]] = self._call("process_usage")
        return rows if pids is None else [row for row in rows if row[0] in set(pids)]

    def process_name(self, pid: int) -> str | None:
        value = self._keyed("process_name", pid)
        return None if value is None else str(value)

    def process_args(self, pid: int) -> list[str] | None:
        value = self._keyed("process_args", pid)
        return None if value is None else [str(arg) for arg in value]

    def close(self) -> None:
        pass


def _gb(value: float) -> str:
    return f"{value / 2**30:.1f} GB"


def _pct(value: float) -> str:
    return f"{value:.0f}%"


def _busy(before: list[int], after: list[int]) -> tuple[int, int]:
    """Busy and total ticks between two [user, system, idle, nice] readings."""
    delta = [max(b - a, 0) for a, b in zip(before, after, strict=True)]
    total = sum(delta)
    return total - delta[2], total


def energy_by_part(channels: dict[str, float]) -> dict[str, float]:
    """Sum IOReport channels into CPU, GPU and Neural Engine joules."""
    parts: dict[str, float] = {}
    if "CPU Energy" in channels:
        parts["cpu"] = channels["CPU Energy"]
    else:
        cpu = [value for name, value in channels.items() if name.endswith("CPU Energy")]
        if cpu:
            parts["cpu"] = sum(cpu)
    gpu = channels.get("GPU Energy", channels.get("GPU"))
    if gpu is not None:
        parts["gpu"] = gpu
    ane = [value for name, value in channels.items() if name.startswith("ANE")]
    if ane:
        parts["npu"] = sum(ane)
    return parts


def platform_info(r: MacReaders, hostname: str) -> PlatformInfo:
    notes: list[str] = []
    clusters: list[CpuCluster] = []
    for level in range(r.sysctl_int("hw.nperflevels") or 0):
        name = r.sysctl_str(f"hw.perflevel{level}.name") or f"Cluster {level}"
        cores = r.sysctl_int(f"hw.perflevel{level}.logicalcpu")
        if cores:
            clusters.append(CpuCluster(name, cores))
    try:
        gpus = r.gpu()
    except OSError:
        gpus = []
    gpu = gpus[0] if gpus else {}
    if r.sysctl_int("sysctl.proc_translated") == 1:
        notes.append("Python runs under Rosetta; use a native arm64 Python for full readings")
    return PlatformInfo(
        os="macos",
        os_version=r.sysctl_str("kern.osproductversion") or "unknown",
        arch="arm64",
        hostname=hostname,
        chip=r.sysctl_str("machdep.cpu.brand_string"),
        model=r.sysctl_str("hw.model"),
        cpu_cores=r.sysctl_int("hw.ncpu"),
        clusters=tuple(clusters),
        gpu=gpu.get("model"),
        gpu_cores=gpu.get("cores"),
        memory_bytes=r.sysctl_int("hw.memsize"),
        memory_type=PoolType.UNIFIED,
        backend=BACKEND,
        notes=tuple(notes),
    )


def _checks(r: MacReaders, info: PlatformInfo, interval_s: float) -> list[MetricCheck]:
    checks: list[MetricCheck] = []

    ticks: list[list[list[int]]] = []

    def cpu_util() -> MetricCheck:
        ticks.append(r.cpu_ticks())
        time.sleep(interval_s)
        ticks.append(r.cpu_ticks())
        busy = total = 0
        for before, after in zip(*ticks, strict=True):
            b, t = _busy(before, after)
            busy, total = busy + b, total + t
        value = _pct(100.0 * busy / total) if total else None
        return MetricCheck("cpu.util", Support.SUPPORTED, "host_processor_info", value)

    def cpu_clusters() -> MetricCheck:
        if len(ticks) != 2:
            raise OSError("CPU ticks are not readable")
        layout = r.cpu_layout()
        per: dict[str, list[int]] = {}
        if layout and len(layout) == len(ticks[0]):
            for cpu_id, kind in layout:
                b, t = _busy(ticks[0][cpu_id], ticks[1][cpu_id])
                acc = per.setdefault(kind, [0, 0])
                acc[0] += b
                acc[1] += t
            value = ", ".join(
                f"{CLUSTER_NAMES.get(k, k)} {_pct(100.0 * b / t) if t else '-'}"
                for k, (b, t) in sorted(per.items(), key=lambda item: item[0] != "P")
            )
            return MetricCheck("cpu.clusters", Support.SUPPORTED, "IOKit device tree", value)
        if len(info.clusters) > 1:
            return MetricCheck(
                "cpu.clusters",
                Support.PARTIAL,
                "sysctl hw.perflevel*",
                reason="core counts known, but not which core is which",
            )
        raise OSError("this Mac reports no P/E cluster layout")

    checks.append(run_check("cpu.util", cpu_util))
    checks.append(run_check("cpu.clusters", cpu_clusters))

    energy: dict[str, float] = {}
    energy_error: list[str] = []
    try:
        energy = energy_by_part(r.energy(interval_s))
    except Exception as exc:
        energy_error.append(str(exc) or type(exc).__name__)

    def power(metric: str, part: str, support: Support, reason: str | None = None) -> MetricCheck:
        if energy_error:
            raise OSError(energy_error[0])
        if part not in energy:
            raise OSError(f"IOReport has no {part.upper()} energy channel on this chip")
        watts = energy[part] / interval_s
        return MetricCheck(metric, support, IOREPORT, f"{watts:.2f} W", reason)

    def gpu_stats() -> dict[str, Any]:
        gpus = r.gpu()
        if not gpus:
            raise OSError("no GPU found in IOKit (IOAccelerator)")
        stats = gpus[0].get("stats") or {}
        if not stats:
            raise OSError("the GPU publishes no performance statistics")
        return dict(stats)

    def gpu_util() -> MetricCheck:
        stats = gpu_stats()
        if "Device Utilization %" not in stats:
            raise OSError("the GPU statistics have no 'Device Utilization %'")
        return MetricCheck(
            "gpu.util",
            Support.SUPPORTED,
            "IOKit GPU statistics",
            _pct(float(stats["Device Utilization %"])),
        )

    def gpu_memory() -> MetricCheck:
        stats = gpu_stats()
        if "In use system memory" not in stats:
            raise OSError("the GPU statistics have no 'In use system memory'")
        return MetricCheck(
            "gpu.memory",
            Support.PARTIAL,
            "IOKit GPU statistics",
            _gb(float(stats["In use system memory"])),
            "a share of unified memory, not separate GPU memory",
        )

    checks.append(run_check("cpu.power", lambda: power("cpu.power", "cpu", Support.SUPPORTED)))
    checks.append(run_check("gpu.util", gpu_util))
    checks.append(run_check("gpu.memory", gpu_memory))
    checks.append(run_check("gpu.power", lambda: power("gpu.power", "gpu", Support.SUPPORTED)))
    checks.append(
        run_check(
            "npu.power",
            lambda: power(
                "npu.power",
                "npu",
                Support.PARTIAL,
                "energy only; macOS shows no Neural Engine use or per-app share",
            ),
        )
    )

    def page_size() -> int:
        size = r.sysctl_int("hw.pagesize")
        if not size:
            raise OSError("page size is not readable")
        return size

    def memory_used() -> MetricCheck:
        vm = r.vm_stats()
        pages = (
            vm["internal_page_count"]
            - vm["purgeable_count"]
            + vm["wire_count"]
            + vm["compressor_page_count"]
        )
        used = pages * page_size()
        total = f" of {_gb(info.memory_bytes)}" if info.memory_bytes else ""
        return MetricCheck("memory.used", Support.SUPPORTED, "host_statistics64", _gb(used) + total)

    def memory_detail() -> MetricCheck:
        vm = r.vm_stats()
        size = page_size()
        swap = r.swap()
        if swap is None:
            raise OSError("swap usage is not readable")
        value = (
            f"wired {_gb(vm['wire_count'] * size)}, "
            f"compressed {_gb(vm['compressor_page_count'] * size)}, swap {_gb(swap['used'])}"
        )
        return MetricCheck(
            "memory.detail", Support.SUPPORTED, "host_statistics64, sysctl vm.swapusage", value
        )

    def memory_pressure() -> MetricCheck:
        level = r.sysctl_int("kern.memorystatus_vm_pressure_level")
        if level not in PRESSURE_LEVELS:
            raise OSError(f"unknown memory pressure level {level!r}")
        return MetricCheck(
            "memory.pressure", Support.SUPPORTED, "sysctl memorystatus", PRESSURE_LEVELS[level]
        )

    def thermal() -> MetricCheck:
        level = r.thermal_level()
        return MetricCheck(
            "thermal.state",
            Support.SUPPORTED,
            "thermal pressure notification",
            THERMAL_LEVELS.get(level, f"level {level}"),
        )

    def process_cpu() -> MetricCheck:
        procs = r.processes()
        listed, readable = int(procs["listed"]), int(procs["readable"])
        value = f"{readable} of {listed} processes readable"
        if readable >= listed:
            return MetricCheck("process.cpu_time", Support.SUPPORTED, "libproc", value)
        if readable:
            return MetricCheck(
                "process.cpu_time",
                Support.PARTIAL,
                "libproc",
                value,
                "system processes need admin rights; their time counts as unattributed",
            )
        raise OSError("no process CPU time is readable")

    def process_gpu() -> MetricCheck:
        clients = r.gpu_clients()
        timed = [c for c in clients if c.get("gpu_ns") is not None]
        if timed:
            return MetricCheck(
                "process.gpu_time",
                Support.SUPPORTED,
                "IOKit GPU clients",
                f"{len({c.get('pid') for c in timed})} apps using the GPU",
            )
        if clients:
            return MetricCheck(
                "process.gpu_time",
                Support.PARTIAL,
                "IOKit GPU clients",
                f"{len(clients)} GPU connections",
                "apps are visible but their GPU time is not",
            )
        raise OSError("no GPU clients visible")

    checks.append(run_check("memory.used", memory_used))
    checks.append(run_check("memory.detail", memory_detail))
    checks.append(run_check("memory.pressure", memory_pressure))
    checks.append(run_check("thermal.state", thermal))
    checks.append(run_check("process.cpu_time", process_cpu))
    checks.append(run_check("process.gpu_time", process_gpu))
    return checks


def probe_macos(
    readers: MacReaders | None = None,
    *,
    interval_s: float = 0.25,
    hostname: str | None = None,
) -> ProbeReport:
    owned = readers is None
    if readers is None:
        from .native import NativeReaders

        readers = NativeReaders()
    recording = RecordingReaders(readers)
    r: MacReaders = recording
    try:
        info = platform_info(r, hostname or socket.gethostname())
        checks = _checks(r, info, interval_s)
    finally:
        if owned:
            readers.close()
    return ProbeReport(info, tuple(checks), readings=recording.readings)
