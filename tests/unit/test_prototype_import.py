import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ai_meterix.collectors.prototype_import import MIB, import_prototype
from ai_meterix.core.errors import AiMeterixError
from ai_meterix.core.model import DeviceKind, PoolType
from ai_meterix.storage import Dataset, StorageWriter, UsageQuery, open_writer

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)

# Schema copied from the prototype (computemeter/db.py).
PROTOTYPE_SCHEMA = """
CREATE TABLE hw_samples (
    ts REAL NOT NULL, host TEXT NOT NULL, device TEXT NOT NULL, model TEXT, util_pct REAL,
    mem_used_mb REAL, mem_total_mb REAL, power_w REAL, interval_s REAL NOT NULL,
    mocked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE llm_calls (
    ts REAL NOT NULL, provider TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER,
    latency_ms REAL, status INTEGER, project TEXT
);
"""


@pytest.fixture
def prototype_db(tmp_path: Path) -> Path:
    path = tmp_path / "computemeter.db"
    conn = sqlite3.connect(path)
    conn.executescript(PROTOTYPE_SCHEMA)
    t = T0.timestamp()
    conn.executemany(
        "INSERT INTO hw_samples VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (t + 60, "lab", "cpu", "Xeon", 20.0, 1024.0, 65536.0, None, 60.0, 0),
            (t + 120, "lab", "cpu", "Xeon", 40.0, 2048.0, 65536.0, None, 60.0, 0),
            (t + 60, "lab", "gpu:0", "NVIDIA H100", 90.0, 40000.0, 81559.0, 500.0, 60.0, 1),
            (t + 60, "lab", "gpu:0", "NVIDIA H100", 150.0, 1.0, 81559.0, 1.0, 60.0, 1),  # bad
            (t + 60, "lab", "tpu", None, 1.0, None, None, None, 60.0, 0),  # unknown device
        ],
    )
    conn.executemany(
        "INSERT INTO llm_calls VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (t + 30, "openai", "gpt-4o", 100, 20, 850.0, 200, "chatbot"),
            (t + 40, "anthropic", "claude", 50, 5, None, 500, None),
        ],
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def writer(tmp_path: Path) -> Iterator[StorageWriter]:
    storage = open_writer(f"sqlite:///{tmp_path / 'meter.db'}")
    storage.migrate()
    yield storage
    storage.close()


def test_import_maps_prototype_rows(prototype_db: Path, writer: StorageWriter) -> None:
    report = import_prototype(prototype_db, writer)
    assert (report.hosts, report.device_samples, report.memory_samples, report.calls) == (
        1,
        3,
        3,
        2,
    )
    assert sum(report.skipped.values()) == 2

    (inventory,) = writer.get_hosts()
    assert inventory.host.id == "lab"
    assert inventory.host.os == "unknown"
    pools = {p.id: p for p in inventory.pools}
    assert pools["lab:system"].type is PoolType.SYSTEM
    assert pools["lab:vram0"].total_bytes == round(81559.0 * MIB)
    kinds = {d.id: d.kind for d in inventory.devices}
    assert kinds == {"lab:cpu": DeviceKind.CPU, "lab:gpu0": DeviceKind.GPU}

    window = (T0, T0 + timedelta(days=1))
    rows = writer.query(UsageQuery(Dataset.DEVICE, *window, group_by=("device_id", "source")))
    by_device = {r["device_id"]: r for r in rows.dicts()}
    assert by_device["lab:gpu0"]["source"] == "mock"
    assert by_device["lab:gpu0"]["energy_j"] == pytest.approx(500 * 60)
    assert by_device["lab:cpu"]["energy_j"] is None
    (mem,) = writer.query(
        UsageQuery(Dataset.MEMORY, *window, filters={"pool_id": ["lab:system"]})
    ).dicts()
    assert mem["used_max"] == 2048 * MIB

    calls = writer.query(UsageQuery(Dataset.CALLS, *window, group_by=("app", "feature")))
    assert [(r["app"], r["feature"], r["requests"]) for r in calls.dicts()] == [
        ("prototype", None, 1),
        ("prototype", "chatbot", 1),
    ]
    (gpt,) = writer.query(UsageQuery(Dataset.CALLS, *window, filters={"model": ["gpt-4o"]})).dicts()
    assert gpt["latency_mean"] == pytest.approx(0.85)
    assert gpt["cache_read_tokens"] is None

    assert writer.rollup(T0 + timedelta(hours=1)) > 0
    hourly = writer.query(UsageQuery(Dataset.CALLS_HOURLY, *window)).dicts()
    assert hourly[0]["input_tokens"] == 150


def test_second_import_is_refused_unless_forced(prototype_db: Path, writer: StorageWriter) -> None:
    import_prototype(prototype_db, writer)
    with pytest.raises(AiMeterixError, match="already imported"):
        import_prototype(prototype_db, writer)
    report = import_prototype(prototype_db, writer, force=True)
    assert report.calls == 2


def test_not_a_prototype_database(tmp_path: Path, writer: StorageWriter) -> None:
    other = tmp_path / "other.db"
    sqlite3.connect(other).execute("CREATE TABLE x (a)").connection.close()
    with pytest.raises(AiMeterixError, match="not a computemeter database"):
        import_prototype(other, writer)
    with pytest.raises(AiMeterixError, match="not found"):
        import_prototype(tmp_path / "missing.db", writer)
