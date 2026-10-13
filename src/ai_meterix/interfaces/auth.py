"""Accounts and logins for the localhost API (record 0013).

Two accounts, ``user`` (read) and ``admin`` (read and write), start with their
name as password and must change it at first login. Passwords are stored only
as salted scrypt hashes (standard library). A login returns a random token,
kept in memory: restarting the server logs everyone out.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass

from ai_meterix.core.model import Role, User
from ai_meterix.storage.base import StorageReader, StorageWriter

ACCOUNTS = {"user": Role.USER, "admin": Role.ADMIN}
MIN_PASSWORD = 8
TOKEN_TTL_S = 12 * 3600
_N, _R, _P = 2**14, 8, 1  # about 16 MB and a few tens of ms per hash


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def hash_password(password: str, salt: bytes | None = None) -> str:
    """``scrypt$n$r$p$salt$hash``, all base64; never the password itself."""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def check_new_password(name: str, password: str) -> None:
    """Raise ``ValueError`` when ``password`` is too weak to replace the default."""
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"use at least {MIN_PASSWORD} characters")
    if password.lower() in {name.lower(), *ACCOUNTS}:
        raise ValueError("use something other than an account name")


def ensure_accounts(storage: StorageWriter) -> list[str]:
    """Create the missing default accounts (password = name, change required)."""
    created = []
    for name, role in ACCOUNTS.items():
        if storage.get_user(name) is None:
            storage.save_user(User(name, role, hash_password(name), must_change=True))
            created.append(name)
    return created


def set_password(storage: StorageWriter, name: str, password: str) -> User:
    user = storage.get_user(name)
    if user is None:
        raise ValueError(f"no account named {name!r}")
    check_new_password(name, password)
    changed = User(name, user.role, hash_password(password), must_change=False)
    storage.save_user(changed)
    return changed


def defaults_unchanged(storage: StorageReader) -> list[str]:
    """Accounts that still have to change their first password."""
    return [n for n in ACCOUNTS if (u := storage.get_user(n)) is None or u.must_change]


@dataclass(frozen=True, slots=True)
class Session:
    name: str
    role: Role
    must_change: bool
    expires: float


class Sessions:
    """Login tokens held in memory; each lasts ``ttl_s`` seconds."""

    def __init__(self, ttl_s: float = TOKEN_TTL_S) -> None:
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._tokens: dict[str, Session] = {}

    def login(self, storage: StorageReader, name: str, password: str) -> tuple[str, Session] | None:
        user = storage.get_user(name)
        if user is None or not verify_password(password, user.password_hash):
            return None
        token = secrets.token_urlsafe(32)
        session = Session(user.name, user.role, user.must_change, time.monotonic() + self.ttl_s)
        with self._lock:
            self._tokens[token] = session
        return token, session

    def get(self, token: str) -> Session | None:
        with self._lock:
            session = self._tokens.get(token)
            if session is not None and session.expires < time.monotonic():
                del self._tokens[token]
                return None
            return session

    def changed_password(self, name: str) -> None:
        """Clear the change-required flag for the user's open sessions."""
        with self._lock:
            for token, s in self._tokens.items():
                if s.name == name:
                    self._tokens[token] = Session(s.name, s.role, False, s.expires)

    def logout(self, token: str) -> None:
        with self._lock:
            self._tokens.pop(token, None)

    def drop_user(self, name: str, keep: str | None = None) -> None:
        """End the user's other sessions (after a password change)."""
        with self._lock:
            for token in [t for t, s in self._tokens.items() if s.name == name and t != keep]:
                del self._tokens[token]


__all__ = [
    "ACCOUNTS",
    "Session",
    "Sessions",
    "check_new_password",
    "defaults_unchanged",
    "ensure_accounts",
    "hash_password",
    "set_password",
    "verify_password",
]
