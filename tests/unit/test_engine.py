"""The cost engine against a hand-checked hour (record 0012).

The numbers in the comments are worked out by hand; the test checks the engine
gets them to the last decimal.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ai_meterix.core.model import (
    Caller,
    CallType,
    CostProfile,
    Device,
    DeviceKind,
    DeviceSample,
    FeeSpread,
    Host,
    LlmCall,
    LocalModel,
    ModelPrice,
    PriceSource,
    ProcessSample,
    Project,
)
from ai_meterix.pricing.engine import (
    IDLE,
    OTHER_APPS,
    PRICE_NOT_SET,
    CostEngine,
    _profile_at,
    month_hours,
)
from ai_meterix.storage import HostInventory, StorageWriter, open_writer

UTC = timezone.utc
H10 = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)
D = Decimal


def build(
    tmp_path: Path, fees: str = "74.40", spread: FeeSpread = FeeSpread.CALENDAR
) -> StorageWriter:
    s = open_writer(f"sqlite:///{tmp_path / 'meter.db'}")
    s.migrate()
    s.upsert_projects([Project("p", "p")])
    host = Host("mac", "p", "mac", "darwin", "arm64")
    s.upsert_hosts(
        [
            HostInventory(
                host,
                devices=(
                    Device("mac:gpu", "mac", DeviceKind.GPU),
                    Device("mac:cpu-p", "mac", DeviceKind.CPU),
                ),
            )
        ]
    )
    # 10:00-11:00: GPU 50 W and CPU 10 W for the whole hour = 216,000 J = 0.06 kWh
    mid = H10 + timedelta(minutes=30)
    s.write_device_samples(
        [
            DeviceSample(mid, "mac:gpu", 1800, util_pct=60, power_w=50),
            DeviceSample(H10 + timedelta(minutes=59), "mac:gpu", 1800, util_pct=60, power_w=50),
            DeviceSample(mid, "mac:cpu-p", 1800, util_pct=20, power_w=10),
            DeviceSample(H10 + timedelta(minutes=59), "mac:cpu-p", 1800, util_pct=20, power_w=10),
        ]
    )
    s.upsert_local_models(
        [
            LocalModel(
                "mac:llama-server:qwen", "mac", "llama.cpp", "qwen", H10, H10 + timedelta(hours=1)
            )
        ]
    )
    # the model used half the energy (108,000 J), other apps a fifth (43,200 J)
    s.write_process_samples(
        [
            ProcessSample(
                mid,
                "mac",
                "llama-server",
                1,
                3600,
                attributed_energy_j=108_000,
                local_model_id="mac:llama-server:qwen",
            ),
            ProcessSample(mid, "mac", "Safari", 2, 3600, attributed_energy_j=43_200),
        ]
    )
    s.upsert_callers([Caller("unknown", "unknown")])
    s.write_calls(
        [
            LlmCall(
                H10 + timedelta(minutes=15),
                "anthropic",
                "claude-x",
                "unknown",
                input_tokens=1000,
                output_tokens=500,
                cache_read_tokens=2000,
                cache_write_tokens=0,
            ),
            LlmCall(
                H10 + timedelta(minutes=16),
                "openai",
                "gpt-x",
                "unknown",
                input_tokens=10,
                output_tokens=5,
                cache_read_tokens=0,
            ),
            LlmCall(
                H10 + timedelta(minutes=20),
                "local",
                "qwen",
                "unknown",
                input_tokens=300,
                output_tokens=180_000,
                cache_read_tokens=100,
            ),
            LlmCall(
                H10 + timedelta(minutes=21),
                "local",
                "qwen",
                "unknown",
                input_tokens=100,
                output_tokens=0,
                cache_read_tokens=0,
            ),
            LlmCall(
                H10 + timedelta(minutes=22), "openai", "batch", "unknown", call_type=CallType.BATCH
            ),
        ]
    )
    s.save_profile(
        CostProfile(
            "mac", "home", "USD", D("0.30"), fixed_fees_per_month=D(fees), fee_spread=spread
        ),
        by="admin",
    )
    s.save_price(
        ModelPrice(
            "claude-x",
            PriceSource.API,
            H10 - timedelta(days=30),
            D("3"),
            D("15"),
            D("0.30"),
            D("3.75"),
        ),
        by="admin",
    )
    return s


def lines(storage: StorageWriter, hours: int = 2) -> dict[str, dict]:
    engine = CostEngine(storage, tz=UTC)
    st = engine.statement(H10, H10 + timedelta(hours=hours), now=H10 + timedelta(hours=hours))
    assert st.currency == "USD"
    return {line.model: line.as_dict() for line in st.by_model()}


def test_one_hour_by_hand(tmp_path: Path) -> None:
    s = build(tmp_path)
    try:
        got = lines(s)
    finally:
        s.close()
    # October has 744 h: fees 74.40 / 744 = 0.10 per hour; energy 0.06 kWh x 0.30 = 0.018
    qwen = got["qwen"]  # half of the 10:00 hour
    assert qwen["kwh"] == D("0.03")
    assert qwen["energy_cost"] == D("0.009")
    assert qwen["fee_cost"] == D("0.05")
    assert (qwen["requests"], qwen["input_tokens"], qwen["output_tokens"]) == (2, 400, 180_000)
    assert qwen["cache_read_tokens"] == 100 and qwen["cache_write_tokens"] is None
    assert qwen["token_cost"] == 0
    assert qwen["source"] == "local"
    assert qwen["cost"] == D("0.059")

    other = got[OTHER_APPS]  # a fifth
    assert (other["kwh"], other["energy_cost"], other["fee_cost"]) == (
        D("0.012"),
        D("0.0036"),
        D("0.02"),
    )

    idle = got[IDLE]  # the rest of 10:00 (0.3) plus all of 11:00, when nothing was measured
    assert idle["kwh"] == D("0.018")
    assert idle["energy_cost"] == D("0.0054")
    assert idle["fee_cost"] == D("0.03") + D("0.10")

    claude = got["claude-x"]  # 1000 x 3 + 500 x 15 + 2000 x 0.30 + 0 x 3.75, per 1M
    assert claude["token_cost"] == D("0.0111")
    assert claude["source"] == "api" and claude["provider"] == "anthropic"
    assert claude["kwh"] == 0

    gpt = got["gpt-x"]
    assert gpt["token_cost"] == 0
    assert PRICE_NOT_SET in gpt["notes"]
    assert "batch tokens not metered yet" in got["batch"]["notes"]


def test_the_total_adds_up(tmp_path: Path) -> None:
    s = build(tmp_path)
    try:
        st = CostEngine(s, tz=UTC).statement(
            H10, H10 + timedelta(hours=2), now=H10 + timedelta(hours=2)
        )
    finally:
        s.close()
    total = st.total()
    assert total.kwh == D("0.06")
    assert total.energy_cost == D("0.018")
    assert total.fee_cost == D("0.20")
    assert total.token_cost == D("0.0111")
    assert total.cost == D("0.2291")


def test_editing_a_price_reprices_past_days(tmp_path: Path) -> None:
    s = build(tmp_path)
    try:
        s.save_price(
            ModelPrice("gpt-x", PriceSource.API, H10 - timedelta(days=1), D("1000"), D("2000")),
            by="admin",
        )
        got = lines(s)
    finally:
        s.close()
    assert got["gpt-x"]["token_cost"] == D("0.02")  # 10 x 1000 + 5 x 2000, per 1M
    assert PRICE_NOT_SET not in got["gpt-x"]["notes"]


def test_uptime_spread_puts_the_fee_on_hours_the_machine_was_on(tmp_path: Path) -> None:
    s = build(tmp_path, spread=FeeSpread.UPTIME)
    try:
        got = lines(s)
    finally:
        s.close()
    # the agent saw one hour this month, so that hour carries the whole 74.40
    assert got["qwen"]["fee_cost"] == D("37.20")
    assert got[IDLE]["fee_cost"] == D("74.40") * D("0.3")


def test_days_are_local_days(tmp_path: Path) -> None:
    s = build(tmp_path)
    try:
        # 10:00 UTC is 19:00 in Tokyo; 2 hours later it is the next day there
        engine = CostEngine(s, tz=ZoneInfo("Asia/Tokyo"))
        st = engine.statement(H10, H10 + timedelta(hours=6), now=H10 + timedelta(hours=6))
    finally:
        s.close()
    days = {line.day.isoformat() for line in st.lines}
    assert days == {"2026-10-05", "2026-10-06"}


def test_without_a_profile_energy_is_shown_but_not_priced(tmp_path: Path) -> None:
    s = open_writer(f"sqlite:///{tmp_path / 'meter.db'}")
    s.migrate()
    s.upsert_projects([Project("p", "p")])
    s.upsert_hosts(
        [
            HostInventory(
                Host("mac", "p", "mac", "darwin", "arm64"),
                devices=(Device("mac:gpu", "mac", DeviceKind.GPU),),
            )
        ]
    )
    s.write_device_samples([DeviceSample(H10 + timedelta(minutes=30), "mac:gpu", 3600, power_w=10)])
    try:
        st = CostEngine(s, tz=UTC).statement(
            H10, H10 + timedelta(hours=1), now=H10 + timedelta(hours=1)
        )
    finally:
        s.close()
    (line,) = st.lines
    assert line.kwh == D("0.01") and line.cost == 0
    assert "no cost profile" in line.notes
    assert st.currency is None


@pytest.mark.parametrize(
    ("month", "zone", "hours"),
    [
        (datetime(2026, 2, 10, tzinfo=UTC), "Europe/Prague", 672),
        (datetime(2026, 3, 10, tzinfo=UTC), "Europe/Prague", 743),  # clocks go forward
        (datetime(2026, 10, 10, tzinfo=UTC), "Europe/Prague", 745),  # and back
        (datetime(2026, 10, 10, tzinfo=UTC), "UTC", 744),
    ],
)
def test_month_hours_follow_the_real_calendar(month: datetime, zone: str, hours: int) -> None:
    assert month_hours(month, ZoneInfo(zone)) == hours


def test_profile_versions() -> None:
    def version(n: int, saved: datetime) -> CostProfile:
        return CostProfile("h", "x", "EUR", D(n), version=n, saved_at=saved)

    history = [version(1, H10), version(2, H10 + timedelta(days=1))]
    assert _profile_at(history, H10 - timedelta(days=5)).version == 1  # type: ignore[union-attr]
    assert _profile_at(history, H10 + timedelta(hours=3)).version == 1  # type: ignore[union-attr]
    assert _profile_at(history, H10 + timedelta(days=2)).version == 2  # type: ignore[union-attr]
    assert _profile_at([], H10) is None


def test_usage_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from ai_meterix.interfaces.cli import main

    build(tmp_path).close()
    url = f"sqlite:///{tmp_path / 'meter.db'}"
    assert main(["usage", "--url", url, "--from", "2026-10-05", "--by", "model"]) == 0
    table = capsys.readouterr().out
    assert "qwen" in table and "claude-x" in table and "Cost USD" in table
    assert main(["usage", "--url", url, "--from", "2026-10-05", "--csv", "-"]) == 0
    rows = capsys.readouterr().out.splitlines()
    assert rows[0].startswith("day,model,source,provider,requests,input_tokens")
    assert any(r.startswith("2026-10-05,claude-x,api,anthropic,1,1000,500,2000,0") for r in rows)


def test_usage_command_without_a_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from ai_meterix.interfaces.cli import main

    assert main(["usage", "--url", f"sqlite:///{tmp_path / 'none.db'}"]) == 1
    assert "database not found" in capsys.readouterr().err


def test_profile_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from ai_meterix.interfaces.cli import main

    build(tmp_path).close()
    url = f"sqlite:///{tmp_path / 'meter.db'}"
    assert main(["profile", "--url", url]) == 0
    assert "0.30 USD/kWh" in capsys.readouterr().out
    assert main(["profile", "set", "--url", url, "--rate", "0.25", "--fees", "60"]) == 0
    out = capsys.readouterr().out
    assert "version 2" in out and "0.25 USD/kWh" in out and "60 USD/month" in out
    assert main(["profile", "history", "--url", url]) == 0
    assert "v1" in capsys.readouterr().out
    assert main(["profile", "set", "--url", url, "--rate", "abc"]) == 2


def test_a_profile_in_another_currency_moves_to_usd(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from ai_meterix.interfaces.cli import main

    s = build(tmp_path)
    s.save_profile(CostProfile("mac", "home", "EUR", D("0.30"), fixed_fees_per_month=D(10)), "a")
    s.close()
    url = f"sqlite:///{tmp_path / 'meter.db'}"
    assert main(["profile", "set", "--url", url, "--pue", "1.1"]) == 2
    assert "give --rate and --fees in USD" in capsys.readouterr().err
    assert main(["profile", "set", "--url", url, "--rate", "0.33", "--fees", "11"]) == 0
    assert "0.33 USD/kWh" in capsys.readouterr().out


def test_usage_shows_watt_hours(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from ai_meterix.interfaces.cli import main

    build(tmp_path).close()
    url = f"sqlite:///{tmp_path / 'meter.db'}"
    assert main(["usage", "--url", url, "--from", "2026-10-05", "--by", "model"]) == 0
    table = capsys.readouterr().out
    assert " Wh " in table and "30.0" in table  # qwen: 0.03 kWh
    assert main(["usage", "--url", url, "--from", "2026-10-05", "--by", "model", "--csv", "-"]) == 0
    rows = capsys.readouterr().out.splitlines()
    head = rows[0].split(",")
    qwen = next(r.split(",") for r in rows if r.startswith("2026-10-05,qwen,"))
    assert qwen[head.index("wh")] == "30.000" and qwen[head.index("kwh")] == "0.030"
