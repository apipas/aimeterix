"""`aimeterix start` / `stop` / `health`: background processes, PID files, logs (record 0016)."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from ai_meterix.interfaces.cli import main
from ai_meterix.interfaces.service import SERVICES, health, port_in_use, start, status, stop


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated ~ and database; the children inherit it through the environment."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AIMETERIX_STORAGE_URL", f"sqlite:///{tmp_path / 'meter.db'}")
    monkeypatch.setenv("AIMETERIX_PROXY_PORT", str(free_port()))
    monkeypatch.setenv("AIMETERIX_API_PORT", str(free_port()))
    monkeypatch.chdir(tmp_path)
    return tmp_path / ".aimeterix"


def test_start_runs_in_the_background_and_stop_leaves_nothing(home: Path) -> None:
    results = {name: ok for name, ok, _ in start(["proxy", "serve"], home=home)}
    assert results == {"proxy": True, "serve": True}
    for name in ("proxy", "serve"):
        assert status(name, home).running
        assert (home / "run" / f"{name}.pid").exists()
        assert (home / "logs" / f"{name}.log").exists()
    again = start(["proxy"], home=home)
    assert again[0][1] and "already running" in again[0][2]  # never a second copy
    proxy_port = int(__import__("os").environ["AIMETERIX_PROXY_PORT"])
    assert port_in_use("127.0.0.1", proxy_port)

    stopped = {name: (ok, msg) for name, ok, msg in stop(["proxy", "serve", "agent"], home=home)}
    assert stopped["proxy"] == (True, "stopped") and stopped["serve"] == (True, "stopped")
    assert stopped["agent"] == (True, "not running")
    assert not list((home / "run").glob("*.pid"))
    assert not port_in_use("127.0.0.1", proxy_port)
    assert "proxy stopped" in (home / "logs" / "proxy.log").read_text()  # flushed and exited


def test_a_service_that_fails_at_start_is_reported(home: Path) -> None:
    (name, ok, message), = start(["agent"], home=home, python="/bin/false")  # fmt: skip
    assert name == "agent" and not ok and "exited at start" in message
    assert not (home / "run" / "agent.pid").exists()


def test_a_stale_pid_file_is_not_running(home: Path) -> None:
    (home / "run").mkdir(parents=True)
    (home / "run" / "agent.pid").write_text("999999\n")
    assert not status("agent", home).running
    assert stop(["agent"], home=home) == [("agent", True, "not running")]
    assert not (home / "run" / "agent.pid").exists()


def test_health_names_what_is_missing(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    url = f"sqlite:///{home.parent / 'meter.db'}"
    report = health(url, home=home)
    assert not report["ok"] and [p["name"] for p in report["processes"]] == list(SERVICES)
    assert "agent is not running" in report["problems"]
    assert main(["health"]) == 1
    out = capsys.readouterr().out
    assert "agent  stopped" in out and "NOT healthy" in out
    assert main(["health", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_start_and_stop_commands(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["start", "serve"]) == 0
    out = capsys.readouterr().out
    assert "serve  started" in out and "aimeterix health" in out
    assert main(["health"]) == 1  # agent and proxy are not running
    assert "serve  running" in capsys.readouterr().out
    assert main(["stop"]) == 0
    assert "serve  stopped" in capsys.readouterr().out
