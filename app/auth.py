"""Accounts, password hashing, signed session cookies, and simple rate limits.

- Users live in their own `users` table (separate metadata from the decision record, so erasing the
  record never deletes accounts). Roles: manager (everything) and reviewer (read, verify, ask).
- Passwords are hashed with scrypt from the standard library, with a random salt per user.
- Sessions are a signed cookie (HMAC-SHA256 with SESSION_SECRET): username, expiry, and a fingerprint
  of the password hash, so changing a password signs out existing sessions.
- In-memory limits: sign-in lockout after repeated failures, and a per-user cap on model-backed actions.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import sqlalchemy as sa

log = logging.getLogger("closure_call")

ROLES = ("manager", "reviewer")
SESSION_COOKIE = "cc_session"
SESSION_DAYS = 7

auth_meta = sa.MetaData()
users = sa.Table(
    "users", auth_meta,
    sa.Column("username", sa.String(64), primary_key=True),
    sa.Column("display_name", sa.String(128), nullable=False),
    sa.Column("role", sa.String(16), nullable=False),
    sa.Column("password_hash", sa.String(256), nullable=False),
    sa.Column("disabled", sa.Boolean, nullable=False, default=False),
    sa.Column("created_at", sa.String(40), nullable=False),
)


@dataclass(frozen=True)
class User:
    username: str
    display_name: str
    role: str
    fingerprint: str  # changes when the password changes

    @property
    def is_manager(self) -> bool:
        return self.role == "manager"

    @property
    def actor(self) -> str:
        """How this person appears in the decision record."""
        return f"{self.display_name} (@{self.username}, {self.role})"


class AuthError(ValueError):
    pass


# ---------- passwords ----------

_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise AuthError("use at least 10 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32, **_SCRYPT)
    b64 = lambda b: base64.b64encode(b).decode("ascii")  # noqa: E731
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        got = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt), dklen=32,
                             n=int(n), r=int(r), p=int(p))
        return hmac.compare_digest(got, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


def _fingerprint(password_hash: str) -> str:
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()[:12]


# ---------- accounts ----------

class Accounts:
    def __init__(self, engine: sa.Engine):
        self.engine = engine
        auth_meta.create_all(engine)

    def _user(self, row) -> User:
        return User(row.username, row.display_name, row.role, _fingerprint(row.password_hash))

    def add(self, username: str, display_name: str, role: str, password: str):
        username = username.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,31}", username):
            raise AuthError("username: 2-32 lowercase letters, numbers, dots, dashes or underscores")
        if role not in ROLES:
            raise AuthError(f"role must be one of {ROLES}")
        with self.engine.begin() as c:
            if c.execute(sa.select(users.c.username).where(users.c.username == username)).first():
                raise AuthError(f"user {username} already exists")
            c.execute(users.insert().values(username=username, display_name=display_name.strip(), role=role,
                                            password_hash=hash_password(password), disabled=False,
                                            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds")))

    def set_password(self, username: str, password: str):
        with self.engine.begin() as c:
            n = c.execute(users.update().where(users.c.username == username)
                          .values(password_hash=hash_password(password))).rowcount
        if not n:
            raise AuthError(f"no user {username}")

    def set_disabled(self, username: str, disabled: bool):
        with self.engine.begin() as c:
            if not c.execute(users.update().where(users.c.username == username).values(disabled=disabled)).rowcount:
                raise AuthError(f"no user {username}")

    def list(self) -> list[dict]:
        with self.engine.connect() as c:
            return [dict(username=r.username, display_name=r.display_name, role=r.role, disabled=r.disabled,
                         created_at=r.created_at) for r in c.execute(sa.select(users).order_by(users.c.username))]

    def get(self, username: str) -> User | None:
        with self.engine.connect() as c:
            r = c.execute(sa.select(users).where(users.c.username == username, users.c.disabled.is_(False))).first()
        return self._user(r) if r else None

    def authenticate(self, username: str, password: str) -> User | None:
        with self.engine.connect() as c:
            r = c.execute(sa.select(users).where(users.c.username == username.strip().lower(),
                                                 users.c.disabled.is_(False))).first()
        if r is None:
            verify_password(password, hash_password("x" * 16))  # same cost whether or not the user exists
            return None
        return self._user(r) if verify_password(password, r.password_hash) else None

    def count(self) -> int:
        with self.engine.connect() as c:
            return c.execute(sa.select(sa.func.count()).select_from(users)).scalar()


# ---------- sessions ----------

def _secret() -> bytes:
    s = os.environ.get("SESSION_SECRET")
    if not s:
        s = secrets.token_urlsafe(32)
        os.environ["SESSION_SECRET"] = s
        log.warning("SESSION_SECRET not set: generated one, so sessions end when the app restarts")
    return s.encode("utf-8")


def make_session(user: User, days: int = SESSION_DAYS) -> str:
    body = json.dumps({"u": user.username, "f": user.fingerprint, "exp": int(time.time()) + days * 86400},
                      separators=(",", ":")).encode("utf-8")
    b = base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")
    sig = hmac.new(_secret(), b.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{b}.{sig}"


def read_session(token: str | None, accounts: Accounts) -> User | None:
    if not token or "." not in token:
        return None
    b, sig = token.rsplit(".", 1)
    good = hmac.new(_secret(), b.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)))
    except (ValueError, TypeError):
        return None
    if data.get("exp", 0) < time.time():
        return None
    user = accounts.get(data.get("u", ""))
    if user is None or not hmac.compare_digest(user.fingerprint, data.get("f", "")):
        return None  # disabled, deleted, or password changed since this session began
    return user


# ---------- rate limits ----------

class RateLimit:
    """At most `limit` events per key in a sliding window of `window_s` seconds (in memory)."""

    def __init__(self, limit: int, window_s: int):
        self.limit, self.window = limit, window_s
        self._events: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> bool:
        """Records an event; returns False if the key is over the limit."""
        now = time.time()
        with self._lock:
            ev = [t for t in self._events.get(key, []) if now - t < self.window]
            if len(ev) >= self.limit:
                self._events[key] = ev
                return False
            ev.append(now)
            self._events[key] = ev
            return True

    def blocked(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            return len([t for t in self._events.get(key, []) if now - t < self.window]) >= self.limit

    def clear(self, key: str):
        with self._lock:
            self._events.pop(key, None)


failed_logins = RateLimit(limit=5, window_s=300)  # 5 failures per username or IP, then wait 5 minutes
model_actions = RateLimit(limit=8, window_s=600)  # checks and questions per user per 10 minutes
