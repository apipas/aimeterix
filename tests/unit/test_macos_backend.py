"""Apple Silicon sampling backend, replayed from readings recorded on an M4 Max."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from ai_meterix.collectors.macos.backend import AppleSiliconBackend, cluster_energy
from ai_meterix.collectors.macos.probe import ReplayReaders
from ai_meterix.core.model import DeviceKind, MemoryPressure, PoolType, Project
from ai_meterix.storage import open_writer

FIXTURE = Path(__file__).parent.parent / "fixtures" / "doctor-m4.json"
T0 = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
INTERVAL = 0.25


def readings() -> dict[str, Any]:
    recorded: dict[str, Any] = json.loads(FIXTURE.read_text())["readings"]
    data = copy.deepcopy(recorded)
    data["energy_delta"] = [{}, recorded["energy"][0]]
    data["gpu_stats"] = [recorded["gpu"][0][0]["stats"]]
    # synthetic apps: pid 7 runs llama-server and is also a recorded GPU client
    clients = recorded["gpu_clients"][0]
    data["gpu_clients"] = [clients, [*clients, {"pid": 7, "gpu_ns": 200_000_000}]]
    data["process_usage"] = [[[7, 10.0, 1], [8, 3.0, 2]], [[7, 10.1, 1], [8, 3.05, 2]]]
    data["process_name"] = {"7": "llama-server", "8": "editor"}
    data["process_args"] = {"7": ["llama-server", "-m", "/models/qwen-a3b.gguf"]}
    return data


def backend(data: dict[str, Any] | None = None) -> AppleSiliconBackend:
    ticks: Iterator[float] = iter(i * INTERVAL for i in range(100))
    stamps: Iterator[datetime] = iter(T0 + timedelta(seconds=i) for i in range(100))
    return AppleSiliconBackend(
        "lab",
        host_id="m4",
        readers=ReplayReaders(data if data is not None else readings()),
        clock=lambda: next(ticks),
        now=lambda: next(stamps),
        fetch=refuse,
    )


def refuse(url: str) -> Any:
    raise OSError("connection refused")


def by_id(batch: Any) -> dict[str, Any]:
    return {s.device_id: s for s in batch.device_samples}


def test_inventory_has_clusters_gpu_npu_and_one_unified_pool() -> None:
    inv = backend().inventory()
    assert inv.host.id == "m4"
    assert inv.host.project_id == "lab"
    assert inv.host.chip == "Apple M4 Max"
    assert inv.host.tags == {"model": "Mac16,6"}
    assert [(d.id, d.kind) for d in inv.devices] == [
        ("m4:cpu-p", DeviceKind.CPU),
        ("m4:cpu-e", DeviceKind.CPU),
        ("m4:gpu", DeviceKind.GPU),
        ("m4:npu", DeviceKind.NPU),
    ]
    assert all(d.memory_pool_id == "m4:unified" for d in inv.devices)
    (pool,) = inv.pools
    assert pool.type is PoolType.UNIFIED
    assert pool.total_bytes == 64 * 2**30


def test_first_sample_only_starts_the_counters() -> None:
    batch = backend().sample()
    assert batch.device_samples == ()
    assert batch.memory_samples == ()


def test_second_sample_turns_counters_into_rates() -> None:
    data = readings()
    b = backend(data)
    b.sample()
    batch = b.sample()
    assert batch.problems == ()
    samples = by_id(batch)
    assert set(samples) == {"m4:cpu-p", "m4:cpu-e", "m4:gpu", "m4:npu"}
    assert all(s.interval_s == INTERVAL for s in samples.values())
    assert all(s.ts == T0 + timedelta(seconds=1) for s in samples.values())

    before, after = data["cpu_ticks"]

    def cluster_util(cpus: range) -> float:
        busy = total = 0
        for cpu in cpus:
            delta = [b - a for a, b in zip(before[cpu], after[cpu], strict=True)]
            busy += sum(delta) - delta[2]
            total += sum(delta)
        return 100.0 * busy / total

    assert samples["m4:cpu-e"].util_pct == pytest.approx(cluster_util(range(0, 4)))
    assert samples["m4:cpu-p"].util_pct == pytest.approx(cluster_util(range(4, 16)))

    energy = data["energy"][0]
    assert samples["m4:cpu-e"].power_w == pytest.approx(energy["EACC_CPU"] / INTERVAL)
    p_joules = energy["PACC0_CPU"] + energy["PACC1_CPU"]
    assert samples["m4:cpu-p"].power_w == pytest.approx(p_joules / INTERVAL)
    assert samples["m4:gpu"].power_w == pytest.approx(energy["GPU Energy"] / INTERVAL)
    assert samples["m4:npu"].power_w == pytest.approx(energy["ANE"] / INTERVAL)
    assert samples["m4:npu"].util_pct is None  # macOS shows no Neural Engine use
    assert samples["m4:gpu"].util_pct == data["gpu_stats"][0]["Device Utilization %"]
    assert samples["m4:cpu-p"].extra == {"thermal": "nominal"}


def test_apps_get_cpu_and_gpu_time_and_a_share_of_the_energy() -> None:
    data = readings()
    b = backend(data)
    b.sample()
    batch = b.sample()
    apps = {p.process_name: p for p in batch.process_samples}
    assert set(apps) == {"llama-server", "editor"}
    llama = apps["llama-server"]
    assert llama.cpu_time_s == pytest.approx(0.1)
    assert llama.gpu_time_s == pytest.approx(0.2)
    assert llama.local_model_id == "m4:llama.cpp:qwen-a3b"
    energy = data["energy"][0]
    gpu_busy = data["gpu_stats"][0]["Device Utilization %"] / 100 * INTERVAL
    gpu_share = energy["GPU Energy"] * 0.2 / max(0.2, gpu_busy)
    assert llama.attributed_energy_j is not None
    assert llama.attributed_energy_j > gpu_share
    assert apps["editor"].attributed_energy_j == pytest.approx(
        (llama.attributed_energy_j - gpu_share) / 2
    )
    assert [m.model_name for m in batch.local_models] == ["qwen-a3b"]


def test_memory_sample_reads_the_unified_pool() -> None:
    data = readings()
    b = backend(data)
    b.sample()
    (memory,) = b.sample().memory_samples
    vm = data["vm_stats"][0]
    page = data["sysctl_int"]["hw.pagesize"]
    used = (
        vm["internal_page_count"]
        - vm["purgeable_count"]
        + vm["wire_count"]
        + vm["compressor_page_count"]
    ) * page
    assert memory.pool_id == "m4:unified"
    assert memory.used_bytes == used
    assert memory.wired_bytes == vm["wire_count"] * page
    assert memory.compressed_bytes == vm["compressor_page_count"] * page
    assert memory.swap_bytes == data["swap"][0]["used"]
    assert memory.gpu_allocated_bytes == data["gpu_stats"][0]["In use system memory"]
    assert memory.pressure is MemoryPressure.NORMAL
    assert memory.extra == {"dram_w": round(data["energy"][0]["DRAM"] / INTERVAL, 4)}


def test_refused_energy_leaves_power_missing_and_is_not_retried() -> None:
    data = readings()
    data["energy_delta"] = [{"error": "IOReport refused the energy subscription"}]
    b = backend(data)
    first = b.sample()
    assert first.problems == (
        "energy: IOReport refused the energy subscription (disabled until restart)",
    )
    second = b.sample()
    assert second.problems == ()
    assert all(s.power_w is None for s in second.device_samples)
    assert by_id(second)["m4:cpu-p"].util_pct is not None


def test_failed_memory_read_gives_missing_values_not_zero() -> None:
    data = readings()
    data["vm_stats"] = [{"error": "host_statistics64 failed (5)"}]
    b = backend(data)
    b.sample()
    batch = b.sample()
    (memory,) = batch.memory_samples
    assert memory.used_bytes is None
    assert memory.wired_bytes is None
    assert "memory: host_statistics64 failed (5)" in batch.problems


def test_unknown_cluster_layout_falls_back_to_one_cpu_device() -> None:
    data = readings()
    data["cpu_layout"] = [[]]
    b = backend(data)
    assert b.inventory().devices[0].id == "m4:cpu"
    b.sample()
    cpu = by_id(b.sample())["m4:cpu"]
    assert cpu.util_pct is not None
    assert cpu.power_w == pytest.approx(data["energy"][0]["CPU Energy"] / INTERVAL)


def test_clock_that_does_not_advance_skips_the_tick() -> None:
    b = AppleSiliconBackend(
        "lab", host_id="m4", readers=ReplayReaders(readings()), clock=lambda: 1.0
    )
    b.sample()
    batch = b.sample()
    assert batch.device_samples == ()
    assert batch.problems == ("clock did not advance; tick skipped",)


def test_samples_store_in_sqlite(tmp_path: Path) -> None:
    b = backend()
    storage = open_writer(f"sqlite:///{tmp_path / 'meter.db'}")
    try:
        storage.migrate()
        storage.upsert_projects([Project("lab", "Lab")])
        storage.upsert_hosts([b.inventory()])
        b.sample()
        batch = b.sample()
        assert storage.write_device_samples(batch.device_samples) == 4
        assert storage.write_memory_samples(batch.memory_samples) == 1
        storage.upsert_local_models(batch.local_models)
        assert storage.write_process_samples(batch.process_samples) == 2
        assert [m.model_name for m in storage.get_local_models("m4")] == ["qwen-a3b"]
    finally:
        storage.close()


def test_cluster_energy_groups_channels() -> None:
    parts = cluster_energy(
        {"EACC_CPU": 1.0, "PACC0_CPU": 2.0, "PACC1_CPU": 3.0, "PACC0_CPU4": 9.0, "GPU": 0.5}
    )
    assert parts == {"P": 5.0, "E": 1.0, "cpu": 6.0, "gpu": 0.5}
    assert cluster_energy({"CPU Energy": 4.0, "GPU Energy": 1.0, "GPU": 0.9})["gpu"] == 1.0
    assert cluster_energy({}) == {}
