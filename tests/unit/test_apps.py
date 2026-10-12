"""Per-app CPU and GPU time, attributed energy and local model detection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from ai_meterix.collectors.macos.apps import OTHER, AppTracker
from ai_meterix.collectors.macos.native import parse_procargs
from ai_meterix.collectors.runtimes import (
    DetectedModel,
    detect_models,
    llama_cpp_model,
    local_fetch,
)

T0 = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
LLAMA_ARGS = ["llama-server", "-hf", "org/Model-GGUF:Q4", "--alias", "qwen-a3b", "--port", "8080"]


class FakeReaders:
    """Process readers with state the test changes between ticks."""

    def __init__(self) -> None:
        self.cpu: dict[int, tuple[float, int]] = {}  # pid -> (CPU seconds, start)
        self.gpu: dict[int, int] = {}  # pid -> GPU ns
        self.names: dict[int, str] = {}
        self.args: dict[int, list[str]] = {}
        self.usage_calls: list[list[int] | None] = []

    def process_usage(self, pids: list[int] | None = None) -> list[list[Any]]:
        self.usage_calls.append(pids)
        wanted = self.cpu if pids is None else {p: self.cpu[p] for p in pids if p in self.cpu}
        return [[pid, cpu, start] for pid, (cpu, start) in sorted(wanted.items())]

    def gpu_clients(self) -> list[dict[str, Any]]:
        return [{"pid": pid, "gpu_ns": ns} for pid, ns in self.gpu.items()]

    def process_name(self, pid: int) -> str | None:
        return self.names.get(pid)

    def process_args(self, pid: int) -> list[str] | None:
        return self.args.get(pid)


def no_fetch(url: str) -> Any:
    raise OSError("connection refused")


class Ticker:
    def __init__(self, tracker: AppTracker, interval: float = 10.0) -> None:
        self.tracker = tracker
        self.interval = interval
        self.n = 0
        self.problems: list[str] = []

    def tick(
        self,
        cpu_j: float | None = 0.0,
        busy: float | None = 0.0,
        gpu_j: float | None = 0.0,
        gpu_busy: float | None = 0.0,
    ) -> tuple[dict[str, Any], list[Any]]:
        t = self.n * self.interval
        self.n += 1
        self.problems = []
        samples, models = self.tracker.sample(
            T0 + timedelta(seconds=t),
            t,
            self.interval,
            cpu_j=cpu_j,
            busy_cpu_s=busy,
            gpu_j=gpu_j,
            gpu_busy_s=gpu_busy,
            problems=self.problems,
        )
        return {s.process_name: s for s in samples}, models


def setup(**kwargs: Any) -> tuple[FakeReaders, Ticker]:
    r = FakeReaders()
    r.names = {10: "editor", 20: "browser", 30: "daemon", 40: "WindowServer"}
    r.cpu = {10: (100.0, 1), 20: (50.0, 2), 30: (5.0, 3)}
    r.gpu = {20: 0, 40: 0}
    tracker = AppTracker("m4", r, fetch=no_fetch, **kwargs)  # type: ignore[arg-type]
    return r, Ticker(tracker)


def test_first_tick_only_records_starting_values() -> None:
    _, ticker = setup()
    samples, _ = ticker.tick(cpu_j=None, busy=None, gpu_j=None, gpu_busy=None)
    assert samples == {}


def test_cpu_energy_is_split_by_share_of_busy_cpu_time() -> None:
    r, ticker = setup()
    ticker.tick()
    r.cpu = {10: (104.0, 1), 20: (51.0, 2), 30: (5.0, 3)}
    samples, _ = ticker.tick(cpu_j=20.0, busy=10.0)  # 2 J per busy CPU second
    assert samples["editor"].cpu_time_s == pytest.approx(4.0)
    assert samples["editor"].attributed_energy_j == pytest.approx(8.0)
    assert samples["browser"].attributed_energy_j == pytest.approx(2.0)
    assert "daemon" not in samples  # no CPU time used, no row
    # 5 busy CPU seconds belong to nobody we can read: their energy stays unattributed
    assert sum(s.attributed_energy_j or 0 for s in samples.values()) == pytest.approx(10.0)
    assert samples["editor"].interval_s == 10.0
    assert samples["editor"].gpu_time_s is None


def test_gpu_energy_is_split_by_gpu_time_including_unreadable_apps() -> None:
    r, ticker = setup()
    ticker.tick()
    r.gpu = {20: 3_000_000_000, 40: 1_000_000_000}
    samples, _ = ticker.tick(gpu_j=80.0, gpu_busy=5.0)
    # the GPU was busy 5 s, the apps add up to 4 s: 1 s of energy stays unattributed
    assert samples["browser"].gpu_time_s == pytest.approx(3.0)
    assert samples["browser"].attributed_energy_j == pytest.approx(48.0)
    window_server = samples["WindowServer"]
    assert window_server.cpu_time_s is None  # needs admin rights to read
    assert window_server.attributed_energy_j == pytest.approx(16.0)


def test_apps_with_little_use_are_added_up_into_one_row() -> None:
    r, ticker = setup(min_share=0.01)
    ticker.tick()
    r.cpu = {10: (110.0, 1), 20: (50.01, 2), 30: (5.02, 3)}
    samples, _ = ticker.tick(cpu_j=11.0, busy=11.0)
    assert set(samples) == {"editor", OTHER}
    other = samples[OTHER]
    assert other.pid == 0
    assert other.cpu_time_s == pytest.approx(0.03)
    assert other.attributed_energy_j == pytest.approx(0.03)


def test_between_full_scans_only_watched_apps_are_read() -> None:
    r, ticker = setup(full_scan_s=60.0, top_n=1)
    ticker.tick()  # t=0, full scan
    r.cpu = {10: (101.0, 1), 20: (50.5, 2), 30: (5.0, 3)}
    ticker.tick(cpu_j=3.0, busy=1.5)  # t=10, second full scan picks the top app
    assert r.usage_calls == [None, None]
    r.gpu = {20: 2_000_000_000, 40: 0}
    r.cpu = {10: (102.0, 1), 20: (51.0, 2), 30: (6.0, 3)}
    samples, _ = ticker.tick(cpu_j=5.0, busy=2.5)  # t=20: editor (top) and browser (GPU)
    assert r.usage_calls[-1] == [10, 20]
    assert "daemon" not in samples
    for _ in range(4):  # t=30 to 60
        ticker.tick(cpu_j=2.0, busy=1.0)
    samples, _ = ticker.tick(cpu_j=2.0, busy=1.0)  # t=70: full scan again
    assert r.usage_calls[-1] is None
    daemon = samples["daemon"]
    assert daemon.cpu_time_s == pytest.approx(1.0)
    assert daemon.interval_s == pytest.approx(60.0)  # covers the time since it was last read
    assert daemon.attributed_energy_j == pytest.approx(2.0)  # 2 J per busy second over that window


def test_a_reused_pid_counts_as_a_new_process() -> None:
    r, ticker = setup()
    ticker.tick()
    r.cpu = {10: (3.0, 99), 20: (50.0, 2), 30: (5.0, 3)}  # pid 10 restarted, now 3 s
    samples, _ = ticker.tick(cpu_j=6.0, busy=3.0)
    assert samples["editor"].cpu_time_s == pytest.approx(3.0)


def test_missing_energy_leaves_attributed_energy_missing() -> None:
    r, ticker = setup()
    ticker.tick()
    r.cpu = {10: (104.0, 1), 20: (50.0, 2), 30: (5.0, 3)}
    r.gpu = {20: 1_000_000_000, 40: 0}
    samples, _ = ticker.tick(cpu_j=None, busy=4.0, gpu_j=None, gpu_busy=1.0)
    assert samples["editor"].cpu_time_s == pytest.approx(4.0)
    assert samples["editor"].attributed_energy_j is None
    assert samples["browser"].attributed_energy_j is None


def test_failed_readers_are_reported_not_raised() -> None:
    class Broken(FakeReaders):
        def process_usage(self, pids: list[int] | None = None) -> list[list[Any]]:
            raise OSError("cannot list processes")

        def gpu_clients(self) -> list[dict[str, Any]]:
            raise OSError("no GPU")

    ticker = Ticker(AppTracker("m4", Broken(), fetch=no_fetch))  # type: ignore[arg-type]
    ticker.tick()
    assert ticker.problems == ["apps.gpu: no GPU", "apps.cpu: cannot list processes"]


def test_llama_server_model_is_linked_to_its_process() -> None:
    r, ticker = setup()
    r.names[50] = "llama-server"
    r.cpu[50] = (10.0, 5)
    r.gpu[50] = 0
    r.args[50] = LLAMA_ARGS

    def fetch(url: str) -> Any:
        if url == "http://127.0.0.1:8080/props":
            return {"model_alias": "qwen-a3b", "model_path": "/models/Model-Q4.gguf"}
        if url == "http://127.0.0.1:8080/v1/models":
            return {"data": [{"id": "qwen-a3b", "meta": {"size": 22_000_000_000}}]}
        raise AssertionError(url)

    ticker.tracker._fetch = fetch
    _, models = ticker.tick()
    (model,) = models
    assert model.id == "m4:llama.cpp:qwen-a3b"
    assert (model.runtime, model.model_name, model.pid) == ("llama.cpp", "qwen-a3b", 50)
    assert model.size_bytes == 22_000_000_000
    assert model.first_seen == T0

    r.cpu[50] = (11.0, 5)
    r.gpu[50] = 5_000_000_000
    samples, models = ticker.tick(cpu_j=2.0, busy=1.0, gpu_j=50.0, gpu_busy=5.0)
    llama = samples["llama-server"]
    assert llama.local_model_id == "m4:llama.cpp:qwen-a3b"
    assert llama.attributed_energy_j == pytest.approx(52.0)
    assert models[0].first_seen == T0  # still the first sighting
    assert models[0].last_seen == T0 + timedelta(seconds=10)


def test_llama_cpp_falls_back_to_the_command_line() -> None:
    assert llama_cpp_model(7, LLAMA_ARGS, no_fetch) == DetectedModel("llama.cpp", "qwen-a3b", 7)
    by_path = llama_cpp_model(7, ["llama-server", "--model=/m/Phi-4-Q8_0.gguf"], no_fetch)
    assert by_path == DetectedModel("llama.cpp", "Phi-4-Q8_0", 7)
    by_repo = llama_cpp_model(7, ["llama-server", "-hf", "org/Repo-GGUF"], no_fetch)
    assert by_repo is not None and by_repo.model_name == "org/Repo-GGUF"
    assert llama_cpp_model(7, ["llama-server"], no_fetch) is None


def test_llama_cpp_on_a_remote_interface_is_not_asked() -> None:
    def fetch(url: str) -> Any:
        raise AssertionError("must not be called")

    args = ["llama-server", "-m", "/m/x.gguf", "--host", "10.0.0.5"]
    assert llama_cpp_model(1, args, fetch) == DetectedModel("llama.cpp", "x", 1)


def ollama_fetch(models: list[dict[str, Any]]) -> Any:
    def fetch(url: str) -> Any:
        assert url == "http://127.0.0.1:11434/api/ps"
        return {"models": models}

    return fetch


def test_ollama_model_is_linked_when_one_runner_runs_one_model() -> None:
    procs = {1: "ollama", 2: "ollama", 3: "Ollama"}
    args = {1: ["/x/ollama", "serve"], 2: ["/x/ollama", "runner", "--port", "5"], 3: ["/x/Ollama"]}
    found = detect_models(procs, args.get, ollama_fetch([{"name": "llama3.1:8b", "size": 5}]))
    assert found == [DetectedModel("ollama", "llama3.1:8b", 2, 5)]


def test_ollama_models_without_a_clear_runner_have_no_process() -> None:
    procs = {1: "ollama", 2: "ollama", 4: "ollama"}
    args = {1: ["ollama", "serve"], 2: ["ollama", "runner"], 4: ["ollama", "runner"]}
    fetch = ollama_fetch([{"name": "a:1b"}, {"name": "b:7b"}])
    found = detect_models(procs, args.get, fetch)
    assert [(m.model_name, m.pid) for m in found] == [("a:1b", None), ("b:7b", None)]


def test_unreachable_ollama_is_a_problem_and_other_apps_are_not_asked() -> None:
    problems: list[str] = []
    asked: list[int] = []

    def read_args(pid: int) -> list[str] | None:
        asked.append(pid)
        return ["ollama", "serve"]

    found = detect_models({1: "ollama", 2: "Safari"}, read_args, no_fetch, problems)
    assert found == []
    assert asked == [1]  # only runtime processes have their arguments read
    assert problems == ["ollama: status not readable (connection refused)"]


def test_procargs_buffer_is_parsed_without_the_environment() -> None:
    raw = (2).to_bytes(
        4, "little"
    ) + b"/bin/llama-server\x00\x00\x00llama-server\x00-m\x00SECRET=1\x00"
    assert parse_procargs(raw) == ["llama-server", "-m"]
    assert parse_procargs(b"") == []


def test_local_fetch_only_asks_this_machine() -> None:
    with pytest.raises(ValueError):
        local_fetch("http://example.com:11434/api/ps")
