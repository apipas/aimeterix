"""Detect local LLM models and link them to their process (record 0011 item 3, D2.9).

Two runtimes are known in MVP-1:

- **llama.cpp** (``llama-server``): the model comes from its own ``/props`` and
  ``/v1/models`` endpoints, or from its command line when those don't answer.
  The alias (``--alias``) is preferred because clients send it as the model name.
- **Ollama**: loaded models come from its ``/api/ps`` endpoint. Each loaded model
  runs in an ``ollama runner`` process; the model is linked to it when there is
  exactly one of each, otherwise the model is recorded without a process.

Command-line arguments are read only for processes of these runtimes, and only
the model, alias, host and port are kept. Status endpoints are asked on
127.0.0.1 only, never through a proxy (record 0017 item 4).
"""

from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

LLAMA_CPP = "llama.cpp"
OLLAMA = "ollama"
OLLAMA_PORT = 11434
LLAMA_CPP_PORT = 8080
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "0.0.0.0", "::1", "::"}

Fetch = Callable[[str], Any]
"""GET a URL on 127.0.0.1 and return its JSON; raises on any failure."""


@dataclass(frozen=True)
class DetectedModel:
    runtime: str
    model_name: str
    pid: int | None = None
    size_bytes: int | None = None


def local_fetch(url: str, timeout_s: float = 1.0) -> Any:
    """GET a JSON endpoint on this machine, ignoring any proxy settings."""
    if not url.startswith("http://127.0.0.1:"):
        raise ValueError("only 127.0.0.1 endpoints are asked")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout_s) as response:
        return json.loads(response.read(1_000_000))


def runtime_of(process_name: str) -> str | None:
    name = process_name.lower()
    if name == "llama-server":
        return LLAMA_CPP
    if name == "ollama":
        return OLLAMA
    return None


def _options(args: Sequence[str], *names: str) -> str | None:
    """The value after the last of ``names`` (``-m x`` or ``--model=x``)."""
    value = None
    for i, arg in enumerate(args):
        for name in names:
            if arg == name and i + 1 < len(args):
                value = args[i + 1]
            elif arg.startswith(name + "="):
                value = arg.split("=", 1)[1]
    return value


def _model_file_name(path: str) -> str:
    name = os.path.basename(path.rstrip("/"))
    return name[: -len(".gguf")] if name.lower().endswith(".gguf") else name


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def llama_cpp_model(pid: int, args: Sequence[str], fetch: Fetch) -> DetectedModel | None:
    alias = _options(args, "-a", "--alias")
    path = _options(args, "-m", "--model")
    repo = _options(args, "-hf", "-hfr", "--hf-repo")
    host = _options(args, "--host") or "127.0.0.1"
    port = _options(args, "--port") or str(LLAMA_CPP_PORT)
    size = None
    if host in _LOCAL_HOSTS and port.isdigit():
        base = f"http://127.0.0.1:{int(port)}"
        try:
            props = fetch(f"{base}/props")
            alias = props.get("model_alias") or alias
            path = props.get("model_path") or path
        except Exception:
            pass
        try:
            for entry in fetch(f"{base}/v1/models").get("data", []):
                size = _int((entry.get("meta") or {}).get("size"))
                if size is not None:
                    break
        except Exception:
            pass
    name = alias or (_model_file_name(path) if path else None) or repo
    return DetectedModel(LLAMA_CPP, name, pid, size) if name else None


def ollama_models(runner_pids: Sequence[int], fetch: Fetch) -> list[DetectedModel]:
    loaded = fetch(f"http://127.0.0.1:{OLLAMA_PORT}/api/ps").get("models") or []
    pid = runner_pids[0] if len(loaded) == 1 and len(runner_pids) == 1 else None
    return [
        DetectedModel(OLLAMA, str(entry["name"]), pid, _int(entry.get("size")))
        for entry in loaded
        if isinstance(entry, dict) and entry.get("name")
    ]


def detect_models(
    processes: dict[int, str],
    read_args: Callable[[int], list[str] | None],
    fetch: Fetch = local_fetch,
    problems: list[str] | None = None,
) -> list[DetectedModel]:
    """Models loaded by the runtimes among ``processes`` (pid to name)."""
    found: list[DetectedModel] = []
    ollama_seen = False
    runners: list[int] = []
    for pid, name in sorted(processes.items()):
        runtime = runtime_of(name)
        if runtime is None:
            continue
        args = read_args(pid) or []
        if runtime == LLAMA_CPP:
            model = llama_cpp_model(pid, args, fetch)
            if model is not None:
                found.append(model)
        else:
            ollama_seen = True
            if len(args) > 1 and args[1] == "runner":
                runners.append(pid)
    if ollama_seen:
        try:
            found.extend(ollama_models(runners, fetch))
        except Exception as exc:
            if problems is not None:
                problems.append(f"ollama: status not readable ({exc or type(exc).__name__})")
    return found
