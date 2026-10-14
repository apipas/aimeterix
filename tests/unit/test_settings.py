"""Live settings in the database: seed, edit, audit, export and reload (record 0013)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ai_meterix.collectors.agent import Agent
from ai_meterix.collectors.mock import MockBackend
from ai_meterix.collectors.proxy import Router
from ai_meterix.core.config import Config, load_config
from ai_meterix.core.settings import (
    LIVE,
    SettingsWatcher,
    effective,
    export_yaml,
    live_values,
    parse_value,
    reset_setting,
    seed_settings,
    set_setting,
)
from ai_meterix.storage import StorageWriter, open_reader, open_writer


@pytest.fixture
def url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'meter.db'}"


@pytest.fixture
def store(url: str) -> StorageWriter:
    s = open_writer(url)
    s.migrate()
    return s


def file_config(**agent: float) -> Config:
    return load_config(env={"AIMETERIX_AGENT_INTERVAL_S": str(agent.get("interval_s", 10))})


def test_the_first_start_copies_the_file_and_then_the_database_wins(store: StorageWriter) -> None:
    assert sorted(seed_settings(store, file_config(interval_s=20))) == sorted(LIVE)
    assert seed_settings(store, file_config(interval_s=30)) == []  # only once
    live, notes = effective(file_config(interval_s=30), store)
    assert live.agent.interval_s == 20
    assert len(notes) == 1 and "agent.interval_s" in notes[0] and "database" in notes[0]
    assert store.get_setting_changes("agent.interval_s")[0].by == "init"


def test_set_validates_like_the_config_file_and_logs_who(store: StorageWriter) -> None:
    config = Config()
    seed_settings(store, config)
    assert set_setting(store, config, "agent.interval_s", 5, by="maher") == 5
    with pytest.raises(ValueError, match="interval_s"):
        set_setting(store, config, "agent.interval_s", 0.5, by="maher")
    with pytest.raises(ValueError, match="unknown setting"):
        set_setting(store, config, "storage.url", "sqlite:///x.db", by="maher")
    with pytest.raises(ValueError, match="http"):
        set_setting(store, config, "proxy.default_upstream", "ftp://x", by="maher")
    with pytest.raises(ValueError, match="SHA-256"):
        set_setting(store, config, "proxy.allowed_key_hashes", ["sk-a-real-key"], by="maher")
    url = set_setting(store, config, "proxy.default_upstream", "http://127.0.0.1:8081/", by="m")
    assert url == "http://127.0.0.1:8081"
    hashes = set_setting(store, config, "proxy.allowed_key_hashes", "a" * 64, by="m")
    assert hashes == ["a" * 64]
    (change,) = [c for c in store.get_setting_changes("agent.interval_s") if c.by == "maher"]
    assert (change.old, change.new) == ("10.0", "5.0")
    assert reset_setting(store, config, "agent.interval_s", by="m") == 10.0
    assert effective(config, store)[0].agent.interval_s == 10.0


def test_parse_value_reads_json_or_text() -> None:
    assert parse_value("agent.interval_s", "5") == 5
    assert parse_value("proxy.default_upstream", "null") is None
    assert parse_value("proxy.default_upstream", "http://x") == "http://x"
    assert parse_value("proxy.upstreams", '{"local": "http://x"}') == {"local": "http://x"}
    with pytest.raises(ValueError, match="unknown setting"):
        parse_value("nope", "1")


def test_export_leaves_out_passwords() -> None:
    config = load_config(env={"AIMETERIX_STORAGE_URL": "postgresql://me:secret@db:5432/m"})
    text = export_yaml(config)
    assert "secret" not in text and "me@db:5432" in text
    data = yaml.safe_load(text)
    assert data["agent"]["interval_s"] == 10.0
    assert Config.model_validate(data) == config.model_copy(
        update={"storage": config.storage.model_copy(update={"url": data["storage"]["url"]})}
    )


def test_the_watcher_applies_changes_and_skips_pinned_ones(url: str, store: StorageWriter) -> None:
    config = Config()
    seed_settings(store, config)
    seen: list[list[str]] = []
    watcher = SettingsWatcher(
        lambda: open_reader(url),
        config,
        lambda new, changed: seen.append(changed),
        pinned=["proxy.default_upstream"],
    )
    assert watcher.check() == []  # first read: nothing differs from the start
    assert watcher.check() == []  # no new version
    set_setting(store, config, "agent.interval_s", 30, by="m")
    set_setting(store, config, "proxy.default_upstream", "http://127.0.0.1:8081", by="m")
    assert watcher.check() == ["agent.interval_s"]
    assert seen == [["agent.interval_s"]] and watcher.config.agent.interval_s == 30
    assert live_values(watcher.config)["agent.interval_s"] == 30


def test_routes_and_interval_change_while_running(tmp_path: Path) -> None:
    router = Router({"openai": "https://api.openai.com"})
    assert router.resolve("/v1/chat/completions") is None
    router.update({}, "http://127.0.0.1:8081/")
    route = router.resolve("/v1/chat/completions")
    assert route is not None and route.upstream == "http://127.0.0.1:8081"
    moved = router.resolve("/openai/v1/x")
    assert moved is not None and moved.name == "default"

    backend = MockBackend("p", interval_s=10)
    agent = Agent(backend, lambda: open_writer("sqlite:///:memory:"), data_dir=tmp_path)
    agent.set_interval(30)
    assert (agent.interval_s, agent.base_interval_s, backend.interval_s) == (30, 30, 30)
    with pytest.raises(ValueError):
        agent.set_interval(0)
    agent.backend.close()


def test_config_command(url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from ai_meterix.interfaces.cli import main

    assert main(["config", "--url", url]) == 0
    out = capsys.readouterr().out
    assert "agent.interval_s" in out and "version 1" in out and "Bootstrap" in out
    assert main(["config", "set", "agent.interval_s", "15", "--url", url]) == 0
    assert "agent.interval_s = 15" in capsys.readouterr().out
    assert main(["config", "set", "agent.interval_s", "abc", "--url", url]) == 2
    assert main(["config", "set", "storage.url", "x", "--url", url]) == 2
    assert main(["config", "set", "agent.interval_s", "--url", url]) == 2
    assert main(["config", "history", "agent.interval_s", "--url", url]) == 0
    assert "10.0 -> 15" in capsys.readouterr().out
    target = tmp_path / "export.yaml"
    assert main(["config", "export", str(target), "--url", url]) == 0
    assert yaml.safe_load(target.read_text())["agent"]["interval_s"] == 15
    assert main(["config", "export", str(target), "--url", url]) == 2  # never overwrites
    assert main(["config", "reset", "agent.interval_s", "--url", url]) == 0
    assert "agent.interval_s = 10.0" in capsys.readouterr().out
