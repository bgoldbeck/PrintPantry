"""Optional single-password login.

Set ``PP_PASSWORD`` to turn it on; ``PP_SESSION_DAYS`` (default 365) sets how
long a login lasts. The session is an HMAC-signed cookie, keyed
by a random secret kept in the data volume, so restarts don't log people out.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

COOKIE = "pp_session"
# How long a login lasts. Long by default: this is a LAN-only app.
MAX_AGE = 60 * 60 * 24 * int(os.environ.get("PP_SESSION_DAYS", "365"))


class Auth:
    def __init__(self, password: str, data_dir: Path):
        self.password = password
        secret_file = data_dir / "session.secret"
        if not secret_file.exists():
            secret_file.write_text(secrets.token_hex(32))
            secret_file.chmod(0o600)
        self.secret = secret_file.read_text().strip().encode()

    @property
    def enabled(self) -> bool:
        return bool(self.password)

    def check_password(self, attempt: str) -> bool:
        return hmac.compare_digest(attempt.encode(), self.password.encode())

    def _sign(self, payload: str) -> str:
        return hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()

    def make_cookie(self) -> str:
        expires = str(int(time.time()) + MAX_AGE)
        # Bind to the password so changing it logs everyone out.
        pw = hashlib.sha256(self.password.encode()).hexdigest()[:16]
        return f"{expires}.{self._sign(expires + pw)}"

    def valid(self, cookie: str | None) -> bool:
        if not self.enabled:
            return True
        if not cookie or "." not in cookie:
            return False
        expires, sig = cookie.split(".", 1)
        if not expires.isdigit() or int(expires) < time.time():
            return False
        pw = hashlib.sha256(self.password.encode()).hexdigest()[:16]
        return hmac.compare_digest(sig, self._sign(expires + pw))

    # -- one-off download links for desktop slicers, which have no login cookie
    def link_token(self, file_id: str, ttl: int = 15 * 60) -> str:
        expires = str(int(time.time()) + ttl)
        return f"{file_id}.{expires}.{self._sign('dl:' + file_id + expires)[:32]}"

    def check_link(self, token: str) -> str | None:
        """Return the file id a link token is for, or None if bad or expired."""
        parts = token.split(".")
        if len(parts) != 3 or not parts[1].isdigit() or int(parts[1]) < time.time():
            return None
        file_id, expires, sig = parts
        if not hmac.compare_digest(sig, self._sign("dl:" + file_id + expires)[:32]):
            return None
        return file_id


# -- exposure to the internet through a TLS proxy ------------------------------
import ipaddress  # noqa: E402
import threading  # noqa: E402

# Peers whose X-Real-IP / X-Forwarded-Proto we believe: the HTTPS proxy
# container reaches the app from a Docker bridge network. LAN browsers come
# from 192.168.x.x, so they can't spoof their address to dodge the rate limit.
TRUSTED_PROXIES = [
    ipaddress.ip_network(n.strip())
    for n in os.environ.get("PP_TRUSTED_PROXIES", "172.16.0.0/12,127.0.0.1/32").split(",")
    if n.strip()
]


def _ip(text: str | None):
    try:
        return ipaddress.ip_address((text or "").strip())
    except ValueError:
        return None


def from_trusted_proxy(peer: str | None) -> bool:
    ip = _ip(peer)
    return bool(ip) and any(ip in n for n in TRUSTED_PROXIES)


def client_ip(peer: str | None, headers) -> str:
    """The browser's address: the proxy's X-Real-IP (or last X-Forwarded-For hop)
    when the request came through the trusted proxy, else the socket peer."""
    if from_trusted_proxy(peer):
        real = _ip(headers.get("x-real-ip"))
        if real:
            return str(real)
        hops = [h for h in (headers.get("x-forwarded-for") or "").split(",") if h.strip()]
        if hops and _ip(hops[-1]):
            return str(_ip(hops[-1]))
    return peer or "?"


def is_https(scheme: str, peer: str | None, headers) -> bool:
    if scheme == "https":
        return True
    return from_trusted_proxy(peer) and headers.get("x-forwarded-proto", "").lower() == "https"


def is_private(ip: str) -> bool:
    a = _ip(ip)
    return bool(a) and (a.is_private or a.is_loopback)


class LoginLimiter:
    """Slows password guessing from the internet.

    Each address gets PER_IP wrong passwords per WINDOW seconds, then is locked
    out until the oldest one ages out. If wrong passwords from all public
    addresses together reach GLOBAL in a window (a spread-out attack), public
    addresses are locked out for that window; home-network addresses still get in.
    """

    PER_IP = int(os.environ.get("PP_LOGIN_MAX_FAILS", "5"))
    GLOBAL = int(os.environ.get("PP_LOGIN_MAX_FAILS_ALL", "30"))
    WINDOW = 15 * 60

    def __init__(self):
        self._fails: dict[str, list[float]] = {}
        self._public: list[float] = []
        self._lock = threading.Lock()

    def _trim(self, now: float):
        cut = now - self.WINDOW
        for ip in list(self._fails):
            self._fails[ip] = [t for t in self._fails[ip] if t > cut]
            if not self._fails[ip]:
                del self._fails[ip]
        self._public = [t for t in self._public if t > cut]

    def retry_after(self, ip: str, now: float | None = None) -> int:
        """Seconds until this address may try again, 0 if it may now."""
        now = now or time.time()
        with self._lock:
            self._trim(now)
            waits = []
            fails = self._fails.get(ip, [])
            if len(fails) >= self.PER_IP:
                waits.append(fails[-self.PER_IP] + self.WINDOW - now)
            if not is_private(ip) and len(self._public) >= self.GLOBAL:
                waits.append(self._public[-self.GLOBAL] + self.WINDOW - now)
            return int(max(waits)) + 1 if waits else 0

    def failed(self, ip: str, now: float | None = None):
        now = now or time.time()
        with self._lock:
            self._fails.setdefault(ip, []).append(now)
            if not is_private(ip):
                self._public.append(now)

    def succeeded(self, ip: str):
        with self._lock:
            self._fails.pop(ip, None)
