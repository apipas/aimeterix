"""A mock hardware backend for demos and tests on any machine.

It invents a small Mac-like host: CPU, GPU and Neural Engine, one unified
memory pool, and a local model server that is busy now and then. Every row is
marked ``Source.MOCK`` so it can never be mistaken for a measurement. The same
seed gives the same readings.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from datetime import datetime

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
    Source,
)
from ai_meterix.core.timeutil import utc_now
from ai_meterix.storage.base import HostInventory

from .base import SampleBatch

GB = 2**30
MODEL_NAME = "demo-model:8b"


class MockBackend:
    name = "mock"

    def __init__(
        self,
        project_id: str,
        *,
        host_id: str = "demo-mac",
        interval_s: float = 10.0,
        seed: int = 0,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.project_id = project_id
        self.host_id = host_id
        self.interval_s = interval_s
        self._random = random.Random(seed)
        self._now = now
        self._tick = 0
        self._model: LocalModel | None = None

    def _id(self, part: str) -> str:
        return f"{self.host_id}:{part}"

    def inventory(self) -> HostInventory:
        pool = self._id("unified")
        host = Host(
            self.host_id,
            self.project_id,
            self.host_id,
            "macos",
            "arm64",
            "Demo chip",
            {"mock": "yes"},
        )
        devices = (
            Device(self._id("cpu"), self.host_id, DeviceKind.CPU, "Demo CPU", pool),
            Device(self._id("gpu"), self.host_id, DeviceKind.GPU, "Demo GPU", pool),
            Device(self._id("npu"), self.host_id, DeviceKind.NPU, "Demo Neural Engine", pool),
        )
        return HostInventory(
            host, (MemoryPool(pool, self.host_id, PoolType.UNIFIED, 64 * GB),), devices
        )

    def sample(self) -> SampleBatch:
        self._tick += 1
        ts = self._now()
        if self._tick == 1:
            self._model = LocalModel(
                self._id(f"llama.cpp:{MODEL_NAME}"),
                self.host_id,
                "llama.cpp",
                MODEL_NAME,
                ts,
                ts,
                size_bytes=5 * GB,
                pid=4242,
            )
            return SampleBatch(local_models=(self._model,))
        rnd = self._random
        dt = self.interval_s
        # the model server works in bursts: busy about one tick in three
        busy = math.sin(self._tick / 2.0) > 0.4
        gpu_util = rnd.uniform(70, 95) if busy else rnd.uniform(0, 5)
        cpu_util = rnd.uniform(8, 20) if busy else rnd.uniform(2, 8)
        cpu_w = 1.0 + cpu_util * 0.25
        gpu_w = 0.2 + gpu_util * 0.35
        devices = (
            DeviceSample(ts, self._id("cpu"), dt, cpu_util, cpu_w, Source.MOCK),
            DeviceSample(ts, self._id("gpu"), dt, gpu_util, gpu_w, Source.MOCK),
            DeviceSample(ts, self._id("npu"), dt, None, 0.0, Source.MOCK),
        )
        used = int((30 + (8 if busy else 0) + rnd.uniform(-1, 1)) * GB)
        memory = MemorySample(
            ts,
            self._id("unified"),
            dt,
            used_bytes=used,
            wired_bytes=5 * GB,
            compressed_bytes=2 * GB,
            swap_bytes=0,
            gpu_allocated_bytes=int((6 if busy else 1) * GB),
            pressure=MemoryPressure.NORMAL,
            source=Source.MOCK,
        )
        gpu_s = gpu_util / 100 * dt
        model_id = self._model.id if self._model else None
        processes = (
            ProcessSample(
                ts,
                self.host_id,
                "llama-server",
                4242,
                dt,
                cpu_time_s=cpu_util / 200 * dt,
                gpu_time_s=gpu_s * 0.95,
                attributed_energy_j=(gpu_w * 0.95 + cpu_w * 0.3) * dt,
                local_model_id=model_id,
                source=Source.MOCK,
            ),
            ProcessSample(
                ts,
                self.host_id,
                "Browser",
                1000,
                dt,
                cpu_time_s=0.2 * dt,
                gpu_time_s=gpu_s * 0.05,
                attributed_energy_j=(gpu_w * 0.05 + cpu_w * 0.2) * dt,
                source=Source.MOCK,
            ),
        )
        models: tuple[LocalModel, ...] = ()
        if self._model is not None and self._tick % 6 == 0:
            self._model = LocalModel(
                self._model.id,
                self.host_id,
                self._model.runtime,
                self._model.model_name,
                self._model.first_seen,
                ts,
                self._model.size_bytes,
                self._model.pid,
            )
            models = (self._model,)
        return SampleBatch(devices, (memory,), processes, models)

    def close(self) -> None:
        pass
