"""``aimeterix start`` / ``stop`` / ``health`` (PLAN.md Phase 5 steps 1b and 1c, record 0016).

One command runs the agent, the proxy and the API in the background, each as
its own process, so a crash in one never stops the proxy that apps depend on.
PID files go to the run folder and logs to the logs folder (``core/paths.py``). They
keep running after the terminal closes, not after a reboot (auto-start is MVP-2).
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ai_meterix.core.paths import Paths, paths
from ai_meterix.storage.base import Dataset, StorageReader, UsageQuery

SERVICES = ("agent", "proxy", "serve")
STOP_WAIT_S = 10.0
SAMPLE_STALE = timedelta(minutes=5)


def run_dir(where: Paths | None = None) -> Path:
    return (where or paths()).run_dir


def log_dir(where: Paths | None = None) -> Path:
    return (where or paths()).log_dir


def _pid_file(name: str, where: Paths | None) -> Path:
    return run_dir(where) / f"{name}.pid"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by someone else: not ours
        return False
    try:  # a reused PID belongs to some other program
        out = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    return "ai_meterix" in out or "aimeterix" in out


@dataclass(frozen=True, slots=True)
class Process:
    name: str
    pid: int | None
    running: bool
    since: datetime | None

    @property
    def uptime(self) -> timedelta | None:
        return None if self.since is None else datetime.now(timezone.utc) - self.since


def status(name: str, where: Paths | None = None) -> Process:
    path = _pid_file(name, where)
    try:
        pid = int(path.read_text().strip())
        since = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except (OSError, ValueError):
        return Process(name, None, False, None)
    running = _alive(pid)
    return Process(name, pid, running, since if running else None)


def start(
    names: Sequence[str],
    *,
    config: str | None = None,
    where: Paths | None = None,
    python: str = sys.executable,
    settle_s: float = 1.5,
) -> list[tuple[str, bool, str]]:
    """Start each service in the background; returns (name, ok, message)."""
    run_dir(where).mkdir(parents=True, exist_ok=True)
    log_dir(where).mkdir(parents=True, exist_ok=True)
    results = []
    for name in names:
        current = status(name, where)
        if current.running:
            results.append((name, True, f"already running (pid {current.pid})"))
            continue
        log_path = log_dir(where) / f"{name}.log"
        command = [python, "-m", "ai_meterix.interfaces.cli", name]
        if config:
            command += ["--config", str(Path(config).expanduser().resolve())]
        with open(log_path, "ab") as log:
            log.write(f"\n--- start {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC\n".encode())
            log.flush()
            child = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # keeps running after the terminal closes
            )
        _pid_file(name, where).write_text(f"{child.pid}\n")
        deadline = time.monotonic() + settle_s
        while time.monotonic() < deadline and child.poll() is None:
            time.sleep(0.1)
        if child.poll() is not None:
            _pid_file(name, where).unlink(missing_ok=True)
            results.append((name, False, f"exited at start; see {log_path}:\n{_tail(log_path)}"))
        else:
            results.append((name, True, f"started (pid {child.pid}, log {log_path})"))
    return results


def stop(
    names: Iterable[str], *, where: Paths | None = None, wait_s: float = STOP_WAIT_S
) -> list[tuple[str, bool, str]]:
    """Ask each service to stop (it flushes its buffer first) and check it is gone."""
    asked = []
    results = []
    for name in names:
        current = status(name, where)
        if current.pid is None or not current.running:
            _pid_file(name, where).unlink(missing_ok=True)
            results.append((name, True, "not running"))
            continue
        os.kill(current.pid, signal.SIGTERM)
        asked.append(current)
    deadline = time.monotonic() + wait_s
    for proc in asked:
        assert proc.pid is not None
        while time.monotonic() < deadline and _alive(proc.pid):
            time.sleep(0.1)
        if _alive(proc.pid):
            results.append((proc.name, False, f"still running (pid {proc.pid})"))
        else:
            _pid_file(proc.name, where).unlink(missing_ok=True)
            results.append((proc.name, True, "stopped"))
    return results


def port_in_use(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def last_seen(storage: StorageReader, dataset: Dataset, now: datetime) -> datetime | None:
    """Roughly when the newest row arrived (to the minute), within the last 6 hours."""
    table = storage.query(
        UsageQuery(dataset, now - timedelta(hours=6), now + timedelta(minutes=1),
                   bucket=timedelta(minutes=1))
    )  # fmt: skip
    if not table.rows:
        return None
    newest: datetime = max(row[0] for row in table.rows)
    return min(newest + timedelta(minutes=1), now)


def health(
    storage_url: str, *, where: Paths | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """The running setup: each process, the newest sample and call, the database.

    ``ok`` is true when all three processes run, the database answers and the
    newest hardware sample is less than five minutes old.
    """
    from ai_meterix.storage import open_reader

    now = now or datetime.now(timezone.utc)
    processes = [status(name, where) for name in SERVICES]
    problems = [f"{p.name} is not running" for p in processes if not p.running]
    report: dict[str, Any] = {
        "processes": [
            {
                "name": p.name,
                "running": p.running,
                "pid": p.pid if p.running else None,
                "uptime_s": int(p.uptime.total_seconds()) if p.uptime else None,
            }
            for p in processes
        ],
        "database": {"ok": False, "backend": None, "size_bytes": None, "detail": ""},
        "last_sample": None,
        "last_call": None,
    }
    try:
        storage = open_reader(storage_url)
    except Exception as exc:
        report["database"]["detail"] = str(exc)
        problems.append(f"database: {exc}")
    else:
        try:
            db = storage.health()
            report["database"].update(ok=db.ok, backend=db.backend, detail=db.detail)
            if not db.ok:
                problems.append(f"database: {db.detail}")
            else:
                report["last_sample"] = last_seen(storage, Dataset.DEVICE, now)
                report["last_call"] = last_seen(storage, Dataset.CALLS, now)
        finally:
            storage.close()
    if storage_url.startswith("sqlite"):
        from ai_meterix.storage.sqlite import parse_url

        with contextlib.suppress(OSError, ValueError):
            report["database"]["size_bytes"] = Path(parse_url(storage_url)).stat().st_size
    sample = report["last_sample"]
    if report["database"]["ok"] and (sample is None or now - sample > SAMPLE_STALE):
        problems.append("no hardware sample in the last 5 minutes")
    report["problems"] = problems
    report["ok"] = not problems
    return report


def _tail(path: Path, lines: int = 5) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


__all__ = [
    "SERVICES",
    "Process",
    "health",
    "last_seen",
    "log_dir",
    "port_in_use",
    "run_dir",
    "start",
    "status",
    "stop",
]
