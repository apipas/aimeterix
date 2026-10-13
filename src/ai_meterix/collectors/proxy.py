"""The LLM proxy (PLAN.md Phase 2 step 2, records 0011, 0014 and 0017).

Apps point their SDK's base URL at the proxy and it forwards every request,
streaming included, to the provider unchanged. On the way back it reads the
token counts and records one ``LlmCall``: model, normalized token counts,
latency, status and the provider's request id. Prompts, responses and API
keys are never stored or logged.

URLs, for a route named ``openai``::

    http://127.0.0.1:8787/openai/v1/...            -> https://api.openai.com/v1/...
    http://127.0.0.1:8787/app/notes/openai/v1/...  -> same, caller app "notes"
    http://127.0.0.1:8787/v1/...                   -> the default upstream, if set

The caller (record 0011 item 5, record 0014 item 8) comes from the headers
``X-AIMeterix-App``, ``X-AIMeterix-Feature`` and ``X-AIMeterix-Tag-<name>``; the
app header wins over the path. These headers are not forwarded.

The proxy listens on loopback only. With ``allowed_key_hashes`` set it forwards
only requests whose API key hashes to one of them, so it cannot be used as an
open relay.

Calls are written through ``StorageWriter`` by a background thread about once
a second. When the database is down they stay in memory, with a copy in
``~/.aimeterix/proxy-buffer.jsonl``, and are written once it is back.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import http.client
import ipaddress
import json
import logging
import ssl
import threading
import time
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ai_meterix.core.model import Caller, CallType, LlmCall
from ai_meterix.core.timeutil import utc_now
from ai_meterix.pricing.prices import ensure_price_row
from ai_meterix.storage.base import StorageWriter

from .agent import DATA_DIR, from_record, to_record
from .usage import Usage, extract_usage

log = logging.getLogger("ai_meterix.proxy")

BUFFER_FILE = "proxy-buffer.jsonl"
HEADER_PREFIX = "x-aimeterix-"
TAG_PREFIX = "x-aimeterix-tag-"
UNKNOWN_APP = "unknown"
UPSTREAM_TIMEOUT_S = 600.0
CHUNK = 65536
KEEP_BYTES = 32 * 2**20  # responses up to this size are parsed whole
HEAD_BYTES = 2**20  # larger ones: only the start and the end are kept
TAIL_BYTES = 4 * 2**20

_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "accept-encoding",  # ask for plain bodies so the usage can be read
}
_KEY_HEADERS = ("x-api-key", "x-goog-api-key", "api-key")
_MODEL_CALLS = (
    "/chat/completions",
    "/completions",
    "/responses",
    "/messages",
    "/embeddings",
    "/api/chat",
    "/api/generate",
    "/api/embed",
    "/api/embeddings",
    ":generatecontent",
    ":streamgeneratecontent",
)


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def key_hash(key: str) -> str:
    """The value to put in ``proxy.allowed_key_hashes`` for an API key."""
    return hashlib.sha256(key.strip().encode("utf-8")).hexdigest()


# --- routing and callers -------------------------------------------------------------


@dataclass(frozen=True)
class Route:
    name: str
    upstream: str
    path: str  # the request path to send upstream
    app: str | None  # from /app/<name>/


class Router:
    def __init__(self, upstreams: Mapping[str, str], default: str | None = None) -> None:
        self.upstreams = {name: url.rstrip("/") for name, url in upstreams.items()}
        self.default = default.rstrip("/") if default else None

    def resolve(self, path: str) -> Route | None:
        app = None
        if path.startswith("/app/"):
            app, _, rest = path[len("/app/") :].partition("/")
            path = "/" + rest
            app = app or None
        first, _, rest = path[1:].partition("/")
        name = first.split("?", 1)[0]
        if name in self.upstreams:
            return Route(name, self.upstreams[name], "/" + rest, app)
        if self.default is not None:
            return Route("default", self.default, path, app)
        return None

    def describe(self) -> list[str]:
        lines = [f"/{name}/... -> {url}" for name, url in self.upstreams.items()]
        if self.default:
            lines.append(f"/...  -> {self.default} (default)")
        return lines


def caller_of(headers: Mapping[str, str], path_app: str | None) -> Caller:
    """The caller from the tag headers (lowercase names), else the path, else ``unknown``."""
    app = headers.get("x-aimeterix-app") or path_app or UNKNOWN_APP
    feature = headers.get("x-aimeterix-feature") or None
    tags = {
        name[len(TAG_PREFIX) :]: value
        for name, value in sorted(headers.items())
        if name.startswith(TAG_PREFIX) and name[len(TAG_PREFIX) :]
    }
    parts = [app] + ([feature] if feature else [])
    cid = ":".join(parts) + ("".join(f";{k}={v}" for k, v in tags.items()) if tags else "")
    return Caller(cid, app, feature, tags)


def call_type_of(method: str, path: str) -> CallType | None:
    """What kind of call a request is, or ``None`` when it is not metered (e.g. model lists)."""
    if method != "POST":
        return None
    p = path.split("?", 1)[0].rstrip("/").lower()
    if p.endswith("/batches"):
        return CallType.BATCH
    if p.endswith("/files"):
        return CallType.FILE
    if p.endswith(_MODEL_CALLS):
        return CallType.NORMAL
    return None


def provider_of(route: Route) -> str:
    host = urlsplit(route.upstream).hostname or ""
    if is_loopback(host):
        return "local" if route.name == "default" else route.name
    for name in ("openai", "anthropic", "googleapis", "openrouter", "groq", "mistral", "together"):
        if name in host:
            return "google" if name == "googleapis" else name
    return route.name if route.name != "default" else host


def request_model(body: bytes | None, path: str) -> str | None:
    """The model a request asks for, from its JSON body or (Gemini) its path."""
    if "/models/" in path and ":" in path:
        return path.split("/models/", 1)[1].split(":", 1)[0] or None
    if not body:
        return None
    try:
        doc = json.loads(body)
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    if isinstance(doc.get("model"), str):
        return str(doc["model"])
    requests = doc.get("requests")  # Anthropic message batches
    if isinstance(requests, list) and requests and isinstance(requests[0], dict):
        params = requests[0].get("params")
        if isinstance(params, dict) and isinstance(params.get("model"), str):
            return str(params["model"])
    return None


# --- the recorder ---------------------------------------------------------------------


class Recorder:
    """Writes calls in the background; keeps them when the database is down."""

    def __init__(
        self,
        open_storage: Callable[[], StorageWriter],
        *,
        data_dir: Path = DATA_DIR,
        flush_s: float = 1.0,
        max_pending: int = 100_000,
    ) -> None:
        self._open_storage = open_storage
        self._storage: StorageWriter | None = None
        self.data_dir = data_dir.expanduser()
        self.flush_s = flush_s
        self.max_pending = max_pending
        self._lock = threading.Lock()
        self._pending: list[LlmCall] = []
        self._callers: dict[str, Caller] = {}
        self._saved_callers: set[str] = set()
        self._priced_models: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.written = 0
        self.dropped = 0
        self.last_error: str | None = None
        self._load_buffer()

    @property
    def buffer_path(self) -> Path:
        return self.data_dir / BUFFER_FILE

    def add(self, call: LlmCall, caller: Caller) -> None:
        with self._lock:
            self._callers[caller.id] = caller
            self._pending.append(call)
            if len(self._pending) > self.max_pending:
                del self._pending[0]
                self.dropped += 1

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._pending)

    def flush(self) -> bool:
        with self._lock:
            calls = list(self._pending)
            callers = [c for cid, c in self._callers.items() if cid not in self._saved_callers]
        if not calls and not callers and not self.buffer_path.exists():
            return True
        try:
            if self._storage is None:
                storage = self._open_storage()
                storage.migrate()
                self._storage = storage
            if callers:
                self._storage.upsert_callers(callers)
            if calls:
                self.written += self._storage.write_calls(calls)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            if reason != self.last_error:
                log.warning("cannot write to the database, keeping calls in a buffer: %s", reason)
            self.last_error = reason
            if self._storage is not None:
                with contextlib.suppress(Exception):
                    self._storage.close()
                self._storage = None
            self._save_buffer()
            return False
        with self._lock:
            del self._pending[: len(calls)]
            self._saved_callers.update(c.id for c in callers)
            remaining = bool(self._pending)
        if self.last_error is not None:
            log.info("database reachable again; buffered calls written")
        self.last_error = None
        if not remaining:
            self.buffer_path.unlink(missing_ok=True)
        self._ensure_prices(calls)
        return True

    def _ensure_prices(self, calls: list[LlmCall]) -> None:
        """Give models seen for the first time their default price row (record 0012 D3.7)."""
        for call in calls:
            if call.model in self._priced_models or self._storage is None:
                continue
            try:
                ensure_price_row(
                    self._storage,
                    call.model,
                    local=call.provider == "local",
                    ts=call.ts,
                    by="proxy",
                )
            except Exception as exc:  # the calls are saved; the row is retried next time
                log.warning("cannot add a price row for %s: %s", call.model, exc)
                return
            self._priced_models.add(call.model)

    def _records(self) -> Iterable[dict[str, Any]]:
        with self._lock:
            callers = list(self._callers.values())
            calls = list(self._pending)
        for caller in callers:
            yield to_record("caller", caller)
        for call in calls:
            yield to_record("call", call)

    def _save_buffer(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.buffer_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as out:
            for record in self._records():
                out.write(json.dumps(record, separators=(",", ":")) + "\n")
        tmp.replace(self.buffer_path)

    def _load_buffer(self) -> None:
        if not self.buffer_path.exists():
            return
        try:
            for line in self.buffer_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                kind, row = from_record(json.loads(line))
                if kind == "caller":
                    self._callers[row.id] = row
                elif kind == "call":
                    self._pending.append(row)
        except Exception as exc:
            log.warning("ignoring an unreadable buffer file %s: %s", self.buffer_path, exc)
            return
        log.info("loaded %d buffered calls from %s", len(self._pending), self.buffer_path)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="aimeterix-recorder", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.flush_s):
            self.flush()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        try:
            self.flush()
        finally:
            if self._storage is not None:
                self._storage.close()
                self._storage = None


# --- the server -----------------------------------------------------------------------


class _Capture:
    """Keeps a response body for reading the usage: whole up to ``KEEP_BYTES``,
    else its start and end."""

    def __init__(self) -> None:
        self.head = bytearray()
        self.tail = bytearray()
        self.size = 0

    def add(self, chunk: bytes) -> None:
        self.size += len(chunk)
        if self.size <= KEEP_BYTES and not self.tail:
            self.head += chunk
            return
        if len(self.head) > HEAD_BYTES:  # first time over the limit
            self.tail = self.head[HEAD_BYTES:]
            del self.head[HEAD_BYTES:]
        self.tail += chunk
        if len(self.tail) > TAIL_BYTES:
            del self.tail[: len(self.tail) - TAIL_BYTES]

    def body(self) -> bytes:
        return bytes(self.head) if not self.tail else bytes(self.head) + b"\n" + bytes(self.tail)


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        router: Router,
        recorder: Recorder,
        allowed_key_hashes: Iterable[str] = (),
    ) -> None:
        if not is_loopback(address[0]):
            raise ValueError(
                f"the proxy listens on 127.0.0.1 only, not {address[0]!r} (record 0017)"
            )
        self.router = router
        self.recorder = recorder
        self.allowed = frozenset(allowed_key_hashes)
        self.upstreams = Upstreams()
        self._warned: set[str] = set()
        super().__init__(address, ProxyHandler)

    def server_close(self) -> None:
        super().server_close()
        self.upstreams.close()

    def warn_once(self, key: str, message: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(message)


def _api_key(headers: Mapping[str, str]) -> str | None:
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    for name in _KEY_HEADERS:
        if headers.get(name):
            return headers[name].strip()
    return None


class Upstreams:
    """Keeps connections to providers open between calls (HTTP keep-alive).

    A new TLS connection to a cloud provider costs tens of milliseconds, so
    idle connections are reused. A reused connection the provider has closed
    in the meantime is retried once on a new one; nothing was processed then.
    Remote hosts honor the usual ``HTTPS_PROXY`` / ``NO_PROXY`` settings;
    local ones never go through a proxy.
    """

    MAX_IDLE = 8  # per host

    def __init__(self, timeout_s: float = UPSTREAM_TIMEOUT_S) -> None:
        self.timeout_s = timeout_s
        self._idle: dict[tuple[str, str, int], list[http.client.HTTPConnection]] = {}
        self._lock = threading.Lock()
        self._tls = ssl.create_default_context()
        self.opened = 0

    def _new(self, scheme: str, host: str, port: int) -> http.client.HTTPConnection:
        self.opened += 1
        proxy = None
        if not is_loopback(host) and not urllib.request.proxy_bypass(host):
            proxy = urllib.request.getproxies().get(scheme)
        conn: http.client.HTTPConnection
        if proxy:
            p = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
            conn = http.client.HTTPConnection(
                p.hostname or "", p.port or 80, timeout=self.timeout_s
            )
            if scheme == "https":
                conn = http.client.HTTPSConnection(
                    p.hostname or "", p.port or 80, timeout=self.timeout_s, context=self._tls
                )
                conn.set_tunnel(host, port)
        elif scheme == "https":
            conn = http.client.HTTPSConnection(
                host, port, timeout=self.timeout_s, context=self._tls
            )
        else:
            conn = http.client.HTTPConnection(host, port, timeout=self.timeout_s)
        return conn

    def request(
        self, method: str, url: str, body: bytes | None, headers: Mapping[str, str]
    ) -> tuple[http.client.HTTPResponse, Callable[[], None]]:
        """Send a request; returns the response and a ``done`` callback that
        gives the connection back (call it after reading the body)."""
        parts = urlsplit(url)
        scheme = parts.scheme
        host = parts.hostname or ""
        port = parts.port or (443 if scheme == "https" else 80)
        key = (scheme, host, port)
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        for attempt in (1, 2):
            with self._lock:
                idle = self._idle.get(key)
                conn = idle.pop() if idle else None
            reused = conn is not None
            if conn is None:
                conn = self._new(scheme, host, port)
            try:
                conn.request(method, target, body=body, headers=dict(headers))
                response = conn.getresponse()
            except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError):
                conn.close()
                if reused and attempt == 1:
                    continue  # the provider closed an idle connection; try a fresh one
                raise
            except Exception:
                conn.close()
                raise
            return response, functools.partial(self._done, key, conn, response)
        raise AssertionError("unreachable")

    def _done(
        self,
        key: tuple[str, str, int],
        conn: http.client.HTTPConnection,
        response: http.client.HTTPResponse,
    ) -> None:
        if response.will_close or not response.isclosed():
            conn.close()
            return
        with self._lock:
            idle = self._idle.setdefault(key, [])
            if len(idle) < self.MAX_IDLE:
                idle.append(conn)
                return
        conn.close()

    def close(self) -> None:
        with self._lock:
            conns = [c for idle in self._idle.values() for c in idle]
            self._idle.clear()
        for conn in conns:
            conn.close()


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True  # small stream chunks go out at once
    server: ProxyServer

    def log_message(self, format: str, *args: Any) -> None:  # no request lines with paths
        pass

    def _send_error(self, status: int, message: str) -> None:
        body = json.dumps({"error": {"type": "aimeterix_proxy", "message": message}}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes | None:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            data = bytearray()
            while True:
                size = int(self.rfile.readline().split(b";", 1)[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    return bytes(data)
                data += self.rfile.read(size)
                self.rfile.readline()
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else None

    def _forward(self) -> None:
        started = time.monotonic()
        ts = utc_now()
        headers = {k.lower(): v for k, v in self.headers.items()}
        body = self._read_body()  # also before refusing, so the connection stays in step
        route = self.server.router.resolve(self.path)
        if route is None:
            names = ", ".join(self.server.router.upstreams) or "none"
            self._send_error(404, f"no upstream for this path; routes: {names}")
            return
        if self.server.allowed:
            key = _api_key(headers)
            if key is None or key_hash(key) not in self.server.allowed:
                self.server.warn_once("key", "refused a call with an API key not in the allowlist")
                self._send_error(403, "API key not allowed by this proxy")
                return
        forward = {
            k: v
            for k, v in self.headers.items()
            if k.lower() not in _HOP_HEADERS and not k.lower().startswith(HEADER_PREFIX)
        }
        try:
            response, done = self.server.upstreams.request(
                self.command, route.upstream + route.path, body, forward
            )
        except OSError as exc:
            self._send_error(502, f"upstream unreachable: {exc}")
            return
        call_type = call_type_of(self.command, route.path)
        capture = _Capture() if call_type is not None else None
        status = response.status
        try:
            self._relay(response, status, capture)
        finally:
            done()
        if call_type is not None and capture is not None:
            self._record(route, headers, body, response, status, capture, call_type, ts, started)

    def _relay(
        self, response: http.client.HTTPResponse, status: int, capture: _Capture | None
    ) -> None:
        self.send_response(status)
        for name, value in response.headers.items():
            if name.lower() not in _HOP_HEADERS:
                self.send_header(name, value)
        no_body = self.command == "HEAD" or status in (204, 304) or 100 <= status < 200
        if no_body:
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        client_open = True
        read = getattr(response, "read1", response.read)
        while True:
            chunk = read(CHUNK)
            if not chunk:
                break
            if capture is not None:
                capture.add(chunk)
            if client_open:
                try:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
                except OSError:  # the client went away; read on for the usage
                    client_open = False
        if client_open:
            with contextlib.suppress(OSError):
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        else:
            self.close_connection = True

    def _record(
        self,
        route: Route,
        headers: Mapping[str, str],
        body: bytes | None,
        response: http.client.HTTPResponse,
        status: int,
        capture: _Capture,
        call_type: CallType,
        ts: datetime,
        started: float,
    ) -> None:
        try:
            usage = extract_usage(capture.body()) if call_type is CallType.NORMAL else Usage()
            model = usage.model or request_model(body, route.path) or "unknown"
            caller = caller_of(headers, route.app)
            provider = provider_of(route)
            request_id = (
                response.headers.get("x-request-id")
                or response.headers.get("request-id")
                or usage.request_id
            )
            if call_type is CallType.NORMAL and not usage.found and status < 400:
                self.server.warn_once(
                    f"nousage:{provider}:{model}",
                    f"{provider} {model}: the response had no token counts. For OpenAI-style "
                    "streaming, ask for them with stream_options={'include_usage': true}",
                )
            call = LlmCall(
                ts=ts,
                provider=provider,
                model=model,
                caller_id=caller.id,
                call_type=call_type,
                request_id=request_id,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                reasoning_tokens=usage.reasoning_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                latency_s=round(time.monotonic() - started, 4),
                status=status,
                file_bytes=len(body or b"") if call_type is CallType.FILE else None,
                raw_usage=dict(usage.raw),
            )
            self.server.recorder.add(call, caller)
            log.debug(
                "%s %s %s: in %s out %s cache %s/%s",
                provider,
                model,
                status,
                call.input_tokens,
                call.output_tokens,
                call.cache_read_tokens,
                call.cache_write_tokens,
            )
        except Exception as exc:  # metering must never break the user's call
            self.server.warn_once(f"record:{type(exc).__name__}", f"could not record a call: {exc}")

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _forward


def serve(
    router: Router,
    recorder: Recorder,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    allowed_key_hashes: Iterable[str] = (),
) -> ProxyServer:
    """A proxy server bound to ``host:port``; call ``serve_forever`` to run it."""
    return ProxyServer((host, port), router, recorder, allowed_key_hashes)
