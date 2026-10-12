"""What every hardware backend provides to the agent (record 0009 item 2)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ai_meterix.core.model import DeviceSample, LocalModel, MemorySample, ProcessSample
from ai_meterix.storage.base import HostInventory


@dataclass(frozen=True)
class SampleBatch:
    """Readings taken in one tick. ``problems`` names readers that failed this
    tick; their values are missing (None), never zero. ``local_models`` lists
    the models found loaded when the backend last looked (not every tick)."""

    device_samples: tuple[DeviceSample, ...] = ()
    memory_samples: tuple[MemorySample, ...] = ()
    process_samples: tuple[ProcessSample, ...] = ()
    local_models: tuple[LocalModel, ...] = ()
    problems: tuple[str, ...] = ()


class HardwareBackend(Protocol):
    name: str

    def inventory(self) -> HostInventory:
        """The host, its memory pools and its devices."""
        ...

    def sample(self) -> SampleBatch:
        """Readings since the previous call. The first call only starts the
        counters and returns an empty batch."""
        ...

    def close(self) -> None: ...
