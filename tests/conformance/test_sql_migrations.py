"""Schema upgrades for the built-in SQL connectors (forward-only migrations)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ai_meterix.storage import Dataset, UsageQuery, open_writer
from ai_meterix.storage.sqlstore import LATEST_VERSION, SQLStorage

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_upgrade_from_v1_rolls_up_existing_data(url: str) -> None:
    storage = open_writer(url)
    assert isinstance(storage, SQLStorage)
    try:
        assert storage.migrate(target=1) == 1
        assert storage.schema_version() == 1
        assert not storage.health().ok  # older schema than this version expects
        ts = int(T0.timestamp() * 1000) + 60_000
        storage._write(
            lambda: storage._run(
                "INSERT INTO device_samples (ts, device_id, interval_s, util_pct, power_w, "
                "source, extra) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ts, "mac:gpu", 60.0, 50.0, 10.0, "measured", "{}"),
            )
        )
        assert storage.migrate() == LATEST_VERSION
        assert storage.health().ok
        assert storage.rollup(T0 + timedelta(hours=1)) == 1
        query = UsageQuery(Dataset.DEVICE_HOURLY, T0, T0 + timedelta(days=1))
        (row,) = storage.query(query).dicts()
        assert row["energy_j"] == 600.0
    finally:
        storage.close()


def test_newer_schema_is_refused(url: str) -> None:
    storage = open_writer(url)
    assert isinstance(storage, SQLStorage)
    try:
        storage.migrate()
        storage._write(lambda: storage._set_meta("schema_version", str(LATEST_VERSION + 1)))
        assert not storage.health().ok
        try:
            storage.migrate()
        except Exception as exc:
            assert "newer" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("migrate accepted a newer schema")
    finally:
        storage.close()
