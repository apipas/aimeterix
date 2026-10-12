from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ai_meterix.core.errors import PluginError, ValidationError
from ai_meterix.core.model import Device, DeviceKind, Host, MemoryPool, PoolType
from ai_meterix.storage import (
    Dataset,
    HostInventory,
    RetentionPolicy,
    UsageQuery,
    open_reader,
    open_writer,
)
from ai_meterix.storage.sql import build_query
from ai_meterix.storage.sqlite import parse_url

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_query_validates_range_bucket_and_columns() -> None:
    with pytest.raises(ValidationError, match="end must be after start"):
        UsageQuery(Dataset.DEVICE, T0, T0)
    with pytest.raises(ValidationError, match="whole number of seconds"):
        UsageQuery(Dataset.DEVICE, T0, T0 + timedelta(1), bucket=timedelta(milliseconds=1500))
    with pytest.raises(ValidationError, match="multiple of 1 hour"):
        UsageQuery(Dataset.DEVICE_HOURLY, T0, T0 + timedelta(1), bucket=timedelta(minutes=30))
    with pytest.raises(ValidationError, match="cannot group or filter calls by 'kind'"):
        UsageQuery(Dataset.CALLS, T0, T0 + timedelta(1), group_by=("kind",))
    with pytest.raises(ValidationError, match="unknown dataset"):
        UsageQuery("cpu", T0, T0 + timedelta(1))  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="timezone-aware"):
        UsageQuery(Dataset.DEVICE, datetime(2026, 1, 1), T0)


def test_query_columns_and_parameters() -> None:
    query = UsageQuery(
        "calls",  # type: ignore[arg-type]
        T0,
        T0 + timedelta(days=1),
        bucket=timedelta(hours=1),
        group_by=("model",),
        filters={"provider": ["openai", "anthropic"]},
    )
    assert query.dataset is Dataset.CALLS
    assert query.columns[:3] == ("bucket", "model", "requests")
    sql, params = build_query(query, placeholder="%s")
    assert "f.provider IN (%s, %s)" in sql
    assert params == [1_790_812_800_000, 1_790_899_200_000, "openai", "anthropic"]


def test_retention_policy_limits() -> None:
    with pytest.raises(ValidationError):
        RetentionPolicy(raw_days=0)
    with pytest.raises(ValidationError, match="at least as long"):
        RetentionPolicy(raw_days=30, rollup_days=7)
    with pytest.raises(ValidationError):
        RetentionPolicy(active_util_pct=120)


def test_inventory_rejects_foreign_or_unknown_parts() -> None:
    host = Host("h", "p", "box", "linux", "x86_64")
    with pytest.raises(ValidationError, match="belongs to other"):
        HostInventory(host, (MemoryPool("m", "other", PoolType.SYSTEM),))
    with pytest.raises(ValidationError, match="unknown memory pool"):
        HostInventory(host, (), (Device("d", "h", DeviceKind.GPU, memory_pool_id="vram0"),))


@pytest.mark.parametrize(
    ("url", "path"),
    [
        ("sqlite:///meter.db", "meter.db"),
        ("sqlite:////var/lib/meter.db", "/var/lib/meter.db"),
        ("sqlite:///:memory:", ":memory:"),
    ],
)
def test_parse_url(url: str, path: str) -> None:
    assert parse_url(url) == path


def test_parse_url_expands_home() -> None:
    assert not parse_url("sqlite:///~/.aimeterix/meter.db").startswith("~")


@pytest.mark.parametrize("url", ["sqlite://host/meter.db", "sqlite://", "postgresql:///x"])
def test_parse_url_rejects(url: str) -> None:
    with pytest.raises(ValidationError):
        parse_url(url)


def test_factory_errors() -> None:
    with pytest.raises(PluginError, match="no plugin 'nosuchdb'"):
        open_writer("nosuchdb:///x")
    with pytest.raises(Exception, match="database not found"):
        open_reader("sqlite:////nonexistent/dir/meter.db")


def test_writer_creates_parent_directory(tmp_path: Path) -> None:
    path = f"{tmp_path}/nested/dir/meter.db"
    with open_writer(f"sqlite:///{path}") as storage:  # type: ignore[attr-defined]
        assert storage.migrate() >= 1
