"""The agent loop: batched writes, the buffer when the database is down, the footprint."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from ai_meterix.collectors.agent import (
    BURST_INTERVAL_S,
    Agent,
    Pending,
    SelfMeter,
    from_record,
    to_record,
)
from ai_meterix.collectors.mock import MODEL_NAME, MockBackend
from ai_meterix.core.model import Source
from ai_meterix.core.paths import paths
from ai_meterix.interfaces.cli import duration, main
from ai_meterix.storage import Dataset, StorageWriter, UsageQuery, open_writer

T0 = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def mock(seed: int = 0) -> MockBackend:
    stamps: Iterator[datetime] = iter(T0 + timedelta(seconds=10 * i) for i in range(10_000))
    return MockBackend("lab", seed=seed, now=lambda: next(stamps))


def quiet_meter(clock: Clock) -> SelfMeter:
    return SelfMeter(clock, cpu=lambda: 0.0, rss=lambda: 50 * 2**20)


def make_agent(
    tmp_path: Path, open_storage: Any, clock: Clock | None = None, **kwargs: Any
) -> Agent:
    clock = clock or Clock()
    return Agent(
        mock(),
        open_storage,
        data_dir=tmp_path / "data",
        clock=clock,
        meter=quiet_meter(clock),
        **kwargs,
    )


def sqlite(tmp_path: Path) -> Any:
    url = f"sqlite:///{tmp_path / 'meter.db'}"
    return lambda: open_writer(url)


def count(storage: StorageWriter, dataset: Dataset) -> int:
    table = storage.query(UsageQuery(dataset, T0 - timedelta(days=1), T0 + timedelta(days=1)))
    return sum(int(row["samples"]) for row in table.dicts()) if table.rows else 0


def test_ticks_are_written_in_one_flush(tmp_path: Path) -> None:
    agent = make_agent(tmp_path, sqlite(tmp_path))
    for _ in range(4):
        agent.tick()  # the first only starts the counters and finds the model
    assert agent.flush()
    assert agent.written == 3 * (3 + 1 + 2)  # devices, memory pool, apps per tick
    assert len(agent.pending) == 0
    storage = sqlite(tmp_path)()
    try:
        (inventory,) = storage.get_hosts()
        assert inventory.host.id == "demo-mac"
        assert [p.id for p in storage.get_projects()] == ["lab"]
        (model,) = storage.get_local_models()
        assert model.model_name == MODEL_NAME
    finally:
        storage.close()
    status = json.loads((tmp_path / "data" / "agent-status.json").read_text())
    assert status["rows_written"] == 18
    assert status["rows_pending"] == 0
    assert status["database_error"] is None
    assert status["backend"] == "mock"


def test_rows_survive_a_database_outage_and_a_restart(tmp_path: Path) -> None:
    def down() -> StorageWriter:
        raise OSError("database is locked")

    agent = make_agent(tmp_path, down)
    for _ in range(3):
        agent.tick()
    assert not agent.flush()
    assert agent.last_error == "OSError: database is locked"
    buffer = tmp_path / "data" / "agent-buffer.jsonl"
    assert len(buffer.read_text().splitlines()) == len(agent.pending) == 1 + 2 * 6

    # the agent restarts while the database is still down, then it comes back
    again = make_agent(tmp_path, sqlite(tmp_path))
    assert len(again.pending) == 13
    assert again.flush()
    assert again.written == 12
    assert not buffer.exists()
    storage = sqlite(tmp_path)()
    try:
        assert len(storage.get_local_models()) == 1
    finally:
        storage.close()


class FlakyStorage:
    """Wraps a writer; process samples fail while ``broken`` is set."""

    broken = True

    def __init__(self, inner: StorageWriter) -> None:
        self.inner = inner

    def write_process_samples(self, samples: Any) -> int:
        if FlakyStorage.broken:
            raise OSError("disk full")
        return self.inner.write_process_samples(samples)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def test_a_failed_write_never_writes_a_row_twice(tmp_path: Path) -> None:
    connect = sqlite(tmp_path)
    agent = make_agent(tmp_path, lambda: FlakyStorage(connect()))
    for _ in range(3):
        agent.tick()
    FlakyStorage.broken = True
    assert not agent.flush()
    assert agent.pending.device == [] and agent.pending.memory == []
    assert len(agent.pending.process) == 4
    FlakyStorage.broken = False
    assert agent.flush()
    storage = connect()
    try:
        assert count(storage, Dataset.DEVICE) == 6
        assert count(storage, Dataset.PROCESS) == 4
    finally:
        storage.close()


def test_a_full_buffer_drops_the_oldest_samples(tmp_path: Path) -> None:
    agent = make_agent(tmp_path, sqlite(tmp_path), max_buffer_rows=10)
    for _ in range(4):
        agent.tick()
    assert len(agent.pending) == 10
    assert agent.dropped == 1 + 18 - 10
    assert agent.pending.device[-1].ts == T0 + timedelta(seconds=30)  # newest kept


def test_buffer_records_round_trip() -> None:
    backend = mock()
    pending = Pending()
    pending.add(backend.sample())
    pending.add(backend.sample())
    records = [json.loads(json.dumps(r)) for r in pending.records()]
    again = Pending()
    again.load(records)
    assert again == pending
    kind, row = from_record(to_record("device", pending.device[0]))
    assert kind == "device" and row.source is Source.MOCK


def test_over_budget_cpu_doubles_the_interval_and_recovers(tmp_path: Path) -> None:
    clock = Clock()
    cpu = [0.0]
    meter = SelfMeter(clock, cpu=lambda: cpu[0], rss=lambda: 0)
    agent = Agent(
        mock(), sqlite(tmp_path), data_dir=tmp_path / "d", clock=clock, meter=meter, interval_s=10
    )
    clock.t, cpu[0] = 60.0, 1.2  # 2% of a core over the last minute
    agent.flush()
    assert agent.interval_s == 20
    status = json.loads(agent.status_path.read_text())
    assert status["over_budget"] == ["agent CPU 2.00% is over 1.0%"]
    clock.t = 600.0  # then nearly idle
    agent.flush()
    assert agent.interval_s == 10
    clock.t = 1200.0
    agent.flush()
    assert agent.interval_s == 10  # never below the configured interval


def test_burst_mode_samples_every_second_for_a_while(tmp_path: Path) -> None:
    clock = Clock()
    agent = make_agent(tmp_path, sqlite(tmp_path), clock=clock, burst_s=60)
    assert agent.current_interval() == BURST_INTERVAL_S
    clock.t = 61.0
    assert agent.current_interval() == 10.0


def test_run_ticks_on_schedule_and_flushes_when_done(tmp_path: Path) -> None:
    clock = Clock()
    agent = make_agent(tmp_path, sqlite(tmp_path), clock=clock, flush_s=30)
    flushes: list[float] = []
    real_flush = agent.flush

    def flush() -> bool:
        flushes.append(clock.t)
        return real_flush()

    def wait(seconds: float | None) -> bool:
        clock.t += seconds or 0
        return False

    agent.flush = flush  # type: ignore[method-assign]
    agent._stop.wait = wait  # type: ignore[method-assign]
    agent.run(duration_s=65)
    assert agent.backend._tick == 7  # type: ignore[attr-defined]  # t = 0, 10, ..., 60
    assert flushes == [30.0, 60.0, 65.0]
    assert agent.written == 6 * 6


def test_interval_outside_the_allowed_range_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        make_agent(tmp_path, sqlite(tmp_path), interval_s=0.5)


def test_durations() -> None:
    assert duration("90") == 90
    assert duration("30s") == 30
    assert duration("10m") == 600
    assert duration("1.5h") == 5400
    with pytest.raises(Exception, match="not a duration"):
        duration("soon")


def test_agent_command_runs_the_mock_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AIMETERIX_STORAGE_URL", f"sqlite:///{tmp_path / 'meter.db'}")
    monkeypatch.chdir(tmp_path)
    assert main(["agent", "--mock", "--for", "0", "--interval", "5"]) == 0
    assert "agent stopped" in capsys.readouterr().out
    assert (paths().data_dir / "agent-status.json").exists()
