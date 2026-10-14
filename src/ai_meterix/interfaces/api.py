"""The localhost API (PLAN.md Phase 3 step 6, record 0013).

A small JSON API on ``127.0.0.1:8788`` (standard library only), run by
``aimeterix serve``. It uses the same computation as the CLI, so both return
the same numbers. Money and energy are exact decimals sent as strings.

Log in with ``POST /api/login`` ``{"name", "password"}`` and send the token as
``Authorization: Bearer <token>``. Until an account has changed its first
password, only ``POST /api/password`` and ``/api/logout`` answer.

=======  ==============================  =====  ======================================
Method   Path                            Role   What
=======  ==============================  =====  ======================================
GET      /api/health                     none   storage reachable, schema version
POST     /api/login, /api/logout         none   start or end a session
POST     /api/password                   any    change your password (``old``,
                                                ``password``); admin: ``name`` too
GET      /api/usage                      user   ``from``, ``to``, ``last``, ``by``
GET      /api/prices                     user   price per 1M tokens in force now
GET      /api/prices/history             user   change log, ``model`` optional
GET      /api/prices/reference           user   the shipped, dated reference list
PUT      /api/prices/<model>             admin  ``input``, ``output``, ``cache_read``,
                                                ``cache_write``, ``from``, ``local``
POST     /api/prices/apply-reference     admin  ``dry_run``
GET      /api/profiles                   user   each machine's profile and history
PUT      /api/profiles/<host>            admin  ``rate``, ``pue``, ``fees``, ``spread``,
                                                ``name``, ``region``
GET      /api/config                     user   live settings, with their versions
GET      /api/config/history             user   change log, ``key`` optional
PUT      /api/config/<key>               admin  ``value``, or ``reset: true``
=======  ==============================  =====  ======================================
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone, tzinfo
from decimal import Decimal, InvalidOperation
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from ai_meterix.collectors.proxy import is_loopback
from ai_meterix.core.config import Config
from ai_meterix.core.errors import AiMeterixError
from ai_meterix.core.model import CURRENCY, PriceSource, Role
from ai_meterix.core.settings import LIVE, effective, live_values, reset_setting, set_setting
from ai_meterix.pricing.engine import CostEngine, Statement
from ai_meterix.pricing.prices import (
    apply_reference,
    current_prices,
    load_reference,
    set_price,
)
from ai_meterix.pricing.profiles import profile_dict, set_profile
from ai_meterix.storage import open_reader, open_writer
from ai_meterix.storage.base import StorageReader, StorageWriter

from .auth import Session, Sessions, set_password, verify_password
from .usage import period

log = logging.getLogger("ai_meterix.api")
MAX_BODY = 64 * 1024
_PRICE_KEYS = {
    "input": "input_per_m",
    "output": "output_per_m",
    "cache_read": "cache_read_per_m",
    "cache_write": "cache_write_per_m",
}


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _plain(value: Any) -> Any:
    """JSON-ready values: decimals as exact strings, times in ISO 8601 UTC."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return _plain(asdict(value))
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    return value


def statement_dict(statement: Statement, by: str = "day") -> dict[str, Any]:
    lines = statement.by_model() if by == "model" else statement.lines
    out: dict[str, Any] = _plain(
        {
            "start": statement.start,
            "end": statement.end,
            "tz": statement.tz,
            "currency": statement.currency,
            "by": by,
            "lines": [line.as_dict() for line in lines],
            "total": statement.total().as_dict(),
        }
    )
    return out


def _decimal(body: dict[str, Any], key: str) -> Decimal | None:
    if body.get(key) is None:
        return None
    try:
        return Decimal(str(body[key]))
    except InvalidOperation:
        raise ApiError(400, f"{key} must be a number") from None


def _day(text: str | None, name: str) -> date | None:
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ApiError(400, f"{name} must be a date like 2026-10-13") from None


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        storage_url: str,
        tz: tzinfo,
        sessions: Sessions | None = None,
        reader: Callable[[str], StorageReader] = open_reader,
        writer: Callable[[str], StorageWriter] = open_writer,
        config: Config | None = None,
    ) -> None:
        if not is_loopback(address[0]):
            raise ValueError(
                f"the API listens on 127.0.0.1 only, not {address[0]!r} (record 0017); "
                "access from other machines comes later, with its own token"
            )
        self.storage_url = storage_url
        self.tz = tz
        self.sessions = sessions or Sessions()
        self.config = config or Config()
        self._reader = reader
        self._writer = writer
        super().__init__(address, _Handler)

    @contextmanager
    def reader(self) -> Iterator[StorageReader]:
        storage = self._reader(self.storage_url)
        try:
            yield storage
        finally:
            storage.close()

    @contextmanager
    def writer(self) -> Iterator[StorageWriter]:
        storage = self._writer(self.storage_url)
        try:
            yield storage
        finally:
            storage.close()


class _Handler(BaseHTTPRequestHandler):
    server: ApiServer
    server_version = "aimeterix"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("%s %s", self.address_string(), format % args)

    # --- plumbing ---------------------------------------------------------------

    def _send(self, status: int, payload: Any) -> None:
        data = json.dumps(_plain(payload), indent=1).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ApiError(413, "request body too large")
        if not length:
            return {}
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            raise ApiError(400, "the body must be JSON") from None
        if not isinstance(body, dict):
            raise ApiError(400, "the body must be a JSON object")
        return body

    def _token(self) -> str:
        auth = self.headers.get("Authorization", "")
        return auth[7:].strip() if auth.lower().startswith("bearer ") else ""

    def _session(self, *, allow_must_change: bool = False) -> Session:
        session = self.server.sessions.get(self._token())
        if session is None:
            raise ApiError(401, "log in first: POST /api/login")
        if session.must_change and not allow_must_change:
            raise ApiError(403, "change your password first: POST /api/password")
        return session

    def _admin(self) -> Session:
        session = self._session()
        if session.role is not Role.ADMIN:
            raise ApiError(403, "this needs the admin account")
        return session

    def _dispatch(self, method: str) -> None:
        parts = urlsplit(self.path)
        path = parts.path.rstrip("/")
        query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
        try:
            handler, arg = self._route(method, path)
            result = handler(query, arg) if arg is not None else handler(query)
            self._send(200, result)
        except ApiError as exc:
            self._send(exc.status, {"error": str(exc)})
        except (AiMeterixError, ValueError) as exc:
            self._send(400, {"error": str(exc)})
        except Exception:
            log.exception("API error on %s %s", method, path)
            self._send(500, {"error": "internal error; see the server log"})

    def _route(self, method: str, path: str) -> tuple[Callable[..., Any], str | None]:
        fixed: dict[tuple[str, str], Callable[..., Any]] = {
            ("GET", "/api/health"): self.health,
            ("POST", "/api/login"): self.login,
            ("POST", "/api/logout"): self.logout,
            ("POST", "/api/password"): self.password,
            ("GET", "/api/usage"): self.usage,
            ("GET", "/api/prices"): self.prices,
            ("GET", "/api/prices/history"): self.price_history,
            ("GET", "/api/prices/reference"): self.reference,
            ("POST", "/api/prices/apply-reference"): self.apply_reference,
            ("GET", "/api/profiles"): self.profiles,
            ("GET", "/api/config"): self.settings,
            ("GET", "/api/config/history"): self.setting_history,
        }
        if (method, path) in fixed:
            return fixed[(method, path)], None
        for prefix, handler in (
            ("/api/prices/", self.put_price),
            ("/api/profiles/", self.put_profile),
            ("/api/config/", self.put_setting),
        ):
            if path.startswith(prefix) and len(path) > len(prefix):
                if method != "PUT":
                    raise ApiError(405, f"use PUT on {prefix}<name>")
                return handler, unquote(path[len(prefix) :])
        if any(p == path for _, p in fixed):
            raise ApiError(405, "method not allowed")
        raise ApiError(404, "no such endpoint")

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    # --- endpoints --------------------------------------------------------------

    def health(self, query: dict[str, str]) -> dict[str, Any]:
        with self.server.reader() as storage:
            status = storage.health()
        return {"ok": status.ok, "storage": status.backend, "schema": status.schema_version}

    def login(self, query: dict[str, str]) -> dict[str, Any]:
        body = self._body()
        with self.server.reader() as storage:
            found = self.server.sessions.login(
                storage, str(body.get("name", "")), str(body.get("password", ""))
            )
        if found is None:
            time.sleep(0.5)  # slows down guessing
            raise ApiError(401, "wrong name or password")
        token, session = found
        return {"token": token, "role": session.role, "must_change": session.must_change}

    def logout(self, query: dict[str, str]) -> dict[str, Any]:
        self.server.sessions.logout(self._token())
        return {"ok": True}

    def password(self, query: dict[str, str]) -> dict[str, Any]:
        session = self._session(allow_must_change=True)
        body = self._body()
        name = str(body.get("name") or session.name)
        new = str(body.get("password", ""))
        with self.server.writer() as storage:
            if name == session.name:
                user = storage.get_user(name)
                if user is None or not verify_password(
                    str(body.get("old", "")), user.password_hash
                ):
                    raise ApiError(403, "old password is wrong")
            elif session.role is not Role.ADMIN or session.must_change:
                raise ApiError(403, "only the admin can change another account's password")
            set_password(storage, name, new)
        self.server.sessions.drop_user(name, keep=self._token())
        self.server.sessions.changed_password(name)
        return {"ok": True, "name": name}

    def usage(self, query: dict[str, str]) -> dict[str, Any]:
        self._session()
        by = query.get("by", "day")
        if by not in ("day", "model"):
            raise ApiError(400, "by must be day or model")
        last = query.get("last")
        last_s = None
        if last:
            from .cli import duration

            try:
                last_s = duration(last)
            except Exception:
                raise ApiError(400, "last must be like 24h, 90m or 3600") from None
        now = datetime.now(timezone.utc)
        start, end = period(
            now,
            self.server.tz,
            _day(query.get("from"), "from"),
            _day(query.get("to"), "to"),
            last_s,
        )
        with self.server.reader() as storage:
            statement = CostEngine(storage, self.server.tz).statement(start, end, now=now)
        return statement_dict(statement, by)

    def prices(self, query: dict[str, str]) -> dict[str, Any]:
        self._session()
        now = datetime.now(timezone.utc)
        with self.server.reader() as storage:
            rows = current_prices(storage, now)
        reference = load_reference()
        return {
            "currency": CURRENCY,
            "unit": "per 1M tokens",
            "prices": rows,
            "reference_checked": reference.checked,
        }

    def price_history(self, query: dict[str, str]) -> dict[str, Any]:
        self._session()
        with self.server.reader() as storage:
            return {
                "prices": storage.get_prices(query.get("model")),
                "changes": storage.get_price_changes(query.get("model")),
            }

    def reference(self, query: dict[str, str]) -> dict[str, Any]:
        self._session()
        return {"reference": load_reference()}

    def put_price(self, query: dict[str, str], model: str) -> dict[str, Any]:
        session = self._admin()
        body = self._body()
        values = {
            field: value
            for key, field in _PRICE_KEYS.items()
            if (value := _decimal(body, key)) is not None
        }
        since = _day(body.get("from"), "from")
        start = (
            datetime.combine(since, datetime.min.time(), self.server.tz).astimezone(timezone.utc)
            if since
            else None
        )
        with self.server.writer() as storage:
            price = set_price(
                storage,
                model,
                values,
                now=datetime.now(timezone.utc),
                by=session.name,
                start=start,
                source=PriceSource.LOCAL if body.get("local") else None,
            )
        return {"price": price}

    def apply_reference(self, query: dict[str, str]) -> dict[str, Any]:
        session = self._admin()
        body = self._body()
        dry_run = bool(body.get("dry_run"))
        reference = load_reference()
        with self.server.writer() as storage:
            results = apply_reference(
                storage, reference, now=datetime.now(timezone.utc), by=session.name, dry_run=dry_run
            )
        return {"checked": reference.checked, "dry_run": dry_run, "results": results}

    def profiles(self, query: dict[str, str]) -> dict[str, Any]:
        self._session()
        with self.server.reader() as storage:
            out = []
            for inventory in storage.get_hosts():
                host = inventory.host.id
                current = storage.get_profile(host)
                out.append(
                    {
                        "host": host,
                        "current": None if current is None else profile_dict(current),
                        "history": [profile_dict(p) for p in storage.get_profile_history(host)],
                    }
                )
        return {"profiles": out}

    def put_profile(self, query: dict[str, str], host: str) -> dict[str, Any]:
        session = self._admin()
        body = self._body()
        with self.server.writer() as storage:
            if host not in {h.host.id for h in storage.get_hosts()}:
                raise ApiError(404, f"no machine named {host!r}")
            profile = set_profile(storage, host, body, by=session.name)
        return {"profile": profile_dict(profile)}

    def settings(self, query: dict[str, str]) -> dict[str, Any]:
        self._session()
        with self.server.reader() as storage:
            live, notes = effective(self.server.config, storage)
            saved = storage.get_settings()
        return {
            "settings": [
                {"key": key, "value": value, "version": saved[key].version if key in saved else 0}
                for key, value in live_values(live).items()
            ],
            "notes": notes,
        }

    def setting_history(self, query: dict[str, str]) -> dict[str, Any]:
        self._session()
        key = query.get("key")
        if key and key not in LIVE:
            raise ApiError(404, f"no setting named {key!r}")
        with self.server.reader() as storage:
            changes = [c for c in storage.get_setting_changes(key) if c.key in LIVE]
        return {"changes": changes}

    def put_setting(self, query: dict[str, str], key: str) -> dict[str, Any]:
        session = self._admin()
        body = self._body()
        if key not in LIVE:
            raise ApiError(404, f"no setting named {key!r}")
        with self.server.writer() as storage:
            if body.get("reset"):
                value = reset_setting(storage, self.server.config, key, by=session.name)
            elif "value" in body:
                value = set_setting(
                    storage, self.server.config, key, body["value"], by=session.name
                )
            else:
                raise ApiError(400, "send value, or reset: true")
        return {"key": key, "value": value}


def serve(
    storage_url: str,
    tz: tzinfo,
    *,
    host: str = "127.0.0.1",
    port: int = 8788,
    sessions: Sessions | None = None,
    config: Config | None = None,
) -> ApiServer:
    """An API server bound to ``host:port``; call ``serve_forever`` to run it."""
    return ApiServer((host, port), storage_url, tz, sessions, config=config)


__all__ = ["ApiError", "ApiServer", "serve", "statement_dict"]
