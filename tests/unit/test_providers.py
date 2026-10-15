"""AI providers: catalog, seeding, editing, routing and pricing (record 0018)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from ai_meterix.collectors.proxy import Route, Router, provider_of
from ai_meterix.collectors.usage import extract_usage
from ai_meterix.core.config import Config, load_config
from ai_meterix.core.errors import ValidationError
from ai_meterix.core.model import Caller, LlmCall, ModelPrice, PriceSource, Provider, ProviderKind
from ai_meterix.core.settings import SettingsWatcher, seed_settings
from ai_meterix.pricing.engine import PRICE_NOT_SET, CostEngine
from ai_meterix.pricing.prices import set_price
from ai_meterix.pricing.providers import (
    add_provider,
    app_url,
    catalog,
    local_names,
    routes,
    seed_providers,
    set_provider,
)
from ai_meterix.storage import StorageWriter, open_reader, open_writer

UTC = timezone.utc
H10 = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)


@pytest.fixture
def url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'meter.db'}"


@pytest.fixture
def store(url: str) -> StorageWriter:
    s = open_writer(url)
    s.migrate()
    return s


def test_the_catalog_has_the_main_cloud_providers() -> None:
    providers = catalog()
    names = [p.name for p in providers]
    assert len(names) == len(set(names)) == 10
    for name in ("openai", "anthropic", "gemini", "mistral", "groq", "together", "fireworks",
                 "openrouter", "deepseek", "xai"):  # fmt: skip
        assert name in names
    for p in providers:
        assert p.base_url.startswith("https://") and p.kind is ProviderKind.CLOUD and p.built_in
    groq = next(p for p in providers if p.name == "groq")
    assert app_url(groq, "http://127.0.0.1:8080") == "http://127.0.0.1:8080/groq/openai/v1"


def test_the_first_start_adds_the_catalog_and_the_config_routes(store: StorageWriter) -> None:
    config = load_config(env={})
    config = config.model_copy(
        update={
            "proxy": config.proxy.model_copy(
                update={"upstreams": {"llama": "http://127.0.0.1:8081"}}
            )
        }
    )
    store.save_setting("proxy.upstreams", {"work": "https://ai.example.com"}, by="init")  # older
    seed_settings(store, config)
    providers = {p.name: p for p in store.get_providers()}
    assert len(providers) == 12
    assert providers["llama"].kind is ProviderKind.LOCAL and not providers["llama"].built_in
    assert providers["work"].kind is ProviderKind.CLOUD
    assert seed_providers(store, {"llama": "http://127.0.0.1:9999"}) == []  # the database wins
    assert store.get_providers()[0].version == 1


def test_providers_are_added_changed_and_logged(store: StorageWriter) -> None:
    seed_providers(store)
    added = add_provider(store, "azure", {"base_url": "https://myco.openai.azure.com/"}, by="m")
    assert (added.base_url, added.kind, added.label, added.version) == (
        "https://myco.openai.azure.com", ProviderKind.CLOUD, "azure", 1
    )  # fmt: skip
    changed = set_provider(store, "azure", {"app_path": "openai/v1", "label": "Azure"}, by="m")
    assert (changed.app_path, changed.label, changed.version) == ("/openai/v1", "Azure", 2)
    off = set_provider(store, "openrouter", {"enabled": "false"}, by="m")
    assert not off.enabled and off.built_in
    assert "openrouter" not in routes(store.get_providers())
    keys = [c.key for c in store.get_setting_changes() if c.by == "m"]
    assert "provider.azure.app_path" in keys and "provider.openrouter.enabled" in keys
    # unchanged values do not make a new version
    assert set_provider(store, "azure", {"label": "Azure"}, by="m").version == 2
    with pytest.raises(ValidationError, match="exists"):
        add_provider(store, "groq", {"base_url": "https://x.example"}, by="m")
    with pytest.raises(ValidationError, match="no provider"):
        set_provider(store, "nope", {"label": "x"}, by="m")
    with pytest.raises(ValidationError, match="base URL"):
        add_provider(store, "new", {}, by="m")
    with pytest.raises(ValidationError, match="http"):
        add_provider(store, "new", {"base_url": "ftp://x"}, by="m")
    with pytest.raises(ValidationError, match="reserved"):
        Provider("local", "http://127.0.0.1:1")
    with pytest.raises(ValidationError, match="lowercase"):
        Provider("My API", "https://x.example")
    with pytest.raises(ValidationError, match="true or false"):
        set_provider(store, "groq", {"enabled": "maybe"}, by="m")


def test_the_proxy_routes_follow_the_providers(url: str, store: StorageWriter) -> None:
    config = Config()
    seed_settings(store, config)
    router = Router(routes(store.get_providers()))
    route = router.resolve("/groq/openai/v1/chat/completions")
    assert route is not None and route.upstream == "https://api.groq.com"
    assert route.path == "/openai/v1/chat/completions" and provider_of(route) == "groq"
    assert provider_of(Route("default", "http://127.0.0.1:8081", "/v1", None)) == "local"
    assert (
        provider_of(Route("default", "https://generativelanguage.googleapis.com", "/", None))
        == "gemini"
    )

    watcher = SettingsWatcher(lambda: open_reader(url), config, lambda new, changed: None)
    watcher.providers = store.get_providers()
    assert watcher.check() == []
    set_provider(store, "groq", {"enabled": False}, by="m")
    assert watcher.check() == ["providers"]
    assert "groq" not in routes(watcher.providers or [])


def test_cost_is_tokens_times_price_and_zero_without_a_price(store: StorageWriter) -> None:
    seed_providers(store)
    add_provider(store, "llama", {"base_url": "http://127.0.0.1:8081"}, by="m")
    assert local_names(store.get_providers()) == {"llama"}
    store.upsert_callers([Caller("unknown", "unknown")])
    store.write_calls(
        [
            LlmCall(H10, "groq", "openai/gpt-oss-120b", "unknown", input_tokens=1_000_000,
                    output_tokens=500_000, cache_read_tokens=0),
            LlmCall(H10, "mistral", "brand-new-model", "unknown", input_tokens=10, output_tokens=5),
            LlmCall(H10, "llama", "qwen", "unknown", input_tokens=300, output_tokens=200),
        ]
    )  # fmt: skip
    set_price(store, "openai/gpt-oss-120b", {"input_per_m": Decimal("0.15"),
              "output_per_m": Decimal("0.60")}, now=H10 - timedelta(days=1), by="m",
              provider="groq")  # fmt: skip
    store.save_price(ModelPrice("brand-new-model", PriceSource.API, H10, price_set=False), by="p")
    end = H10 + timedelta(hours=1)
    statement = CostEngine(store, UTC).statement(H10, end, now=end)
    lines = {line.model: line for line in statement.by_model()}
    assert lines["openai/gpt-oss-120b"].token_cost == Decimal("0.45")  # 0.15 + 0.5 x 0.60
    assert lines["openai/gpt-oss-120b"].provider == "groq"
    assert (
        lines["brand-new-model"].token_cost == 0 and PRICE_NOT_SET in lines["brand-new-model"].notes
    )
    assert lines["qwen"].source == "local" and PRICE_NOT_SET not in lines["qwen"].notes
    assert store.get_prices("openai/gpt-oss-120b")[0].provider == "groq"


def test_deepseek_cache_hits_are_read() -> None:
    body = {
        "model": "deepseek-flash",
        "usage": {"prompt_tokens": 100, "completion_tokens": 7, "prompt_cache_hit_tokens": 60,
                  "prompt_cache_miss_tokens": 40},
    }  # fmt: skip
    usage = extract_usage(json.dumps(body).encode())
    assert (usage.input_tokens, usage.cache_read_tokens, usage.output_tokens) == (40, 60, 7)


def test_the_providers_command(url: str, capsys: pytest.CaptureFixture[str]) -> None:
    from ai_meterix.interfaces.cli import main

    assert main(["providers", "--url", url]) == 0
    out = capsys.readouterr().out
    assert "groq" in out and "/groq/openai/v1" in out
    assert main(["providers", "add", "llama", "--base-url", "http://127.0.0.1:8081", "--path",
                 "/v1", "--url", url]) == 0  # fmt: skip
    assert "local" in capsys.readouterr().out
    assert main(["providers", "set", "llama", "--label", "llama.cpp", "--url", url]) == 0
    assert main(["providers", "set", "llama", "--url", url]) == 2  # nothing to change
    assert main(["providers", "disable", "openrouter", "--url", url]) == 0
    assert "disabled" in capsys.readouterr().out
    assert main(["providers", "history", "llama", "--url", url]) == 0
    assert "label: llama -> llama.cpp" in capsys.readouterr().out
    assert main(["providers", "show", "nope", "--url", url]) == 2
    assert main(["providers", "add", "--url", url]) == 2
    assert main(["providers", "add", "x", "--url", url]) == 2  # needs --base-url
    assert main(["prices", "set", "m1", "--provider", "nope", "--input", "1", "--url", url]) == 2
    assert main(["prices", "set", "m1", "--provider", "llama", "--input", "1", "--url", url]) == 0
    capsys.readouterr()
    assert main(["prices", "--provider", "groq", "--url", url]) == 0
    assert "m1" not in capsys.readouterr().out
    assert main(["providers", "show", "llama", "--url", url]) == 0
    assert "m1" in capsys.readouterr().out


def test_old_price_rows_keep_working_after_the_upgrade(tmp_path: Path) -> None:
    s = open_writer(f"sqlite:///{tmp_path / 'old.db'}")
    s.migrate(target=2)
    # the v2 table has no provider column: write a row the old way
    s._write(lambda: s._run(  # type: ignore[attr-defined]
        "INSERT INTO model_prices (model, valid_from, source, input_per_m, output_per_m, "
        "cache_read_per_m, cache_write_per_m, valid_to, price_set) "
        "VALUES ('gpt-x', 0, 'api', '1', '2', '0', '0', NULL, 1)"
    ))  # fmt: skip
    assert s.migrate() == 3
    (price,) = s.get_prices("gpt-x")
    assert price.provider == "" and price.input_per_m == 1
    s.close()
