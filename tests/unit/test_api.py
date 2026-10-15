"""The localhost API: login, roles, first-password change and the same numbers as the CLI."""

from __future__ import annotations

import http.client
import json
import re
import threading
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from test_engine import H10, build

from ai_meterix.interfaces.api import ApiServer, serve, statement_dict
from ai_meterix.interfaces.auth import (
    Sessions,
    check_new_password,
    ensure_accounts,
    hash_password,
    verify_password,
)
from ai_meterix.pricing.engine import CostEngine
from ai_meterix.storage import open_reader

UTC = timezone.utc


class Client:
    def __init__(self, server: ApiServer) -> None:
        self.port = int(server.server_address[1])
        self.token = ""

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        conn.request(method, path, None if body is None else json.dumps(body), headers)
        response = conn.getresponse()
        result = response.status, json.loads(response.read())
        conn.close()
        return result

    def login(self, name: str, password: str) -> dict[str, Any]:
        status, body = self.call("POST", "/api/login", {"name": name, "password": password})
        assert status == 200, body
        self.token = body["token"]
        return body


@pytest.fixture
def api(tmp_path: Path) -> Iterator[ApiServer]:
    storage = build(tmp_path)
    ensure_accounts(storage)
    storage.close()
    server = serve(f"sqlite:///{tmp_path / 'meter.db'}", UTC, port=0, sessions=Sessions())
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def ready(server: ApiServer, name: str) -> Client:
    """A client logged in as ``name`` after the forced first-password change."""
    c = Client(server)
    c.login(name, name)
    status, _ = c.call("POST", "/api/password", {"old": name, "password": f"{name}-secret-1"})
    assert status == 200
    return c


def test_passwords_are_salted_scrypt_hashes() -> None:
    stored = hash_password("correct horse")
    assert stored.startswith("scrypt$") and "correct horse" not in stored
    assert stored != hash_password("correct horse")  # salted
    assert verify_password("correct horse", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("x", "not-a-hash")
    with pytest.raises(ValueError):
        check_new_password("admin", "short")
    with pytest.raises(ValueError):
        check_new_password("admin", "ADMIN")


def test_login_and_the_forced_first_password_change(api: ApiServer) -> None:
    c = Client(api)
    assert c.call("GET", "/api/usage")[0] == 401
    assert c.call("POST", "/api/login", {"name": "admin", "password": "nope"})[0] == 401
    assert c.login("admin", "admin")["must_change"] is True
    status, body = c.call("GET", "/api/prices")
    assert status == 403 and "change your password" in body["error"]
    assert c.call("POST", "/api/password", {"old": "wrong", "password": "long-enough-1"})[0] == 403
    assert c.call("POST", "/api/password", {"old": "admin", "password": "admin"})[0] == 400
    assert c.call("POST", "/api/password", {"old": "admin", "password": "long-enough-1"})[0] == 200
    assert c.call("GET", "/api/prices")[0] == 200
    # the old password no longer works, the new one does
    other = Client(api)
    assert other.call("POST", "/api/login", {"name": "admin", "password": "admin"})[0] == 401
    assert other.login("admin", "long-enough-1")["must_change"] is False
    assert c.call("POST", "/api/logout")[0] == 200
    assert c.call("GET", "/api/prices")[0] == 401


def test_the_user_account_reads_but_cannot_write(api: ApiServer) -> None:
    user = ready(api, "user")
    assert user.call("GET", "/api/profiles")[0] == 200
    status, body = user.call("PUT", "/api/prices/gpt-x", {"input": "1"})
    assert status == 403 and "admin" in body["error"]
    assert user.call("POST", "/api/prices/apply-reference", {"dry_run": True})[0] == 403
    assert user.call("PUT", "/api/profiles/mac", {"rate": "0.5"})[0] == 403
    assert (
        user.call("POST", "/api/password", {"name": "admin", "password": "takeover-123"})[0] == 403
    )


def test_the_admin_edits_prices_and_profiles(api: ApiServer) -> None:
    admin = ready(api, "admin")
    status, body = admin.call("PUT", "/api/prices/gpt-x", {"input": "2", "output": "8"})
    assert status == 200 and body["price"]["input_per_m"] == "2"
    assert admin.call("PUT", "/api/prices/gpt-x", {"input": "abc"})[0] == 400
    status, body = admin.call("GET", "/api/prices/history?model=gpt-x")
    assert {c["by"] for c in body["changes"]} == {"admin"}
    status, body = admin.call("POST", "/api/prices/apply-reference", {"dry_run": True})
    assert status == 200 and body["dry_run"] and body["results"]
    assert admin.call("PUT", "/api/profiles/mac", {"rate": "cheap"})[0] == 400
    assert admin.call("PUT", "/api/profiles/mac", {"currency": "EUR"})[0] == 400
    status, body = admin.call("PUT", "/api/profiles/mac", {"rate": "0.40"})
    assert status == 200 and body["profile"]["currency"] == "USD"
    assert body["profile"]["rate_per_kwh"] == "0.40"
    assert body["profile"]["version"] == 2
    assert admin.call("PUT", "/api/profiles/nope", {"rate": "1"})[0] == 404
    # the admin can reset the user account's password
    assert (
        admin.call("POST", "/api/password", {"name": "user", "password": "fresh-pass-1"})[0] == 200
    )
    Client(api).login("user", "fresh-pass-1")


def test_the_api_returns_the_same_numbers_as_the_cli(api: ApiServer, tmp_path: Path) -> None:
    user = ready(api, "user")
    status, body = user.call("GET", "/api/usage?from=2026-10-05&to=2026-10-05&by=model")
    assert status == 200
    storage = open_reader(f"sqlite:///{tmp_path / 'meter.db'}")
    try:
        start = datetime(2026, 10, 5, tzinfo=UTC)
        end = datetime(2026, 10, 6, tzinfo=UTC)
        expected = statement_dict(
            CostEngine(storage, UTC).statement(start, end, now=datetime.now(UTC)), "model"
        )
    finally:
        storage.close()
    assert body == json.loads(json.dumps(expected))
    claude = next(line for line in body["lines"] if line["model"] == "claude-x")
    assert claude["input_tokens"] == 1000 and claude["token_cost"].startswith("0.0111")
    assert H10.date().isoformat() == body["lines"][0]["day"]
    assert user.call("GET", "/api/usage?by=week")[0] == 400
    assert user.call("GET", "/api/usage?from=2026-13-01")[0] == 400
    assert user.call("GET", "/api/usage?last=2h")[0] == 200


def test_health_and_unknown_paths(api: ApiServer) -> None:
    c = Client(api)
    status, body = c.call("GET", "/api/health")
    assert status == 200 and body["ok"] is True
    assert c.call("GET", "/api/nothing")[0] == 404
    assert c.call("POST", "/api/usage")[0] == 405


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10"])
def test_the_api_listens_on_this_machine_only(tmp_path: Path, host: str) -> None:
    with pytest.raises(ValueError, match=r"127\.0\.0\.1 only"):
        serve(f"sqlite:///{tmp_path / 'm.db'}", UTC, host=host, port=0)


def test_serve_and_password_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from ai_meterix.interfaces.cli import main

    url = f"sqlite:///{tmp_path / 'm.db'}"
    assert main(["serve", "--url", url, "--port", "0", "--for", "0.2s"]) == 0
    answers = iter(["new-admin-pass", "new-admin-pass", "short", "short", "a-long-one", "other"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    assert main(["password", "admin", "--url", url]) == 0
    assert main(["password", "admin", "--url", url]) == 2  # too short
    assert main(["password", "admin", "--url", url]) == 2  # the two differ
    assert "differ" in capsys.readouterr().err
    storage = open_reader(url)
    try:
        admin = storage.get_user("admin")
        assert admin is not None and not admin.must_change
        assert verify_password("new-admin-pass", admin.password_hash)
    finally:
        storage.close()


def test_settings_are_read_by_all_and_changed_by_the_admin(api: ApiServer) -> None:
    user, admin = ready(api, "user"), ready(api, "admin")
    status, body = user.call("GET", "/api/config")
    assert status == 200
    values = {s["key"]: s["value"] for s in body["settings"]}
    assert values["agent.interval_s"] == 10.0
    assert user.call("PUT", "/api/config/agent.interval_s", {"value": 5})[0] == 403
    assert admin.call("PUT", "/api/config/agent.interval_s", {"value": 0})[0] == 400
    assert admin.call("PUT", "/api/config/storage.url", {"value": "x"})[0] == 404
    assert admin.call("PUT", "/api/config/agent.interval_s", {})[0] == 400
    status, body = admin.call("PUT", "/api/config/agent.interval_s", {"value": 5})
    assert status == 200 and body["value"] == 5
    status, body = user.call("GET", "/api/config/history?key=agent.interval_s")
    assert [c["by"] for c in body["changes"]][-1] == "admin"
    assert user.call("GET", "/api/config/history?key=nope")[0] == 404
    status, body = admin.call("PUT", "/api/config/agent.interval_s", {"reset": True})
    assert status == 200 and body["value"] == 10.0


def raw(server: ApiServer, method: str, path: str, body: Any = None, headers: Any = None) -> Any:
    conn = http.client.HTTPConnection("127.0.0.1", int(server.server_address[1]), timeout=10)
    conn.request(method, path, None if body is None else json.dumps(body), headers or {})
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response, data


def test_the_dashboard_page_is_self_contained(api: ApiServer) -> None:
    response, data = raw(api, "GET", "/")
    page = data.decode()
    assert response.status == 200 and response.getheader("Content-Type").startswith("text/html")
    policy = response.getheader("Content-Security-Policy")
    assert "default-src 'none'" in policy and "frame-ancestors 'none'" in policy
    nonce = policy.split("'nonce-")[1].split("'")[0]
    assert page.count(f'nonce="{nonce}"') == 2 and "{{nonce}}" not in page
    # no external requests: nothing loads from a URL (the help may mention URLs as text)
    assert not re.search(r"""(src|href|action)\s*=\s*["']?(https?:)?//""", page)
    assert "url(" not in page and "@import" not in page
    assert raw(api, "GET", "/")[0].getheader("Content-Security-Policy") != policy  # fresh nonce


def test_every_help_link_has_a_topic(api: ApiServer) -> None:
    page = raw(api, "GET", "/")[1].decode()
    links = set(re.findall(r'href="#(h-[a-z]+)"', page))
    topics = set(re.findall(r'id="(h-[a-z]+)"', page))
    assert links and links <= topics and topics <= links  # no dead link, no unlisted topic


def test_the_help_lists_every_command_and_option(api: ApiServer) -> None:
    import argparse

    from ai_meterix.interfaces.cli import build_parser

    page = raw(api, "GET", "/")[1].decode()
    reference = page[page.index('id="h-commands"') :]
    (commands,) = [a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction)]
    for name, parser in commands.choices.items():
        assert f'id="c-{name}"' in reference, f"no help for aimeterix {name}"
        block = reference[reference.index(f'id="c-{name}"') :].split('class="cmd"')[0]
        for action in parser._actions:
            for option in action.option_strings:
                if option.startswith("--") and option != "--help":
                    assert option in block, f"aimeterix {name} {option} is not in the help"
            for choice in action.choices or () if not action.option_strings else ():
                assert choice in block, f"aimeterix {name} {choice} is not in the help"


def test_the_browser_logs_in_with_a_cookie(api: ApiServer) -> None:
    ready(api, "user")  # first password changed
    response, data = raw(
        api, "POST", "/api/login", {"name": "user", "password": "user-secret-1", "cookie": True}
    )
    assert response.status == 200 and "token" not in json.loads(data)
    cookie = response.getheader("Set-Cookie")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    jar = {"Cookie": cookie.split(";")[0]}
    response, data = raw(api, "GET", "/api/me", headers=jar)
    assert response.status == 200 and json.loads(data)["name"] == "user"
    # a change sent with the cookie alone (as another site could) is refused
    response, _ = raw(api, "POST", "/api/logout", headers=jar)
    assert response.status == 403
    response, _ = raw(api, "POST", "/api/logout", headers={**jar, "X-AIMeterix": "1"})
    assert response.status == 200 and "Max-Age=0" in response.getheader("Set-Cookie")
    assert raw(api, "GET", "/api/me", headers=jar)[0].status == 401


def test_the_dashboard_endpoint_matches_the_cli(api: ApiServer, tmp_path: Path) -> None:
    user = ready(api, "user")
    status, body = user.call("GET", "/api/dashboard?from=2026-10-05&to=2026-10-05")
    assert status == 200
    usage = user.call("GET", "/api/usage?from=2026-10-05&to=2026-10-05&by=model")[1]
    assert body["models"] == usage  # the same computation as `aimeterix usage`
    (cov,) = body["coverage"]
    assert cov == {"day": "2026-10-05", "measured_h": 1.0, "hours": 24.0}
    assert body["health"]["processes"][0]["name"] == "agent"
    assert "agent.interval_s" in body["settings"]
    assert {p["host"] for p in body["profiles"]} == {"mac"}
    assert user.call("GET", "/api/dashboard?from=2026-10-06&to=2026-10-05")[0] == 400
    assert user.call("GET", "/api/dashboard?from=2024-01-01&to=2026-10-05")[0] == 400
    status, body = user.call("GET", "/api/dashboard")  # default: the last 7 days
    assert status == 200 and len(body["coverage"]) == 7
