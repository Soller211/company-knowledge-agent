"""Authentication: password users, API keys and failed-login lockout."""

import hashlib
import hmac
import os
import secrets
import time
from collections.abc import Iterable
from dataclasses import dataclass

import psycopg

GENERAL = "general"
MAX_FAILURES = 5
LOCKOUT_SECONDS = 15 * 60
API_KEY_PREFIX = "cka_"
MAX_TRACKED_USERNAMES = 10_000


@dataclass(frozen=True)
class Principal:
    name: str
    collections: tuple[str, ...]


class AccountLocked(Exception):
    pass


def normalize_collections(collections: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({c.strip().lower() for c in collections if c.strip()} | {GENERAL}))


def _normalize_username(username: str) -> str:
    return username.strip().lower()


def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    return f"scrypt${salt.hex()}${_scrypt(password, salt).hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    return hmac.compare_digest(_scrypt(password, salt).hex(), digest_hex)


# Used for unknown usernames so a failed login takes the same time either way.
_DUMMY_HASH = hash_password("dummy-password-for-timing")


class LoginThrottle:
    """Lock a username for a while after too many failed logins within the lockout window.

    ponytail: in-memory, per process. Counters reset on restart and are not shared between
    replicas; move them to Postgres if the API runs as several replicas.
    """

    def __init__(
        self,
        max_failures=MAX_FAILURES,
        lockout_seconds=LOCKOUT_SECONDS,
        clock=time.monotonic,
        max_tracked=MAX_TRACKED_USERNAMES,
    ):
        self._max_failures = max_failures
        self._lockout_seconds = lockout_seconds
        self._clock = clock
        self._max_tracked = max_tracked
        self._failures: dict[str, tuple[int, float]] = {}  # username -> (count, last failure)
        self._locked_until: dict[str, float] = {}

    def tracked(self) -> int:
        return len(self._failures) + len(self._locked_until)

    def is_locked(self, username: str) -> bool:
        until = self._locked_until.get(username)
        if until is None:
            return False
        if self._clock() >= until:
            del self._locked_until[username]
            self._failures.pop(username, None)
            return False
        return True

    def record_failure(self, username: str) -> None:
        now = self._clock()
        if len(self._failures) >= self._max_tracked:
            self._forget_stale(now)
        count, last = self._failures.get(username, (0, now))
        if now - last > self._lockout_seconds:
            count = 0
        self._failures[username] = (count + 1, now)
        if count + 1 >= self._max_failures:
            self._locked_until[username] = now + self._lockout_seconds

    def _forget_stale(self, now: float) -> None:
        # Failed logins with random usernames must not grow memory forever.
        cutoff = now - self._lockout_seconds
        self._failures = {u: v for u, v in self._failures.items() if v[1] > cutoff}
        self._locked_until = {u: t for u, t in self._locked_until.items() if t > now}

    def record_success(self, username: str) -> None:
        self._failures.pop(username, None)


def create_user(
    conn: psycopg.Connection, username: str, password: str, collections: Iterable[str]
) -> None:
    username = _normalize_username(username)
    cur = conn.execute(
        "INSERT INTO users (username, password_hash, collections) VALUES (%s, %s, %s) "
        "ON CONFLICT (username) DO NOTHING",
        (username, hash_password(password), list(normalize_collections(collections))),
    )
    if cur.rowcount == 0:
        raise ValueError(f"El usuario {username!r} ya existe")


def set_password(conn: psycopg.Connection, username: str, password: str) -> bool:
    cur = conn.execute(
        "UPDATE users SET password_hash = %s WHERE username = %s",
        (hash_password(password), _normalize_username(username)),
    )
    return cur.rowcount == 1


def set_collections(conn: psycopg.Connection, username: str, collections: Iterable[str]) -> bool:
    cur = conn.execute(
        "UPDATE users SET collections = %s WHERE username = %s",
        (list(normalize_collections(collections)), _normalize_username(username)),
    )
    return cur.rowcount == 1


def delete_user(conn: psycopg.Connection, username: str) -> bool:
    cur = conn.execute("DELETE FROM users WHERE username = %s", (_normalize_username(username),))
    return cur.rowcount == 1


def authenticate(
    conn: psycopg.Connection, username: str, password: str, throttle: LoginThrottle
) -> Principal | None:
    username = _normalize_username(username)
    if throttle.is_locked(username):
        raise AccountLocked(username)
    row = conn.execute(
        "SELECT password_hash, collections FROM users WHERE username = %s", (username,)
    ).fetchone()
    password_ok = verify_password(password, row[0] if row else _DUMMY_HASH)
    if row is None or not password_ok:
        throttle.record_failure(username)
        return None
    throttle.record_success(username)
    return Principal(username, normalize_collections(row[1]))


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def create_api_key(conn: psycopg.Connection, name: str, collections: Iterable[str]) -> str:
    raw = API_KEY_PREFIX + secrets.token_urlsafe(32)
    # A revoked key frees its name, so keys can be rotated under the same name.
    conn.execute("DELETE FROM api_keys WHERE name = %s AND revoked_at IS NOT NULL", (name,))
    cur = conn.execute(
        "INSERT INTO api_keys (name, key_hash, collections) VALUES (%s, %s, %s) "
        "ON CONFLICT (name) DO NOTHING",
        (name, _hash_key(raw), list(normalize_collections(collections))),
    )
    if cur.rowcount == 0:
        raise ValueError(f"Ya existe una API key llamada {name!r}")
    return raw


def revoke_api_key(conn: psycopg.Connection, name: str) -> bool:
    cur = conn.execute(
        "UPDATE api_keys SET revoked_at = now() WHERE name = %s AND revoked_at IS NULL", (name,)
    )
    return cur.rowcount == 1


def principal_from_api_key(conn: psycopg.Connection, raw: str) -> Principal | None:
    row = conn.execute(
        "SELECT name, collections FROM api_keys WHERE key_hash = %s AND revoked_at IS NULL",
        (_hash_key(raw),),
    ).fetchone()
    return Principal(f"api:{row[0]}", normalize_collections(row[1])) if row else None


def _session_epoch(password_hash: str) -> str:
    # Changes whenever the password changes (new salt), and when a user is deleted and
    # re-created, so old session cookies stop working in both cases.
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


def start_session(conn: psycopg.Connection, session: dict, username: str) -> None:
    row = conn.execute(
        "SELECT password_hash FROM users WHERE username = %s", (username,)
    ).fetchone()
    session.clear()
    session["user"] = username
    session["epoch"] = _session_epoch(row[0])


def get_principal(request, conn: psycopg.Connection) -> Principal | None:
    """Who is calling and which collections they may read. The one place to add SSO/OIDC."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return principal_from_api_key(conn, header[len("bearer ") :].strip())
    username = request.session.get("user")
    if not username:
        return None
    # Read on every request so permission and password changes apply immediately.
    row = conn.execute(
        "SELECT collections, password_hash FROM users WHERE username = %s", (username,)
    ).fetchone()
    if row is None or request.session.get("epoch") != _session_epoch(row[1]):
        return None
    return Principal(username, normalize_collections(row[0]))
