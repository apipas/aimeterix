"""Model prices: periods, defaults, the reference list and the command (record 0012 D3.7)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from ai_meterix.core.model import Caller, CostProfile, Host, LlmCall, PriceSource, Project
from ai_meterix.pricing.engine import PRICE_NOT_SET, CostEngine
from ai_meterix.pricing.prices import (
    apply_reference,
    base_model,
    ensure_price_row,
    load_reference,
    price_at,
    set_price,
)
from ai_meterix.pricing.providers import catalog
from ai_meterix.storage import HostInventory, StorageWriter, open_writer

UTC = timezone.utc
D = Decimal
T0 = datetime(2026, 10, 1, tzinfo=UTC)
NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)

REFERENCE = json.dumps(
    {
        "checked": "2026-10-13",
        "currency": "USD",
        "sources": {"x": "https://example.com"},
        "models": [
            {
                "provider": "anthropic",
                "model": "claude-x",
                "input": "3",
                "output": "15",
                "cache_read": "0.30",
                "cache_write": "3.75",
            },
            {
                "provider": "openai",
                "model": "gpt-x",
                "input": "2",
                "output": "8",
                "cache_read": "0.5",
            },
        ],
    }
)


@pytest.fixture
def store(tmp_path: Path) -> StorageWriter:
    s = open_writer(f"sqlite:///{tmp_path / 'meter.db'}")
    s.migrate()
    return s


def test_snapshot_dates_are_stripped() -> None:
    assert base_model("claude-opus-4-1-20250805") == "claude-opus-4-1"
    assert base_model("o4-mini-2025-04-16") == "o4-mini"
    assert base_model("gpt-6.1-sol") == "gpt-6.1-sol"
    assert base_model("claude-opus-4-1") == "claude-opus-4-1"


def test_the_shipped_reference_list_is_dated_and_complete() -> None:
    ref = load_reference()
    built_in = {p.name for p in catalog()}
    assert ref.checked and ref.currency == "USD" and ref.sources
    assert len({r.model for r in ref.models}) == len(ref.models)
    for r in ref.models:
        assert r.provider in built_in and r.provider in ref.sources  # every price has a source
        assert r.input_per_m > 0 and r.output_per_m >= r.input_per_m
        assert r.cache_read_per_m <= r.input_per_m
    assert ref.find("claude-opus-4-1-20250805") == ref.find("claude-opus-4-1")


def test_a_new_api_model_starts_unset_and_a_local_one_at_zero(store: StorageWriter) -> None:
    assert ensure_price_row(store, "gpt-x", local=False, ts=T0, by="proxy")
    assert ensure_price_row(store, "qwen", local=True, ts=T0, by="proxy")
    assert not ensure_price_row(store, "gpt-x", local=False, ts=T0, by="proxy")
    api, local = store.get_prices("gpt-x")[0], store.get_prices("qwen")[0]
    assert (api.source, api.price_set, api.input_per_m) == (PriceSource.API, False, 0)
    assert (local.source, local.price_set) == (PriceSource.LOCAL, True)


def test_set_without_a_date_corrects_the_price_in_force(store: StorageWriter) -> None:
    ensure_price_row(store, "gpt-x", local=False, ts=T0, by="proxy")
    set_price(store, "gpt-x", {"input_per_m": D("2")}, now=NOW, by="admin")
    (row,) = store.get_prices("gpt-x")
    assert row.input_per_m == 2 and row.price_set and row.valid_from == T0
    changes = store.get_price_changes("gpt-x")
    assert {c.field_name for c in changes if c.by == "admin"} == {"input_per_m", "price_set"}


def test_set_with_a_date_starts_a_new_period(store: StorageWriter) -> None:
    set_price(store, "gpt-x", {"input_per_m": D("2"), "output_per_m": D("8")}, now=T0, by="a")
    mid = T0 + timedelta(days=5)
    set_price(store, "gpt-x", {"input_per_m": D("1")}, now=NOW, by="a", start=mid)
    old, new = sorted(store.get_prices("gpt-x"), key=lambda p: p.valid_from)
    assert old.valid_to == mid and old.input_per_m == 2
    assert new.valid_from == mid and new.valid_to is None
    assert (new.input_per_m, new.output_per_m) == (1, 8)  # output kept
    rows = store.get_prices("gpt-x")
    assert price_at(rows, mid - timedelta(seconds=1)) == old
    assert price_at(rows, NOW) == new
    # a period before the first row ends where the first row starts
    early = T0 - timedelta(days=3)
    set_price(store, "gpt-x", {"input_per_m": D("5")}, now=NOW, by="a", start=early)
    first = min(store.get_prices("gpt-x"), key=lambda p: p.valid_from)
    assert first.valid_from == early and first.valid_to == T0 and first.input_per_m == 5


def test_apply_reference(store: StorageWriter) -> None:
    ref = load_reference(REFERENCE)
    ensure_price_row(store, "claude-x", local=False, ts=T0, by="proxy")
    ensure_price_row(store, "claude-x-20260101", local=False, ts=T0, by="proxy")
    set_price(store, "gpt-x", {"input_per_m": D("9")}, now=T0, by="admin")

    preview = apply_reference(store, ref, now=NOW, by="admin", dry_run=True)
    assert {r.model: r.action for r in preview} == {
        "claude-x": "priced",
        "claude-x-20260101": "priced",
        "gpt-x": "new period",
    }
    assert store.get_prices("claude-x")[0].price_set is False  # nothing saved

    apply_reference(store, ref, now=NOW, by="admin")
    # unset rows take the price for their whole period
    (claude,) = store.get_prices("claude-x")
    assert claude.valid_from == T0 and claude.price_set and claude.output_per_m == 15
    assert store.get_prices("claude-x-20260101")[0].cache_write_per_m == D("3.75")
    # a price the admin set keeps its past and changes from now
    old, new = sorted(store.get_prices("gpt-x"), key=lambda p: p.valid_from)
    assert (old.input_per_m, old.valid_to) == (9, NOW)
    assert (new.input_per_m, new.cache_write_per_m, new.valid_from) == (2, 0, NOW)

    again = apply_reference(store, ref, now=NOW + timedelta(hours=1), by="admin")
    assert {r.action for r in again} == {"unchanged"}


def _calls(store: StorageWriter) -> None:
    store.upsert_callers([Caller("unknown", "unknown")])
    store.write_calls(
        [
            LlmCall(T0 + timedelta(hours=1), "anthropic", "claude-x-20260101", "unknown",
                    input_tokens=1_000_000, output_tokens=0, cache_read_tokens=0),
        ]
    )  # fmt: skip


def test_a_snapshot_without_its_own_row_uses_the_base_price(store: StorageWriter) -> None:
    _calls(store)
    engine = CostEngine(store, tz=UTC)
    end = T0 + timedelta(hours=2)
    (line,) = engine.statement(T0, end, now=end).by_model()
    assert PRICE_NOT_SET in line.notes
    set_price(store, "claude-x", {"input_per_m": D("3")}, now=T0, by="admin")
    (line,) = engine.statement(T0, end, now=end).by_model()
    assert line.token_cost == 3 and PRICE_NOT_SET not in line.notes


def _db(tmp_path: Path, currency: str | None) -> str:
    s = open_writer(f"sqlite:///{tmp_path / 'meter.db'}")
    s.migrate()
    s.upsert_projects([Project("p", "p")])
    s.upsert_hosts([HostInventory(Host("mac", "p", "mac", "darwin", "arm64"))])
    if currency:
        s.save_profile(CostProfile("mac", "home", currency, D("0.30")), by="admin")
    ensure_price_row(s, "claude-opus-5-5-20261001", local=False, ts=T0, by="proxy")
    s.close()
    return f"sqlite:///{tmp_path / 'meter.db'}"


def test_prices_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from ai_meterix.interfaces.cli import main

    url = _db(tmp_path, "USD")
    assert main(["prices", "--url", url]) == 0
    out = capsys.readouterr().out
    assert "claude-opus-5-5-20261001" in out and "not set" in out and "checked" in out
    assert main(["prices", "reference"]) == 0
    assert "claude-opus-5-5" in capsys.readouterr().out
    assert main(["prices", "apply-reference", "--url", url, "--dry-run"]) == 0
    assert "would change" in capsys.readouterr().out
    assert main(["prices", "apply-reference", "--url", url]) == 0
    assert "priced" in capsys.readouterr().out
    assert main(["prices", "set", "qwen", "--local", "--input", "0", "--url", url]) == 0
    assert "qwen" in capsys.readouterr().out
    assert main(["prices", "set", "x", "--input", "abc", "--url", url]) == 2
    assert main(["prices", "set", "--url", url]) == 2
    assert main(["prices", "history", "claude-opus-5-5-20261001", "--url", url]) == 0
    assert "price_set" in capsys.readouterr().out


def test_apply_reference_refuses_another_currency(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from ai_meterix.interfaces.cli import main

    url = _db(tmp_path, "EUR")
    assert main(["prices", "apply-reference", "--url", url]) == 2
    assert "does not convert currencies" in capsys.readouterr().err
    assert main(["prices", "apply-reference", "--url", url, "--ignore-currency"]) == 0
