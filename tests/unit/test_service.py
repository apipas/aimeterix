"""`aimeterix start` / `stop` / `health`: background processes, PID files, logs (record 0016)."""

from __future__ import annotations

import json
import shutil
import socket
import time
from pathlib import Path

import pytest

from ai_meterix.core.paths import Paths, paths
from ai_meterix.interfaces.cli import main
from ai_meterix.interfaces.service import SERVICES, health, port_in_use, start, status, stop


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Paths:
    """An isolated ~ and database; the children inherit it through the environment."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AIMETERIX_STORAGE_URL", f"sqlite:///{tmp_path / 'meter.db'}")
    monkeypatch.setenv("AIMETERIX_PROXY_PORT", str(free_port()))
    monkeypatch.setenv("AIMETERIX_API_PORT", str(free_port()))
    monkeypatch.chdir(tmp_path)
    return paths()


def test_start_runs_in_the_background_and_stop_leaves_nothing(home: Paths) -> None:
    results = {name: ok for name, ok, _ in start(["proxy", "serve"], where=home)}
    assert results == {"proxy": True, "serve": True}
    for name in ("proxy", "serve"):
        assert status(name, home).running
        assert (home.run_dir / f"{name}.pid").exists()
        assert (home.log_dir / f"{name}.log").exists()
    again = start(["proxy"], where=home)
    assert again[0][1] and "already running" in again[0][2]  # never a second copy
    proxy_port = int(__import__("os").environ["AIMETERIX_PROXY_PORT"])
    deadline = time.monotonic() + 15  # a slow CI machine may take a while to listen
    while not port_in_use("127.0.0.1", proxy_port) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert port_in_use("127.0.0.1", proxy_port)

    stopped = {name: (ok, msg) for name, ok, msg in stop(["proxy", "serve", "agent"], where=home)}
    assert stopped["proxy"] == (True, "stopped") and stopped["serve"] == (True, "stopped")
    assert stopped["agent"] == (True, "not running")
    assert not list(home.run_dir.glob("*.pid"))
    assert not port_in_use("127.0.0.1", proxy_port)
    assert "proxy stopped" in (home.log_dir / "proxy.log").read_text()  # flushed and exited


def test_a_service_that_fails_at_start_is_reported(home: Paths) -> None:
    false = shutil.which("false")  # /bin/false on Linux, /usr/bin/false on macOS
    assert false is not None
    (name, ok, message), = start(["agent"], where=home, python=false)  # fmt: skip
    assert name == "agent" and not ok and "exited at start" in message
    assert not (home.run_dir / "agent.pid").exists()


def test_a_stale_pid_file_is_not_running(home: Paths) -> None:
    home.run_dir.mkdir(parents=True)
    (home.run_dir / "agent.pid").write_text("999999\n")
    assert not status("agent", home).running
    assert stop(["agent"], where=home) == [("agent", True, "not running")]
    assert not (home.run_dir / "agent.pid").exists()


def test_health_names_what_is_missing(home: Paths, capsys: pytest.CaptureFixture[str]) -> None:
    url = home.storage_url
    report = health(url, where=home)
    assert not report["ok"] and [p["name"] for p in report["processes"]] == list(SERVICES)
    assert "agent is not running" in report["problems"]
    assert main(["health"]) == 1
    out = capsys.readouterr().out
    assert "agent  stopped" in out and "NOT healthy" in out
    assert main(["health", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_start_and_stop_commands(home: Paths, capsys: pytest.CaptureFixture[str]) -> None:
    assert not home.config_file.exists()
    assert main(["start", "serve"]) == 0  # no `init` needed: the first start sets up
    out = capsys.readouterr().out
    assert "serve  started" in out and "aimeterix health" in out
    assert "First start" in out and home.config_file.is_file()
    assert main(["start", "serve"]) == 0
    assert "First start" not in capsys.readouterr().out
    assert main(["health"]) == 1  # agent and proxy are not running
    assert "serve  running" in capsys.readouterr().out
    assert main(["stop"]) == 0
    assert "serve  stopped" in capsys.readouterr().out
