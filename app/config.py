"""Runtime settings, read from environment variables.

Every path the container uses is set here so it is easy to move the library to
another TrueNAS pool later: only the volume mounts in compose.yaml change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class PrinterProfile:
    """A printer the library knows how to label files for.

    v1 only uses ``match`` to tag sliced files. v2 adds connection details
    (host, serial, access code) to the same record.
    """

    id: str
    name: str
    match: tuple[str, ...]  # case-insensitive substrings of the slicer's printer model
    file_types: tuple[str, ...]  # which sliced kinds this printer can run
    color: str = "#888888"


DEFAULT_PRINTERS = (
    PrinterProfile(
        id="p1s",
        name="Bambu P1S",
        match=("p1s", "c12"),
        file_types=("sliced3mf",),
        color="#00AE42",
    ),
    PrinterProfile(
        id="u1",
        name="Snapmaker U1",
        match=("snapmaker u1", "u1"),
        file_types=("gcode",),
        color="#F2A900",
    ),
)


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser()


@dataclass
class Settings:
    library: Path = field(default_factory=lambda: _env_path("PP_LIBRARY", "/library"))
    data: Path = field(default_factory=lambda: _env_path("PP_DATA", "/data"))
    password: str = field(default_factory=lambda: os.environ.get("PP_PASSWORD", ""))
    scan_interval: int = field(
        default_factory=lambda: int(os.environ.get("PP_SCAN_INTERVAL", "300"))
    )
    printers: tuple[PrinterProfile, ...] = DEFAULT_PRINTERS

    def ensure_dirs(self) -> None:
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "thumbs").mkdir(exist_ok=True)

    def printer_for(self, model: str, kind: str) -> PrinterProfile | None:
        low = model.lower()
        if low:
            for p in self.printers:
                if any(m in low for m in p.match):
                    return p
        return None
