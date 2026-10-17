"""Platform probe (record 0009).

The probe detects the machine (OS, architecture, chip, cores, memory) and
checks, by actually reading each source once, whether every metric is
**supported**, **partial** or **unsupported** here, and why. ``aimeterix
doctor`` prints the result. Nothing is faked: a metric the probe cannot read is
reported as unsupported and later stored as missing, never as zero.

The probe only reads. It changes no setting and needs no admin rights (0017).
"""

from __future__ import annotations

import os
import platform
import socket
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from ai_meterix.core.model import PoolType
from ai_meterix.core.timeutil import utc_now


class Support(str, Enum):
    SUPPORTED = "supported"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"


# Every platform reports the same metrics, in this order, so gaps are visible.
METRICS: dict[str, str] = {
    "cpu.util": "CPU use",
    "cpu.clusters": "CPU use per P/E cluster",
    "cpu.power": "CPU power",
    "gpu.util": "GPU use",
    "gpu.memory": "GPU memory in use",
    "gpu.power": "GPU power",
    "npu.power": "Neural Engine power",
    "memory.used": "Memory used",
    "memory.detail": "Wired, compressed and swap",
    "memory.pressure": "Memory pressure",
    "thermal.state": "Thermal state",
    "process.cpu_time": "Per-app CPU time",
    "process.gpu_time": "Per-app GPU time",
}


@dataclass(frozen=True)
class MetricCheck:
    """One metric on this machine. ``value`` is a reading taken by the probe."""

    metric: str
    support: Support
    source: str | None = None
    value: str | None = None
    reason: str | None = None

    @property
    def label(self) -> str:
        return METRICS.get(self.metric, self.metric)


@dataclass(frozen=True)
class CpuCluster:
    name: str  # "Performance", "Efficiency", or "CPU" when the OS has no clusters
    cores: int


@dataclass(frozen=True)
class PlatformInfo:
    os: str  # "macos", "linux", "windows", ...
    os_version: str
    arch: str
    hostname: str
    chip: str | None = None
    model: str | None = None  # e.g. "Mac16,5"
    cpu_cores: int | None = None
    clusters: tuple[CpuCluster, ...] = ()
    gpu: str | None = None
    gpu_cores: int | None = None
    memory_bytes: int | None = None
    memory_type: PoolType | None = None
    backend: str | None = None  # None: no backend for this platform yet
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProbeReport:
    platform: PlatformInfo
    checks: tuple[MetricCheck, ...]
    probed_at: datetime = field(default_factory=utc_now)
    readings: dict[str, Any] = field(default_factory=dict)

    def check(self, metric: str) -> MetricCheck:
        for item in self.checks:
            if item.metric == metric:
                return item
        raise KeyError(metric)

    def counts(self) -> dict[Support, int]:
        counts = dict.fromkeys(Support, 0)
        for item in self.checks:
            counts[item.support] += 1
        return counts

    def to_dict(self, *, readings: bool = False) -> dict[str, Any]:
        """JSON-safe form. ``readings`` adds the raw values the probe read."""
        info = asdict(self.platform)
        info["memory_type"] = self.platform.memory_type.value if self.platform.memory_type else None
        data: dict[str, Any] = {
            "probed_at": self.probed_at.isoformat(),
            "platform": info,
            "checks": [
                {**asdict(item), "support": item.support.value, "label": item.label}
                for item in self.checks
            ],
        }
        if readings:
            data["readings"] = self.readings
        return data


def run_check(metric: str, fn: Callable[[], MetricCheck]) -> MetricCheck:
    """Run one check; any error makes the metric unsupported with the error as reason."""
    try:
        return fn()
    except Exception as exc:
        reason = str(exc) or type(exc).__name__
        return MetricCheck(metric, Support.UNSUPPORTED, reason=reason)


def unsupported_all(reason: str) -> tuple[MetricCheck, ...]:
    return tuple(MetricCheck(metric, Support.UNSUPPORTED, reason=reason) for metric in METRICS)


def _linux_info() -> PlatformInfo:
    chip = None
    memory = None
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                chip = line.split(":", 1)[1].strip()
                break
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                memory = int(line.split()[1]) * 1024
                break
    except OSError:
        pass
    cores = os.cpu_count()
    return PlatformInfo(
        os="linux",
        os_version=platform.release(),
        arch=platform.machine(),
        hostname=socket.gethostname(),
        chip=chip,
        cpu_cores=cores,
        clusters=(CpuCluster("CPU", cores),) if cores else (),
        memory_bytes=memory,
        memory_type=PoolType.SYSTEM,
    )


def _generic_info() -> PlatformInfo:
    name = platform.system().lower() or "unknown"
    cores = os.cpu_count()
    return PlatformInfo(
        os="macos" if name == "darwin" else name,
        os_version=platform.release(),
        arch=platform.machine(),
        hostname=socket.gethostname(),
        cpu_cores=cores,
        clusters=(CpuCluster("CPU", cores),) if cores else (),
    )


def probe(*, interval_s: float = 0.25) -> ProbeReport:
    """Probe this machine. ``interval_s`` is the gap between the two readings
    used for rates (CPU use, power)."""
    system = platform.system()
    if system == "Darwin" and platform.machine() == "arm64":
        from .macos.probe import probe_macos

        return probe_macos(interval_s=interval_s)
    if system == "Darwin":
        info = _generic_info()
        return ProbeReport(
            info, unsupported_all("Intel Macs are not supported; Apple Silicon only (record 0009)")
        )
    if system == "Linux":
        info = _linux_info()
        return ProbeReport(
            info,
            unsupported_all("no Linux backend yet; Linux and NVIDIA come next (record 0009)"),
        )
    return ProbeReport(
        _generic_info(), unsupported_all(f"{system or 'this system'} is not supported yet")
    )
