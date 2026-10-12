"""The LLM proxy against a fake provider on 127.0.0.1 (PLAN.md Phase 2 step 2)."""

from __future__ import annotations

import http.client
import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from ai_meterix.collectors.proxy import (
    ProxyServer,
    Recorder,
    Router,
    call_type_of,
    caller_of,
    key_hash,
    serve,
)
from ai_meterix.core.model import CallType
from ai_meterix.interfaces.cli import main
from ai_meterix.storage import StorageWriter, open_writer

SECRET_PROMPT = "a prompt that must never be stored"


class Provider(BaseHTTPRequestHandler):
    """Answers like OpenAI or Anthropic would; echoes the headers it got."""

    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True
    release = threading.Event()  # lets a test hold a stream after its first event

    def log_message(self, *args: Any) -> None:
        pass

    def _json(self, status: int, doc: dict[str, Any], **headers: str) -> None:
        body = json.dumps(doc).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._json(200, {"data": [{"id": "gpt-x"}], "path": self.path})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        req = json.loads(raw) if raw.startswith(b"{") else {}
        seen = sorted(k.lower() for k in self.headers)
        if self.path.endswith("/files"):
            self._json(200, {"id": "file-1", "bytes": length})
        elif self.path.startswith("/bad"):
            self._json(400, {"error": {"message": "bad request"}}, x_request_id="req-bad")
        elif self.path.endswith("/messages") and req.get("stream"):
            self._anthropic_stream(req["model"])
        elif req.get("stream"):
            self._openai_stream(req["model"])
        else:
            usage = {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 64},
            }
            self._json(
                200,
                {"id": "chatcmpl-9", "model": req["model"], "usage": usage, "seen": seen},
                x_request_id="req-123",
            )

    def _start_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()

    def _event(self, doc: dict[str, Any]) -> None:
        self.wfile.write(f"data: {json.dumps(doc)}\n\n".encode())
        self.wfile.flush()

    def _openai_stream(self, model: str) -> None:
        self._start_stream()
        self._event({"model": model, "choices": [{"delta": {"content": "."}}]})
        self.release.wait(5)
        usage = {"prompt_tokens": 9, "completion_tokens": 3}
        self._event({"model": model, "choices": [], "usage": usage})
        self.wfile.write(b"data: [DONE]\n\n")

    def _anthropic_stream(self, model: str) -> None:
        self._start_stream()
        start = {
            "type": "message_start",
            "message": {
                "id": "msg_7",
                "model": model,
                "usage": {
                    "input_tokens": 30,
                    "cache_read_input_tokens": 500,
                    "cache_creation_input_tokens": 70,
                    "output_tokens": 1,
                },
            },
        }
        self._event(start)
        self._event({"type": "message_delta", "usage": {"output_tokens": 55}})


@dataclass
class Setup:
    proxy: ProxyServer
    recorder: Recorder
    db: Path

    @property
    def port(self) -> int:
        return int(self.proxy.server_address[1])

    def request(
        self, path: str, body: dict[str, Any] | None = None, method: str = "POST", **headers: str
    ) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = json.dumps(body).encode() if body is not None else None
        sent = {
            "Content-Type": "application/json",
            **{k.replace("_", "-"): v for k, v in headers.items()},
        }
        conn.request(method, path, body=data, headers=sent)
        response = conn.getresponse()
        result = response.status, response.read()
        conn.close()
        return result

    def calls(self) -> list[dict[str, Any]]:
        assert self.recorder.flush()
        if not self.db.exists():
            return []
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in conn.execute("SELECT * FROM llm_calls ORDER BY ts")]
        finally:
            conn.close()


def _server(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture
def setup(tmp_path: Path) -> Iterator[Setup]:
    upstream, url = _server(Provider)
    db = tmp_path / "meter.db"

    def open_storage() -> StorageWriter:
        return open_writer(f"sqlite:///{db}")

    recorder = Recorder(open_storage, data_dir=tmp_path / "data")
    proxy = serve(Router({"up": url}, default=url), recorder, port=0)
    threading.Thread(target=proxy.serve_forever, args=(0.05,), daemon=True).start()
    Provider.release.clear()
    try:
        yield Setup(proxy, recorder, db)
    finally:
        Provider.release.set()
        proxy.shutdown()
        proxy.server_close()
        recorder.close()
        upstream.shutdown()


def test_a_call_is_forwarded_and_recorded_with_normalized_counts(setup: Setup) -> None:
    status, body = setup.request(
        "/up/v1/chat/completions",
        {"model": "gpt-x", "messages": [{"role": "user", "content": SECRET_PROMPT}]},
        Authorization="Bearer sk-test",
        X_AIMeterix_Feature="summaries",
        X_AIMeterix_App="notes",
        X_AIMeterix_Tag_Team="research",
    )
    assert status == 200
    seen = json.loads(body)["seen"]
    assert "authorization" in seen  # the key reaches the provider
    assert not [h for h in seen if h.startswith("x-aimeterix")]  # our tags do not
    (call,) = setup.calls()
    assert call["model"] == "gpt-x"
    assert call["provider"] == "up"
    assert call["call_type"] == "normal"
    assert (call["input_tokens"], call["output_tokens"], call["cache_read_tokens"]) == (36, 20, 64)
    assert call["cache_write_tokens"] is None
    assert call["request_id"] == "req-123"
    assert call["status"] == 200
    assert call["caller_id"] == "notes:summaries;team=research"
    assert call["latency_s"] >= 0
    assert json.loads(call["raw_usage"])["usage.prompt_tokens"] == 100


def test_prompts_responses_and_keys_are_never_stored(setup: Setup, tmp_path: Path) -> None:
    setup.request(
        "/up/v1/chat/completions",
        {"model": "gpt-x", "messages": [{"role": "user", "content": SECRET_PROMPT}]},
        Authorization="Bearer sk-secret-key-123",
    )
    setup.calls()
    for path in tmp_path.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert SECRET_PROMPT.encode() not in data, path
            assert b"sk-secret-key-123" not in data, path


def test_streams_are_passed_on_as_they_arrive(setup: Setup) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", setup.port, timeout=10)
    body = json.dumps({"model": "gpt-x", "stream": True}).encode()
    conn.request(
        "POST", "/v1/chat/completions", body=body, headers={"Content-Type": "application/json"}
    )
    response = conn.getresponse()
    first = response.read1(65536)  # arrives while the provider still holds the rest
    assert b'"content": "."' in first
    Provider.release.set()
    rest = response.read()
    conn.close()
    assert b"[DONE]" in first + rest
    (call,) = setup.calls()
    assert call["provider"] == "local"  # the default route points at 127.0.0.1
    assert (call["input_tokens"], call["output_tokens"]) == (9, 3)


def test_anthropic_stream_cache_counts(setup: Setup) -> None:
    status, _ = setup.request(
        "/app/writer/up/v1/messages", {"model": "claude-x", "stream": True}, x_api_key="k"
    )
    assert status == 200
    (call,) = setup.calls()
    assert call["caller_id"] == "writer"
    assert call["request_id"] == "msg_7"
    assert (
        call["input_tokens"],
        call["output_tokens"],
        call["cache_read_tokens"],
        call["cache_write_tokens"],
    ) == (30, 55, 500, 70)


def test_provider_errors_are_passed_on_and_recorded(setup: Setup) -> None:
    status, body = setup.request("/bad/v1/chat/completions", {"model": "gpt-x"})
    assert status == 400
    assert json.loads(body)["error"]["message"] == "bad request"
    (call,) = setup.calls()
    assert (call["status"], call["model"], call["request_id"]) == (400, "gpt-x", "req-bad")
    assert call["input_tokens"] is None


def test_model_lists_are_forwarded_but_not_metered(setup: Setup) -> None:
    status, body = setup.request("/up/v1/models?limit=5", method="GET")
    assert status == 200
    assert json.loads(body)["path"] == "/v1/models?limit=5"
    assert setup.calls() == []


def test_file_uploads_are_counted_with_their_size(setup: Setup) -> None:
    status, _ = setup.request("/up/v1/files", {"purpose": "batch", "data": "x" * 100})
    assert status == 200
    (call,) = setup.calls()
    assert call["call_type"] == "file"
    assert call["file_bytes"] > 100


def test_an_unreachable_upstream_gives_502(tmp_path: Path) -> None:
    recorder = Recorder(lambda: open_writer(f"sqlite:///{tmp_path / 'm.db'}"), data_dir=tmp_path)
    proxy = serve(Router({}, default="http://127.0.0.1:9"), recorder, port=0)
    threading.Thread(target=proxy.serve_forever, args=(0.05,), daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=10)
        conn.request("POST", "/v1/chat/completions", body=b"{}")
        response = conn.getresponse()
        assert response.status == 502
        assert b"upstream unreachable" in response.read()
    finally:
        proxy.shutdown()
        proxy.server_close()
        recorder.close()


def test_unknown_routes_get_404(tmp_path: Path) -> None:
    recorder = Recorder(lambda: open_writer(f"sqlite:///{tmp_path / 'm.db'}"), data_dir=tmp_path)
    proxy = serve(Router({"openai": "https://api.openai.com"}), recorder, port=0)
    threading.Thread(target=proxy.serve_forever, args=(0.05,), daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=10)
        conn.request("POST", "/v1/chat/completions", body=b"{}")
        response = conn.getresponse()
        assert response.status == 404
        assert b"routes: openai" in response.read()
    finally:
        proxy.shutdown()
        proxy.server_close()
        recorder.close()


def test_the_key_allowlist_refuses_other_keys(setup: Setup) -> None:
    setup.proxy.allowed = frozenset({key_hash("sk-good")})
    status, body = setup.request(
        "/up/v1/chat/completions", {"model": "m"}, Authorization="Bearer sk-bad"
    )
    assert status == 403
    assert b"not allowed" in body
    status, _ = setup.request("/up/v1/chat/completions", {"model": "m"})
    assert status == 403
    status, _ = setup.request(
        "/up/v1/chat/completions", {"model": "m"}, Authorization="Bearer sk-good"
    )
    assert status == 200
    assert len(setup.calls()) == 1  # refused calls are never forwarded


def test_calls_survive_a_database_outage(tmp_path: Path) -> None:
    upstream, url = _server(Provider)
    up = {"ok": False}

    def open_storage() -> StorageWriter:
        if not up["ok"]:
            raise OSError("database is locked")
        return open_writer(f"sqlite:///{tmp_path / 'meter.db'}")

    recorder = Recorder(open_storage, data_dir=tmp_path / "data")
    proxy = serve(Router({}, default=url), recorder, port=0)
    threading.Thread(target=proxy.serve_forever, args=(0.05,), daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=10)
        conn.request("POST", "/v1/chat/completions", body=json.dumps({"model": "m"}).encode())
        assert conn.getresponse().status == 200  # the app is not affected
    finally:
        proxy.shutdown()
        proxy.server_close()
        upstream.shutdown()
    assert not recorder.flush()
    buffer = tmp_path / "data" / "proxy-buffer.jsonl"
    assert buffer.exists()

    up["ok"] = True
    again = Recorder(open_storage, data_dir=tmp_path / "data")  # a restart
    assert again.pending == 1
    assert again.flush()
    assert again.written == 1
    assert not buffer.exists()
    storage = open_storage()
    try:
        assert [c.id for c in storage.get_callers()] == ["unknown"]
    finally:
        storage.close()
        again.close()


def test_callers_from_headers_and_path() -> None:
    assert caller_of({}, None).id == "unknown"
    assert caller_of({}, "notes").id == "notes"
    caller = caller_of({"x-aimeterix-app": "cli", "x-aimeterix-feature": "chat"}, "notes")
    assert (caller.id, caller.app, caller.feature) == ("cli:chat", "cli", "chat")


def test_call_types() -> None:
    assert call_type_of("POST", "/v1/chat/completions") is CallType.NORMAL
    assert call_type_of("POST", "/v1/messages?beta=true") is CallType.NORMAL
    assert call_type_of("POST", "/v1/messages/batches") is CallType.BATCH
    assert call_type_of("POST", "/v1/batches") is CallType.BATCH
    assert call_type_of("POST", "/v1/files") is CallType.FILE
    assert call_type_of("POST", "/v1beta/models/gemini-x:streamGenerateContent") is CallType.NORMAL
    assert call_type_of("GET", "/v1/models") is None
    assert call_type_of("POST", "/v1/messages/count_tokens") is None


def test_router() -> None:
    router = Router({"openai": "https://api.openai.com/"}, default="http://127.0.0.1:8080")
    route = router.resolve("/app/notes/openai/v1/chat/completions")
    assert route is not None
    assert (route.name, route.upstream, route.path, route.app) == (
        "openai",
        "https://api.openai.com",
        "/v1/chat/completions",
        "notes",
    )
    route = router.resolve("/v1/chat/completions")
    assert route is not None and route.name == "default" and route.path == "/v1/chat/completions"
    assert Router({}).resolve("/v1/x") is None


def test_hash_key_prints_the_hash_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("sk-abc\n"))
    assert main(["proxy", "--hash-key"]) == 0
    out = capsys.readouterr().out.strip()
    assert out == key_hash("sk-abc")
    assert "sk-abc" not in out


def test_proxy_overhead_is_small(setup: Setup) -> None:
    """Record 0017: under 5 ms added per call (median, local upstream, new connections)."""
    upstream, url = _server(Provider)
    port = int(url.rsplit(":", 1)[1])
    body = json.dumps({"model": "gpt-x"}).encode()

    def direct() -> None:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("POST", "/v1/chat/completions", body=body)
        conn.getresponse().read()
        conn.close()

    def proxied() -> None:
        setup.request("/up/v1/chat/completions", {"model": "gpt-x"})

    def median(call: Any) -> float:
        times = []
        for _ in range(30):
            t = time.perf_counter()
            call()
            times.append(time.perf_counter() - t)
        return sorted(times)[len(times) // 2]

    try:
        proxied()  # warm up
        overhead = median(proxied) - median(direct)
    finally:
        upstream.shutdown()
    assert overhead < 0.005, f"{overhead * 1000:.1f} ms"


def test_connections_to_the_provider_are_reused(setup: Setup) -> None:
    for _ in range(3):
        assert setup.request("/up/v1/chat/completions", {"model": "gpt-x"})[0] == 200
    assert setup.proxy.upstreams.opened == 1
    assert len(setup.calls()) == 3


class _ClosesSilently(Provider):
    """Drops each connection after answering, without saying so."""

    def do_POST(self) -> None:
        super().do_POST()
        self.close_connection = True


def test_a_connection_the_provider_closed_is_retried_once(tmp_path: Path) -> None:
    upstream, url = _server(_ClosesSilently)
    recorder = Recorder(lambda: open_writer(f"sqlite:///{tmp_path / 'm.db'}"), data_dir=tmp_path)
    proxy = serve(Router({}, default=url), recorder, port=0)
    threading.Thread(target=proxy.serve_forever, args=(0.05,), daemon=True).start()
    setup = Setup(proxy, recorder, tmp_path / "m.db")
    try:
        for _ in range(3):
            assert setup.request("/v1/chat/completions", {"model": "gpt-x"})[0] == 200
        assert len(setup.calls()) == 3
    finally:
        proxy.shutdown()
        proxy.server_close()
        recorder.close()
        upstream.shutdown()
