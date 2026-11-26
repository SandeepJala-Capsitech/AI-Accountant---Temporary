"""Sign-in with the one username and password set in the environment: the check, a signed session token kept in an
HttpOnly cookie, and a throttle on wrong attempts. Standard library only."""
from __future__ import annotations

import hashlib
import hmac
import threading
import time
from collections import deque
from typing import Callable, Optional

from .config import Settings

COOKIE = "ledgersync_session"
# What a browser that has not signed in may still ask for: the host's health check, and signing in and out.
PUBLIC_PATHS = frozenset({"/api/health", "/api/auth/login", "/api/auth/logout", "/api/auth/session"})
MIN_SECRET = 32


def check_settings(settings: Settings) -> None:
    """Refuses settings that would leave the API open by mistake, or sign sessions with a guessable key."""
    if settings.auth_username and not settings.auth_password:
        raise ValueError("LEDGERSYNC_AUTH_USERNAME is set but LEDGERSYNC_AUTH_PASSWORD is not: set both, or neither.")
    if settings.auth_password and not settings.auth_username:
        raise ValueError("LEDGERSYNC_AUTH_PASSWORD is set but LEDGERSYNC_AUTH_USERNAME is not: set both, or neither.")
    if settings.auth_required and len(settings.auth_secret) < MIN_SECRET:
        raise ValueError(f"Set LEDGERSYNC_AUTH_SECRET to at least {MIN_SECRET} random characters: "
                         'python -c "import secrets; print(secrets.token_urlsafe(48))"')


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def credentials_match(settings: Settings, username: str, password: str) -> bool:
    """Whether this is the pair that was set. Both are compared, in constant time, whatever the first gives."""
    if not settings.auth_required:
        return False
    user_ok = _same(username, settings.auth_username)
    password_ok = _same(password, settings.auth_password)
    return user_ok and password_ok


def _signature(settings: Settings, expires: int) -> str:
    # The username and password are in the message, so changing either signs everyone out.
    message = f"{settings.auth_username}\n{settings.auth_password}\n{expires}".encode("utf-8")
    return hmac.new(settings.auth_secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def make_token(settings: Settings, now: float) -> str:
    """A session that lasts `session_days` from now: "<expires>.<signature>"."""
    expires = int(now) + settings.session_days * 86400
    return f"{expires}.{_signature(settings, expires)}"


def token_valid(settings: Settings, token: Optional[str], now: float) -> bool:
    """Whether the token was signed here, for the current username and password, and has not expired."""
    if not token or "." not in token:
        return False
    expires_text, signature = token.split(".", 1)
    try:
        expires = int(expires_text)
    except ValueError:
        return False
    return _same(signature, _signature(settings, expires)) and now < expires


class LoginThrottle:
    """After `limit` wrong attempts within `window` seconds, no sign-in is tried until the oldest is older than that.
    Kept for everyone at once: behind Vercel's proxy every request comes from Vercel's addresses."""

    def __init__(self, limit: int = 10, window: float = 900.0, clock: Callable[[], float] = time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self._failures: deque[float] = deque()
        self._lock = threading.Lock()

    def _forget_old(self) -> None:
        cutoff = self.clock() - self.window
        while self._failures and self._failures[0] <= cutoff:
            self._failures.popleft()

    def allowed(self) -> bool:
        with self._lock:
            self._forget_old()
            return len(self._failures) < self.limit

    def failed(self) -> None:
        with self._lock:
            self._forget_old()
            self._failures.append(self.clock())

    def succeeded(self) -> None:
        with self._lock:
            self._failures.clear()
