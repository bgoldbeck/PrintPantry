"""Optional single-password login.

Set ``PP_PASSWORD`` to turn it on. The session is an HMAC-signed cookie, keyed
by a random secret kept in the data volume, so restarts don't log people out.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from pathlib import Path

COOKIE = "pp_session"
MAX_AGE = 60 * 60 * 24 * 30  # 30 days


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
