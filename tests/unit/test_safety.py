"""Record 0017 guarantees, tested (PLAN.md Phase 2 step 1e).

- No code path uses sudo or starts other programs.
- Nothing is written outside ``~/.aimeterix/``.
- Servers listen on 127.0.0.1 only; the only connections go to this machine
  or, for the proxy, to the provider the user chose.

The run tests start ``aimeterix agent --mock`` and ``aimeterix proxy`` in a
fresh Python with a fake home folder and an audit hook (PEP 578) that records
every file write, socket and program start, then check the record.
"""

from __future__ import annotations

import ast
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from ai_meterix.collectors.proxy import Recorder, Router, serve
from ai_meterix.collectors.runtimes import local_fetch
from ai_meterix.core.config import load_config
from ai_meterix.storage import open_writer

SRC = Path(__file__).parents[2] / "src" / "ai_meterix"

# --- the code itself ------------------------------------------------------------------

_BANNED_MODULES = {"subprocess", "pty", "multiprocessing"}
_BANNED_OS = ("system", "popen", "exec", "spawn", "posix_spawn", "fork", "kill", "setpriority")


def _sources() -> Iterator[tuple[Path, ast.Module]]:
    for path in sorted(SRC.rglob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _docstrings(tree: ast.Module) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def test_no_code_uses_sudo_or_starts_programs() -> None:
    found = []
    for path, tree in _sources():
        where = path.relative_to(SRC)
        docs = _docstrings(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import | ast.ImportFrom):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else []
                names.append(getattr(node, "module", None) or "")
                for name in names:
                    if name.split(".")[0] in _BANNED_MODULES:
                        found.append(f"{where}:{node.lineno} imports {name}")
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id == "os" and node.attr.startswith(_BANNED_OS):
                    found.append(f"{where}:{node.lineno} uses os.{node.attr}")
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docs
                and "sudo" in node.value.lower()
            ):
                found.append(f"{where}:{node.lineno} mentions sudo")
    assert found == []


# --- a sandboxed run -------------------------------------------------------------------

_HOOK = r"""
import json, os, sys

out = open(sys.argv[1], "w")
events = []
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
PATH_EVENTS = {"os.mkdir", "os.remove", "os.rmdir", "os.truncate", "os.chmod", "os.chown",
               "os.link", "os.symlink", "os.utime", "shutil.rmtree", "shutil.copyfile"}
EXEC_EVENTS = {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn",
               "os.fork", "os.kill", "pty.spawn"}


def path(value):
    return os.path.abspath(os.fsdecode(value))


def hook(event, args):
    if event == "open":
        name, mode, flags = args
        if isinstance(name, int) or name is None:
            return
        if (mode and any(c in mode for c in "wax+")) or (flags or 0) & WRITE_FLAGS:
            events.append(["write", path(name)])
    elif event in PATH_EVENTS:
        events.append(["write", path(args[0])])
    elif event in ("os.rename", "os.replace"):
        events.extend([["write", path(args[0])], ["write", path(args[1])]])
    elif event == "sqlite3.connect":
        name = os.fsdecode(args[0])
        if name != ":memory:":
            events.append(["write", path(name.removeprefix("file:").split("?")[0])])
    elif event in ("socket.bind", "socket.connect"):
        address = args[1]
        events.append([event, list(address) if isinstance(address, tuple) else str(address)])
    elif event == "socket.getaddrinfo":
        events.append([event, str(args[0])])
    elif event in EXEC_EVENTS:
        events.append(["exec", event])


sys.addaudithook(hook)
from ai_meterix.interfaces.cli import main

try:
    code = main(sys.argv[2:])
finally:
    json.dump(events, out)
    out.close()
sys.exit(code)
"""


class Sandbox:
    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        self.cwd = tmp_path / "cwd"
        self.home.mkdir()
        self.cwd.mkdir()
        self.hook = tmp_path / "hook.py"
        self.hook.write_text(_HOOK, encoding="utf-8")
        self.record = tmp_path / "events.json"
        self.env = {
            k: v for k, v in os.environ.items() if not k.startswith(("AIMETERIX_", "PYTHON"))
        }
        self.env.update(HOME=str(self.home), PYTHONDONTWRITEBYTECODE="1")

    def start(self, *args: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [sys.executable, str(self.hook), str(self.record), *args],
            cwd=self.cwd,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def events(self, process: subprocess.Popen[str]) -> list[list[Any]]:
        output, _ = process.communicate(timeout=60)
        assert process.returncode == 0, output
        events: list[list[Any]] = json.loads(self.record.read_text())
        return events

    def check_writes(self, events: list[list[Any]]) -> list[str]:
        allowed = (self.home / ".aimeterix").resolve()
        writes = sorted({e[1] for e in events if e[0] == "write"})
        outside = [w for w in writes if not Path(w).resolve().is_relative_to(allowed)]
        assert outside == [], f"written outside ~/.aimeterix: {outside}"
        assert list(self.cwd.iterdir()) == []
        assert [p.name for p in self.home.iterdir()] == [".aimeterix"]
        return writes


def _loopback(address: Any) -> bool:
    host = address[0] if isinstance(address, list) else address
    return host in ("127.0.0.1", "::1", "localhost")


def test_the_agent_writes_only_to_its_folder_and_opens_no_connections(tmp_path: Path) -> None:
    box = Sandbox(tmp_path)
    events = box.events(box.start("agent", "--mock", "--interval", "1", "--for", "3s"))
    writes = box.check_writes(events)
    assert any(w.endswith("meter.db") for w in writes)
    assert [e for e in events if e[0] == "exec"] == []
    assert [e for e in events if e[0].startswith("socket")] == []


class _Provider(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = b'{"model": "m", "usage": {"prompt_tokens": 2, "completion_tokens": 1}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _post_when_up(port: int, deadline_s: float = 20.0) -> int:
    import http.client

    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/v1/chat/completions", body=b'{"model": "m"}')
            status = conn.getresponse().status
            conn.close()
            return status
        except OSError:
            time.sleep(0.1)
    raise AssertionError("the proxy did not start")


def test_the_proxy_listens_on_loopback_and_connects_only_upstream(tmp_path: Path) -> None:
    provider = ThreadingHTTPServer(("127.0.0.1", 0), _Provider)
    threading.Thread(target=provider.serve_forever, args=(0.05,), daemon=True).start()
    upstream = f"http://127.0.0.1:{provider.server_address[1]}"
    port = _free_port()
    box = Sandbox(tmp_path)
    try:
        process = box.start("proxy", "--port", str(port), "--upstream", upstream, "--for", "4s")
        assert _post_when_up(port) == 200
        events = box.events(process)
    finally:
        provider.shutdown()
    box.check_writes(events)
    binds = [e[1] for e in events if e[0] == "socket.bind"]
    connects = [e[1] for e in events if e[0] == "socket.connect"]
    lookups = [e[1] for e in events if e[0] == "socket.getaddrinfo"]
    assert binds and all(_loopback(b) for b in binds), binds
    assert connects and all(_loopback(c) for c in connects), connects
    assert all(_loopback(h) for h in lookups), lookups
    assert [e for e in events if e[0] == "exec"] == []


# --- configuration and guards ------------------------------------------------------


def test_servers_default_to_loopback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = load_config(env={})
    assert (config.proxy.host, config.proxy.port) == ("127.0.0.1", 8787)
    assert (config.api.host, config.api.port) == ("127.0.0.1", 8788)
    assert config.storage.url == "sqlite:///~/.aimeterix/meter.db"


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10"])
def test_the_proxy_refuses_to_listen_beyond_this_machine(tmp_path: Path, host: str) -> None:
    recorder = Recorder(lambda: open_writer("sqlite:///:memory:"), data_dir=tmp_path)
    with pytest.raises(ValueError, match=r"127\.0\.0\.1 only"):
        serve(Router({}), recorder, host=host, port=0)


def test_the_proxy_cli_refuses_a_public_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from ai_meterix.interfaces.cli import main

    monkeypatch.setenv("AIMETERIX_PROXY_HOST", "0.0.0.0")
    monkeypatch.setenv("AIMETERIX_STORAGE_URL", f"sqlite:///{tmp_path / 'm.db'}")
    monkeypatch.chdir(tmp_path)
    assert main(["proxy", "--for", "1s"]) == 1
    assert "127.0.0.1 only" in capsys.readouterr().err


def test_model_status_is_asked_on_this_machine_only() -> None:
    for url in ("http://example.com:11434/api/ps", "http://10.0.0.2:8080/props"):
        with pytest.raises(ValueError):
            local_fetch(url)
