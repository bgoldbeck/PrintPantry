"""Small SQLite store in the data volume.

Holds the parse cache (so rescans only re-read changed files) and user settings
such as category icons. v2 adds printers and print history here.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS file_info (
    relpath TEXT PRIMARY KEY,
    mtime   REAL NOT NULL,
    size    INTEGER NOT NULL,
    info    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: Path):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._db.commit()

    # -- parse cache
    def cached_info(self, relpath: str, mtime: float, size: int) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT mtime, size, info FROM file_info WHERE relpath = ?", (relpath,)
            ).fetchone()
        if row and row[0] == mtime and row[1] == size:
            return json.loads(row[2])
        return None

    def put_info(self, relpath: str, mtime: float, size: int, info: dict) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO file_info VALUES (?, ?, ?, ?)",
                (relpath, mtime, size, json.dumps(info)),
            )
            self._db.commit()

    def prune_info(self, keep: set[str]) -> None:
        with self._lock:
            rows = [r[0] for r in self._db.execute("SELECT relpath FROM file_info")]
            gone = [(r,) for r in rows if r not in keep]
            if gone:
                self._db.executemany("DELETE FROM file_info WHERE relpath = ?", gone)
                self._db.commit()

    # -- settings
    def get(self, key: str, default=None):
        with self._lock:
            row = self._db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value))
            )
            self._db.commit()
