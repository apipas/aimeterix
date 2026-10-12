"""Readers for macOS system counters, called through ctypes (record 0011 item 2).

Each method returns plain, JSON-safe values so a probe run can be recorded on a
Mac and replayed in tests anywhere (``ReplayReaders``). Process names are never
returned: only counts, pids and times.

Sources, all readable without admin rights:

- ``sysctl``: chip, model, OS version, memory size, P/E clusters, swap, memory pressure.
- Mach host statistics: CPU ticks per core and virtual-memory page counts.
- IOKit: the CPU layout from the device tree, GPU statistics and per-app GPU time
  from the ``IOAccelerator`` service and its user clients.
- IOReport (undocumented): the "Energy Model" counters for CPU, GPU and Neural Engine.
- ``notify``: the system thermal-pressure level.
- libproc: the process list and each process's CPU time.
"""

from __future__ import annotations

import ctypes
import os
import re
import struct
import time
from ctypes import (
    POINTER,
    Structure,
    byref,
    c_char_p,
    c_int,
    c_size_t,
    c_uint32,
    c_uint64,
    c_void_p,
)
from functools import cache
from typing import Any

from . import _cf

_LIBSYSTEM = "/usr/lib/libSystem.B.dylib"
_IOREPORT = "/usr/lib/libIOReport.dylib"

HOST_VM_INFO64 = 4
PROCESSOR_CPU_LOAD_INFO = 2
RUSAGE_INFO_V2 = 2
THERMAL_NOTIFY = b"com.apple.system.thermalpressurelevel"
ENERGY_UNITS = {"J": 1.0, "mJ": 1e-3, "uJ": 1e-6, "µJ": 1e-6, "nJ": 1e-9}

_CLIENT_PID = re.compile(r"pid\s+(\d+)")


class _VmStatistics64(Structure):
    _fields_ = [
        ("free_count", c_uint32),
        ("active_count", c_uint32),
        ("inactive_count", c_uint32),
        ("wire_count", c_uint32),
        ("zero_fill_count", c_uint64),
        ("reactivations", c_uint64),
        ("pageins", c_uint64),
        ("pageouts", c_uint64),
        ("faults", c_uint64),
        ("cow_faults", c_uint64),
        ("lookups", c_uint64),
        ("hits", c_uint64),
        ("purges", c_uint64),
        ("purgeable_count", c_uint32),
        ("speculative_count", c_uint32),
        ("decompressions", c_uint64),
        ("compressions", c_uint64),
        ("swapins", c_uint64),
        ("swapouts", c_uint64),
        ("compressor_page_count", c_uint32),
        ("throttled_count", c_uint32),
        ("external_page_count", c_uint32),
        ("internal_page_count", c_uint32),
        ("total_uncompressed_pages_in_compressor", c_uint64),
    ]


class _RusageInfoV2(Structure):
    _fields_ = [
        ("uuid", ctypes.c_uint8 * 16),
        ("user_time", c_uint64),
        ("system_time", c_uint64),
        ("pkg_idle_wkups", c_uint64),
        ("interrupt_wkups", c_uint64),
        ("pageins", c_uint64),
        ("wired_size", c_uint64),
        ("resident_size", c_uint64),
        ("phys_footprint", c_uint64),
        ("proc_start_abstime", c_uint64),
        ("proc_exit_abstime", c_uint64),
        ("child_user_time", c_uint64),
        ("child_system_time", c_uint64),
        ("child_pkg_idle_wkups", c_uint64),
        ("child_interrupt_wkups", c_uint64),
        ("child_pageins", c_uint64),
        ("child_elapsed_abstime", c_uint64),
        ("diskio_bytesread", c_uint64),
        ("diskio_byteswritten", c_uint64),
    ]


class _Timebase(Structure):
    _fields_ = [("numer", c_uint32), ("denom", c_uint32)]


def _fn(lib: ctypes.CDLL, name: str, restype: Any, *argtypes: Any) -> None:
    func = getattr(lib, name)
    func.restype = restype
    func.argtypes = list(argtypes)


@cache
def _libsystem() -> ctypes.CDLL:
    lib = ctypes.CDLL(_LIBSYSTEM, use_errno=True)
    _fn(lib, "sysctlbyname", c_int, c_char_p, c_void_p, POINTER(c_size_t), c_void_p, c_size_t)
    _fn(lib, "mach_host_self", c_uint32)
    _fn(lib, "host_statistics64", c_int, c_uint32, c_int, c_void_p, POINTER(c_uint32))
    _fn(
        lib,
        "host_processor_info",
        c_int,
        c_uint32,
        c_int,
        POINTER(c_uint32),
        POINTER(POINTER(c_uint32)),
        POINTER(c_uint32),
    )
    _fn(lib, "vm_deallocate", c_int, c_uint32, c_size_t, c_size_t)
    _fn(lib, "mach_timebase_info", c_int, POINTER(_Timebase))
    _fn(lib, "notify_register_check", c_uint32, c_char_p, POINTER(c_int))
    _fn(lib, "notify_get_state", c_uint32, c_int, POINTER(c_uint64))
    _fn(lib, "notify_cancel", c_uint32, c_int)
    _fn(lib, "proc_listallpids", c_int, c_void_p, c_int)
    _fn(lib, "proc_pid_rusage", c_int, c_int, c_int, c_void_p)
    return lib


@cache
def _host() -> int:
    return int(_libsystem().mach_host_self())


@cache
def _task_self() -> int:
    return int(c_uint32.in_dll(_libsystem(), "mach_task_self_").value)


@cache
def _abs_to_s() -> float:
    info = _Timebase()
    _libsystem().mach_timebase_info(byref(info))
    return float(info.numer) / float(info.denom) / 1e9


@cache
def _ioreport() -> ctypes.CDLL:
    lib = ctypes.CDLL(_IOREPORT)
    _fn(
        lib,
        "IOReportCopyChannelsInGroup",
        c_void_p,
        c_void_p,
        c_void_p,
        c_uint64,
        c_uint64,
        c_uint64,
    )
    _fn(
        lib,
        "IOReportCreateSubscription",
        c_void_p,
        c_void_p,
        c_void_p,
        POINTER(c_void_p),
        c_uint64,
        c_void_p,
    )
    _fn(lib, "IOReportCreateSamples", c_void_p, c_void_p, c_void_p, c_void_p)
    _fn(lib, "IOReportCreateSamplesDelta", c_void_p, c_void_p, c_void_p, c_void_p)
    _fn(lib, "IOReportChannelGetChannelName", c_void_p, c_void_p)
    _fn(lib, "IOReportChannelGetUnitLabel", c_void_p, c_void_p)
    _fn(lib, "IOReportSimpleGetIntegerValue", ctypes.c_int64, c_void_p, c_int)
    return lib


def _sysctl_raw(name: str) -> bytes | None:
    lib = _libsystem()
    size = c_size_t()
    if lib.sysctlbyname(name.encode(), None, byref(size), None, 0) != 0 or not size.value:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if lib.sysctlbyname(name.encode(), buf, byref(size), None, 0) != 0:
        return None
    return buf.raw[: size.value]


class EnergySampler:
    """Running IOReport "Energy Model" subscription; ``read`` returns joules
    per channel since the previous ``read`` (the first call returns {})."""

    def __init__(self) -> None:
        lib = _ioreport()
        group = _cf.cfstr("Energy Model")
        try:
            self._channels = lib.IOReportCopyChannelsInGroup(group, None, 0, 0, 0)
        finally:
            _cf.release(group)
        if not self._channels:
            raise OSError("IOReport has no 'Energy Model' channels on this Mac")
        # Subscribe with a mutable copy, as the system tools do.
        copy = _cf.cf().CFDictionaryCreateMutableCopy(
            None, _cf.cf().CFDictionaryGetCount(self._channels), self._channels
        )
        if copy:
            _cf.release(self._channels)
            self._channels = copy
        subscribed = c_void_p()
        self._sub = lib.IOReportCreateSubscription(None, self._channels, byref(subscribed), 0, None)
        if not self._sub:
            _cf.release(self._channels)
            raise OSError("IOReport refused the energy subscription")
        self._subscribed = subscribed.value
        self._last: int | None = None

    def read(self) -> dict[str, float]:
        lib = _ioreport()
        current = lib.IOReportCreateSamples(self._sub, self._subscribed, None)
        if not current:
            raise OSError("IOReport returned no sample")
        previous, self._last = self._last, current
        if previous is None:
            return {}
        delta = lib.IOReportCreateSamplesDelta(previous, current, None)
        _cf.release(previous)
        if not delta:
            raise OSError("IOReport returned no delta")
        try:
            items = _cf.dict_get(delta, "IOReportChannels")
            out: dict[str, float] = {}
            for channel in _cf.iter_array(items) if items else ():
                name = _cf.to_python(lib.IOReportChannelGetChannelName(channel))
                unit = _cf.to_python(lib.IOReportChannelGetUnitLabel(channel))
                scale = ENERGY_UNITS.get(str(unit).strip())
                if not isinstance(name, str) or scale is None:
                    continue
                out[name] = (
                    out.get(name, 0.0) + lib.IOReportSimpleGetIntegerValue(channel, 0) * scale
                )
            return out
        finally:
            _cf.release(delta)

    def close(self) -> None:
        for ref in (self._last, self._sub, self._subscribed, self._channels):
            _cf.release(ref)
        self._last = self._sub = self._subscribed = self._channels = None


class NativeReaders:
    """Live readers for this Mac. Keeps the GPU entry and the energy
    subscription open between samples; call ``close`` when done."""

    def __init__(self) -> None:
        self._energy: EnergySampler | None = None
        self._gpu_entry = 0

    def close(self) -> None:
        if self._energy is not None:
            self._energy.close()
            self._energy = None
        _cf.free(self._gpu_entry)
        self._gpu_entry = 0

    def energy_delta(self) -> dict[str, float]:
        """Joules per energy channel since the previous call ({} on the first)."""
        if self._energy is None:
            self._energy = EnergySampler()
        return self._energy.read()

    def gpu_stats(self) -> dict[str, float]:
        """Performance statistics of the first GPU, read without its other properties."""
        if not self._gpu_entry:
            entries = list(_cf.matching("IOAccelerator"))
            if not entries:
                raise OSError("no GPU found in IOKit (IOAccelerator)")
            self._gpu_entry = entries[0]
            for extra in entries[1:]:
                _cf.free(extra)
        stats = _cf.entry_property(self._gpu_entry, "PerformanceStatistics")
        if not isinstance(stats, dict):
            raise OSError("the GPU publishes no performance statistics")
        return {k: v for k, v in stats.items() if isinstance(v, (int, float))}

    def sysctl_str(self, name: str) -> str | None:
        raw = _sysctl_raw(name)
        return None if raw is None else raw.split(b"\x00", 1)[0].decode("utf-8", "replace")

    def sysctl_int(self, name: str) -> int | None:
        raw = _sysctl_raw(name)
        if raw is None or len(raw) not in (4, 8):
            return None
        return int(struct.unpack("<i" if len(raw) == 4 else "<q", raw)[0])

    def swap(self) -> dict[str, int] | None:
        raw = _sysctl_raw("vm.swapusage")
        if raw is None or len(raw) < 24:
            return None
        total, available, used = struct.unpack("<QQQ", raw[:24])
        return {"total": total, "available": available, "used": used}

    def cpu_ticks(self) -> list[list[int]]:
        """[user, system, idle, nice] ticks per logical CPU, counted since boot."""
        lib = _libsystem()
        count = c_uint32()
        info = POINTER(c_uint32)()
        info_count = c_uint32()
        status = lib.host_processor_info(
            _host(), PROCESSOR_CPU_LOAD_INFO, byref(count), byref(info), byref(info_count)
        )
        if status != 0:
            raise OSError(f"host_processor_info failed ({status})")
        try:
            return [[int(info[cpu * 4 + i]) for i in range(4)] for cpu in range(count.value)]
        finally:
            address = ctypes.cast(info, c_void_p).value or 0
            lib.vm_deallocate(_task_self(), address, info_count.value * 4)

    def cpu_layout(self) -> list[list[Any]]:
        """[logical cpu id, cluster type "E" or "P"] from the IOKit device tree."""
        root = _cf.from_path("IODeviceTree:/cpus")
        if not root:
            return []
        layout: list[list[Any]] = []
        try:
            for entry in _cf.children(root, "IODeviceTree"):
                try:
                    props = _cf.properties(entry)
                finally:
                    _cf.free(entry)
                kind = props.get("cluster-type")
                cpu_id = props.get("logical-cpu-id")
                if isinstance(cpu_id, bytes) and cpu_id:
                    cpu_id = int.from_bytes(cpu_id[:4], "little")
                if isinstance(kind, bytes) and isinstance(cpu_id, int):
                    layout.append([cpu_id, kind.rstrip(b"\x00").decode()])
        finally:
            _cf.free(root)
        return sorted(layout)

    def vm_stats(self) -> dict[str, int]:
        stats = _VmStatistics64()
        count = c_uint32(ctypes.sizeof(stats) // 4)
        status = _libsystem().host_statistics64(_host(), HOST_VM_INFO64, byref(stats), byref(count))
        if status != 0:
            raise OSError(f"host_statistics64 failed ({status})")
        return {field[0]: int(getattr(stats, field[0])) for field in _VmStatistics64._fields_}

    def gpu(self) -> list[dict[str, Any]]:
        """One entry per GPU: model, core count and its performance statistics."""
        gpus: list[dict[str, Any]] = []
        for entry in _cf.matching("IOAccelerator"):
            try:
                props = _cf.properties(entry)
            finally:
                _cf.free(entry)
            stats = props.get("PerformanceStatistics")
            gpus.append(
                {
                    "model": props.get("model"),
                    "cores": props.get("gpu-core-count"),
                    "stats": {
                        key: value
                        for key, value in (stats or {}).items()
                        if isinstance(value, (int, float))
                    },
                }
            )
        return gpus

    def gpu_clients(self) -> list[dict[str, Any]]:
        """Apps holding a GPU connection: pid and accumulated GPU time (ns), no names."""
        clients: list[dict[str, Any]] = []
        for accelerator in _cf.matching("IOAccelerator"):
            try:
                for entry in _cf.children(accelerator):
                    try:
                        props = _cf.properties(entry)
                    finally:
                        _cf.free(entry)
                    match = _CLIENT_PID.search(str(props.get("IOUserClientCreator", "")))
                    usage = props.get("AppUsage")
                    gpu_ns = None
                    if isinstance(usage, list):
                        gpu_ns = sum(
                            int(item.get("accumulatedGPUTime", 0))
                            for item in usage
                            if isinstance(item, dict)
                        )
                    clients.append(
                        {"pid": int(match.group(1)) if match else None, "gpu_ns": gpu_ns}
                    )
            finally:
                _cf.free(accelerator)
        return clients

    def energy(self, interval_s: float) -> dict[str, float]:
        """Joules per IOReport energy channel over ``interval_s``."""
        sampler = EnergySampler()
        try:
            sampler.read()
            time.sleep(interval_s)
            return sampler.read()
        finally:
            sampler.close()

    def thermal_level(self) -> int:
        """0 nominal, 1 moderate, 2 heavy, 3 trapping, 4 sleeping."""
        lib = _libsystem()
        token = c_int()
        if lib.notify_register_check(THERMAL_NOTIFY, byref(token)) != 0:
            raise OSError("thermal-pressure notification is not available")
        try:
            state = c_uint64()
            if lib.notify_get_state(token.value, byref(state)) != 0:
                raise OSError("cannot read the thermal-pressure level")
            return int(state.value)
        finally:
            lib.notify_cancel(token.value)

    def processes(self) -> dict[str, Any]:
        """How many processes exist and how many we may read, plus our own CPU time."""
        lib = _libsystem()
        size = lib.proc_listallpids(None, 0)
        if size <= 0:
            raise OSError("cannot list processes")
        pids = (c_int * (size + 64))()
        found = lib.proc_listallpids(pids, ctypes.sizeof(pids))
        readable = 0
        usage = _RusageInfoV2()
        for pid in pids[:found]:
            if pid > 0 and lib.proc_pid_rusage(pid, RUSAGE_INFO_V2, byref(usage)) == 0:
                readable += 1
        own = _RusageInfoV2()
        if lib.proc_pid_rusage(os.getpid(), RUSAGE_INFO_V2, byref(own)) != 0:
            raise OSError("cannot read this process's CPU time")
        own_s = (own.user_time + own.system_time) * _abs_to_s()
        return {"listed": int(found), "readable": readable, "own_cpu_s": own_s}
