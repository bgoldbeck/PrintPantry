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
