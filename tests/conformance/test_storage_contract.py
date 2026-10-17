"""Shared storage contract (records 0010, 0012, 0013, 0014)."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ai_meterix.core.model import (
    Caller,
    CallType,
    CostProfile,
    Device,
    DeviceKind,
    DeviceSample,
    Host,
    LlmCall,
    LocalModel,
    MemoryPool,
    MemorySample,
    ModelPrice,
    PoolType,
    PriceSource,
    ProcessSample,
    Project,
    Role,
    User,
)
from ai_meterix.storage import (
    Dataset,
    HostInventory,
    RetentionPolicy,
    StorageReader,
    StorageWriter,
    UsageQuery,
    open_reader,
    open_writer,
)

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


def mac_inventory(chip: str = "Apple M4 Max") -> HostInventory:
    host = Host("mac", "p1", "studio.local", "macos", "arm64", chip, {"site": "home"})
    pool = MemoryPool("mac:mem", "mac", PoolType.UNIFIED, 64 * 2**30)
    devices = tuple(
        Device(f"mac:{kind.value}", "mac", kind, "M4 Max", pool.id) for kind in DeviceKind
    )
    return HostInventory(host, (pool,), devices)


def seed(writer: StorageWriter) -> None:
    writer.upsert_projects([Project("p1", "Home lab")])
    writer.upsert_hosts([mac_inventory()])
    writer.upsert_callers(
        [Caller("c-chat", "chat", "support", {"team": "a"}), Caller("unknown", "unknown")]
    )


def q(dataset: Dataset, start: datetime = T0, end: datetime = T0 + DAY, **kw: object) -> UsageQuery:
    return UsageQuery(dataset, start, end, **kw)  # type: ignore[arg-type]


# --- schema and health -------------------------------------------------------


def test_migrate_from_empty_is_idempotent(url: str) -> None:
    with open_writer(url) as storage:  # type: ignore[attr-defined]
        assert storage.schema_version() is None
        assert not storage.health().ok
        version = storage.migrate()
        assert version >= 1
        assert storage.schema_version() == version
        seed(storage)
        assert storage.migrate() == version  # second run changes nothing
        assert storage.health().ok
        assert [p.name for p in storage.get_projects()] == ["Home lab"]


def test_reader_sees_committed_writes_and_cannot_write(
    writer: StorageWriter, reader: StorageReader
) -> None:
    assert reader.health().ok
    seed(writer)
    writer.write_device_samples([DeviceSample(T0, "mac:gpu", 10, util_pct=50.0)])
    assert reader.query(q(Dataset.DEVICE)).dicts()[0]["samples"] == 1
    with pytest.raises(Exception):  # noqa: B017 - connector-specific error type
        reader.write_device_samples(  # type: ignore[attr-defined]
            [DeviceSample(T0, "mac:gpu", 10, util_pct=50.0)]
        )
    assert reader.query(q(Dataset.DEVICE)).dicts()[0]["samples"] == 1


# --- inventory ---------------------------------------------------------------


def test_host_inventory_round_trip_and_update(writer: StorageWriter) -> None:
    seed(writer)
    assert writer.get_hosts() == [mac_inventory()]
    writer.upsert_hosts([mac_inventory(chip="Apple M5")])
    (inventory,) = writer.get_hosts()
    assert inventory.host.chip == "Apple M5"
    assert {d.memory_pool_id for d in inventory.devices} == {"mac:mem"}
    assert inventory.pools[0].type is PoolType.UNIFIED


def test_callers_and_local_models(writer: StorageWriter) -> None:
    seed(writer)
    assert [c.id for c in writer.get_callers()] == ["c-chat", "unknown"]
    assert writer.get_callers()[0].tags == {"team": "a"}
    model = LocalModel("m1", "mac", "ollama", "llama3:8b", T0, T0 + HOUR, 4_700_000_000, 42)
    writer.upsert_local_models([model])
    later = LocalModel("m1", "mac", "ollama", "llama3:8b", T0 + HOUR, T0 + 2 * HOUR, None, 43)
    writer.upsert_local_models([later])
    (stored,) = writer.get_local_models("mac")
    assert stored.first_seen == T0
    assert stored.last_seen == T0 + 2 * HOUR
    assert stored.size_bytes == 4_700_000_000  # unknown size does not erase a known one
    assert stored.pid == 43
    assert writer.get_local_models("other") == []


# --- queries -----------------------------------------------------------------


def test_device_query_weights_by_interval_and_keeps_missing_as_none(
    writer: StorageWriter,
) -> None:
    seed(writer)
    assert writer.write_device_samples([]) == 0
    written = writer.write_device_samples(
        [
            DeviceSample(T0 + timedelta(seconds=10), "mac:gpu", 10, util_pct=100.0, power_w=20.0),
            DeviceSample(T0 + timedelta(seconds=40), "mac:gpu", 30, util_pct=0.0, power_w=4.0),
            DeviceSample(T0 + timedelta(seconds=50), "mac:gpu", 10),  # nothing measured
            DeviceSample(T0 + timedelta(seconds=50), "mac:npu", 10),  # never measured
        ]
    )
    assert written == 4
    rows = writer.query(q(Dataset.DEVICE, group_by=("device_id",))).dicts()
    gpu, npu = rows
    assert gpu["device_id"] == "mac:gpu"
    assert gpu["samples"] == 3
    assert gpu["seconds"] == pytest.approx(50)
    assert gpu["energy_j"] == pytest.approx(10 * 20 + 30 * 4)
    assert gpu["busy_s"] == pytest.approx(10)
    assert gpu["util_mean"] == pytest.approx(25.0)  # 10 s at 100 % over 40 measured seconds
    assert gpu["util_max"] == pytest.approx(100.0)
    assert gpu["power_mean"] == pytest.approx(8.0)
    assert npu["energy_j"] is None
    assert npu["util_mean"] is None
    assert npu["power_max"] is None


def test_buckets_group_by_filters_and_order(writer: StorageWriter) -> None:
    seed(writer)
    samples = [
        DeviceSample(T0 + h * HOUR + timedelta(minutes=m), dev, 60, util_pct=float(m))
        for h in range(3)
        for m in (10, 20)
        for dev in ("mac:cpu", "mac:gpu")
    ]
    writer.write_device_samples(samples)
    table = writer.query(
        q(
            Dataset.DEVICE,
            end=T0 + 2 * HOUR,
            bucket=HOUR,
            group_by=("kind",),
            filters={"host_id": ["mac"]},
        )
    )
    assert table.columns[:2] == ("bucket", "kind")
    assert [(r["bucket"], r["kind"], r["samples"]) for r in table.dicts()] == [
        (T0, "cpu", 2),
        (T0, "gpu", 2),
        (T0 + HOUR, "cpu", 2),
        (T0 + HOUR, "gpu", 2),
    ]
    only_gpu = writer.query(q(Dataset.DEVICE, filters={"kind": ["gpu"]})).dicts()
    assert only_gpu[0]["samples"] == 6
    assert writer.query(q(Dataset.DEVICE, filters={"kind": []})).rows == ()
    assert writer.query(q(Dataset.DEVICE, start=T0 + DAY, end=T0 + 2 * DAY)).rows == ()


def test_day_buckets_fall_on_utc_midnight(writer: StorageWriter) -> None:
    seed(writer)
    writer.write_device_samples(
        [
            DeviceSample(T0 + DAY - timedelta(seconds=1), "mac:cpu", 10, util_pct=1.0),
            DeviceSample(T0 + DAY, "mac:cpu", 10, util_pct=1.0),
        ]
    )
    rows = writer.query(q(Dataset.DEVICE, end=T0 + 2 * DAY, bucket=DAY)).dicts()
    assert [(r["bucket"], r["samples"]) for r in rows] == [(T0, 1), (T0 + DAY, 1)]


def test_calls_tokens_sum_per_model_and_missing_counts_stay_none(writer: StorageWriter) -> None:
    seed(writer)
    writer.write_calls(
        [
            LlmCall(
                T0 + timedelta(minutes=1),
                "anthropic",
                "claude-x",
                "c-chat",
                request_id="r1",
                input_tokens=100,
                output_tokens=10,
                cache_read_tokens=50,
                cache_write_tokens=0,
                latency_s=1.0,
                status=200,
                raw_usage={"input_tokens": 100},
            ),
            LlmCall(
                T0 + timedelta(minutes=2),
                "anthropic",
                "claude-x",
                "c-chat",
                input_tokens=200,
                output_tokens=20,
                cache_read_tokens=0,
                latency_s=3.0,
                status=529,
            ),
            LlmCall(
                T0 + timedelta(minutes=3),
                "ollama",
                "llama3:8b",
                "unknown",
                input_tokens=5,
                output_tokens=7,
            ),
            LlmCall(T0, "openai", "batch-job", "unknown", call_type=CallType.BATCH),
        ]
    )
    rows = {r["model"]: r for r in writer.query(q(Dataset.CALLS, group_by=("model",))).dicts()}
    claude = rows["claude-x"]
    assert claude["requests"] == 2
    assert claude["input_tokens"] == 300
    assert claude["output_tokens"] == 30
    assert claude["cache_read_tokens"] == 50
    assert claude["cache_write_tokens"] == 0  # reported as zero once
    assert claude["latency_mean"] == pytest.approx(2.0)
    assert claude["errors"] == 1
    llama = rows["llama3:8b"]
    assert llama["cache_read_tokens"] is None  # runtime reports no cache counts: "—", not 0
    assert rows["batch-job"]["input_tokens"] is None
    by_feature = writer.query(q(Dataset.CALLS, group_by=("app", "feature"))).dicts()
    assert [(r["app"], r["feature"], r["requests"]) for r in by_feature] == [
        ("chat", "support", 2),
        ("unknown", None, 2),
    ]
    batch = writer.query(q(Dataset.CALLS, filters={"call_type": ["batch"]})).dicts()
    assert batch[0]["requests"] == 1


def test_memory_and_process_queries(writer: StorageWriter) -> None:
    seed(writer)
    writer.write_memory_samples(
        [
            MemorySample(T0 + timedelta(seconds=10), "mac:mem", 10, used_bytes=1000),
            MemorySample(T0 + timedelta(seconds=40), "mac:mem", 30, used_bytes=3000),
        ]
    )
    (mem,) = writer.query(q(Dataset.MEMORY, group_by=("pool_type",))).dicts()
    assert mem["pool_type"] == "unified"
    assert mem["used_mean"] == pytest.approx(2500)
    assert mem["used_max"] == 3000
    writer.write_process_samples(
        [
            ProcessSample(
                T0 + timedelta(seconds=10), "mac", "ollama", 42, 10, 5.0, 2.0, 30.0, "m1"
            ),
            ProcessSample(
                T0 + timedelta(seconds=20), "mac", "ollama", 42, 10, 4.0, None, 20.0, "m1"
            ),
            ProcessSample(T0 + timedelta(seconds=20), "mac", "Safari", 7, 10, 1.0),
        ]
    )
    rows = writer.query(q(Dataset.PROCESS, group_by=("process_name",))).dicts()
    assert [(r["process_name"], r["cpu_time_s"], r["energy_j"]) for r in rows] == [
        ("Safari", pytest.approx(1.0), None),
        ("ollama", pytest.approx(9.0), pytest.approx(50.0)),
    ]


# --- rollups and retention ---------------------------------------------------


def write_two_hours(writer: StorageWriter) -> None:
    seed(writer)
    writer.write_device_samples(
        [
            DeviceSample(T0 + h * HOUR + timedelta(minutes=m), "mac:gpu", 60, util, power)
            for h in range(2)
            for m, util, power in ((1, 5.0, 2.0), (2, 50.0, 10.0), (3, 95.0, 30.0))
        ]
    )
    writer.write_memory_samples(
        [MemorySample(T0 + timedelta(minutes=m), "mac:mem", 60, used_bytes=m * 100) for m in (1, 2)]
    )
    writer.write_process_samples(
        [ProcessSample(T0 + timedelta(minutes=1), "mac", "ollama", 42, 60, 30.0, 10.0, 90.0)]
    )
    writer.write_calls(
        [LlmCall(T0 + timedelta(minutes=1), "ollama", "llama3:8b", "unknown", input_tokens=9)]
    )


def test_hourly_rollups_match_raw_hourly_buckets(writer: StorageWriter) -> None:
    write_two_hours(writer)
    assert writer.rollup(T0 + HOUR + timedelta(minutes=30)) > 0  # only the first hour is complete
    hourly = writer.query(q(Dataset.DEVICE_HOURLY, bucket=HOUR)).dicts()
    assert [r["bucket"] for r in hourly] == [T0]
    raw = writer.query(q(Dataset.DEVICE, end=T0 + HOUR, bucket=HOUR)).dicts()[0]
    row = hourly[0]
    for name in ("samples", "seconds", "energy_j", "busy_s", "util_mean", "power_mean"):
        assert row[name] == pytest.approx(raw[name]), name
    assert row["active_s"] == pytest.approx(120)  # two samples at or above 10 %
    assert row["util_min"] == pytest.approx(5.0)
    assert row["util_p95"] == pytest.approx(95.0)
    assert row["power_max"] == pytest.approx(30.0)

    assert writer.rollup(T0 + 2 * HOUR) > 0
    assert len(writer.query(q(Dataset.DEVICE_HOURLY, bucket=HOUR)).rows) == 2
    before = writer.query(q(Dataset.DEVICE_HOURLY)).rows
    writer.rollup(T0 + 2 * HOUR)  # running again changes nothing
    assert writer.query(q(Dataset.DEVICE_HOURLY)).rows == before

    (mem,) = writer.query(q(Dataset.MEMORY_HOURLY)).dicts()
    assert mem["used_mean"] == pytest.approx(150)
    assert mem["used_min"] == 100
    (proc,) = writer.query(q(Dataset.PROCESS_HOURLY, group_by=("process_name",))).dicts()
    assert proc["energy_j"] == pytest.approx(90.0)
    (calls,) = writer.query(q(Dataset.CALLS_HOURLY, group_by=("model",))).dicts()
    assert calls["input_tokens"] == 9
    assert calls["output_tokens"] is None
    assert calls["latency_mean"] is None


def test_rollup_picks_up_late_samples_however_late(writer: StorageWriter) -> None:
    write_two_hours(writer)
    writer.rollup(T0 + 2 * HOUR)
    assert writer.rollup(T0 + 2 * HOUR) == 0  # nothing changed, nothing recomputed
    # a sample for the first hour arrives 20 hours later (e.g. a buffered remote agent)
    writer.write_device_samples([DeviceSample(T0 + timedelta(minutes=30), "mac:gpu", 60)])
    writer.rollup(T0 + 20 * HOUR)
    rows = writer.query(q(Dataset.DEVICE_HOURLY, bucket=HOUR)).dicts()
    assert [r["samples"] for r in rows] == [4, 3]


def test_late_sample_for_purged_hour_does_not_shrink_its_rollup(writer: StorageWriter) -> None:
    write_two_hours(writer)
    writer.retention(RetentionPolicy(raw_days=1, rollup_days=30), now=T0 + 2 * DAY)
    before = writer.query(q(Dataset.DEVICE_HOURLY, bucket=HOUR)).dicts()
    writer.write_device_samples([DeviceSample(T0 + timedelta(minutes=30), "mac:gpu", 60)])
    writer.rollup(T0 + 2 * DAY)
    assert writer.query(q(Dataset.DEVICE_HOURLY, bucket=HOUR)).dicts() == before


def test_retention_keeps_rollups_after_raw_data_expires(writer: StorageWriter) -> None:
    write_two_hours(writer)
    policy = RetentionPolicy(raw_days=1, rollup_days=2)
    deleted = writer.retention(policy, now=T0 + DAY + 3 * HOUR)
    assert deleted > 0
    assert writer.query(q(Dataset.DEVICE)).rows == ()
    assert writer.query(q(Dataset.CALLS)).rows == ()
    hourly = writer.query(q(Dataset.DEVICE_HOURLY)).dicts()
    assert hourly[0]["samples"] == 6
    assert hourly[0]["energy_j"] == pytest.approx(2 * 60 * (2 + 10 + 30))
    writer.retention(policy, now=T0 + 3 * DAY + 3 * HOUR)
    assert writer.query(q(Dataset.DEVICE_HOURLY)).rows == ()


# --- cost configuration, settings and accounts --------------------------------


def test_profiles_are_versioned_and_logged(writer: StorageWriter) -> None:
    seed(writer)
    first = writer.save_profile(
        CostProfile("mac", "home", "eur", Decimal("0.31"), region="CZ"), by="admin"
    )
    assert first.version == 1
    assert first.saved_at is not None
    second = writer.save_profile(
        CostProfile(
            "mac",
            "home",
            "EUR",
            Decimal("0.2875"),
            region="CZ",
            fixed_fees_per_month=Decimal("12.50"),
        ),
        by="admin",
    )
    assert second.version == 2
    assert second.energy_rate_per_kwh == Decimal("0.2875")  # exact, not a float
    assert writer.get_profile("mac") == second
    assert writer.get_profile("mac", at=first.saved_at) in (first, second)
    assert writer.get_profile("mac", at=T0) is None
    assert [p.version for p in writer.get_profile_history("mac")] == [1, 2]
    changes = [
        (c.key, c.old, c.new)
        for c in writer.get_setting_changes()
        if c.key.startswith("profile.mac.") and c.old is not None
    ]
    assert changes == [
        ("profile.mac.energy_rate_per_kwh", "0.31", "0.2875"),
        ("profile.mac.fixed_fees_per_month", "0", "12.50"),
    ]


def test_prices_are_editable_with_change_log(writer: StorageWriter) -> None:
    price = ModelPrice("claude-x", PriceSource.API, T0, price_set=False)
    writer.save_price(price, by="admin")
    edited = ModelPrice(
        "claude-x",
        PriceSource.API,
        T0,
        input_per_m=Decimal("3"),
        output_per_m=Decimal("15"),
        cache_read_per_m=Decimal("0.30"),
        cache_write_per_m=Decimal("3.75"),
    )
    writer.save_price(edited, by="admin")
    assert writer.get_prices("claude-x") == [edited]
    log = [(c.field_name, c.old, c.new) for c in writer.get_price_changes("claude-x") if c.old]
    assert ("input_per_m", "0", "3") in log
    assert ("price_set", "False", "True") in log
    assert all(c.by == "admin" for c in writer.get_price_changes())
    later = ModelPrice("claude-x", PriceSource.API, T0 + DAY, input_per_m=Decimal("2.5"))
    writer.save_price(later, by="admin")
    assert [p.valid_from for p in writer.get_prices("claude-x")] == [T0, T0 + DAY]
    assert len(writer.get_prices()) == 2


def test_settings_versions_and_audit(writer: StorageWriter) -> None:
    first = writer.save_setting("agent.interval_s", 10, by="init")
    assert first.version == 1
    assert writer.save_setting("agent.interval_s", 10, by="admin").version == 1  # no change
    second = writer.save_setting("agent.interval_s", 5, by="admin")
    assert second.version == 2
    writer.save_setting("budget", {"monthly": "100.00", "currency": "EUR"}, by="admin")
    settings = writer.get_settings()
    assert settings["agent.interval_s"].value == 5
    assert settings["budget"].value == {"monthly": "100.00", "currency": "EUR"}
    log = writer.get_setting_changes("agent.interval_s")
    assert [(c.by, c.old, c.new) for c in log] == [("init", None, "10"), ("admin", "10", "5")]


def test_users(writer: StorageWriter) -> None:
    writer.save_user(User("admin", Role.ADMIN, "scrypt$a$b"))
    writer.save_user(User("admin", Role.ADMIN, "scrypt$c$d", must_change=False))
    user = writer.get_user("admin")
    assert user == User("admin", Role.ADMIN, "scrypt$c$d", must_change=False)
    assert writer.get_user("nobody") is None


# --- concurrency -------------------------------------------------------------


def test_two_writers_and_a_reader_share_one_database(url: str, writer: StorageWriter) -> None:
    seed(writer)
    errors: list[BaseException] = []

    def write(device_id: str) -> None:
        try:
            with open_writer(url) as own:  # type: ignore[attr-defined]
                for batch in range(20):
                    own.write_device_samples(
                        DeviceSample(
                            T0 + timedelta(seconds=batch * 50 + i), device_id, 1, util_pct=1.0
                        )
                        for i in range(50)
                    )
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    def read() -> None:
        try:
            with open_reader(url) as own:  # type: ignore[attr-defined]
                for _ in range(20):
                    own.query(q(Dataset.DEVICE))
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [
        threading.Thread(target=write, args=("mac:cpu",)),
        threading.Thread(target=write, args=("mac:gpu",)),
        threading.Thread(target=read),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    (row,) = writer.query(q(Dataset.DEVICE)).dicts()
    assert row["samples"] == 2000
