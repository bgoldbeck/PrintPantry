"""Common shape every printer driver returns, so the UI doesn't care which brand it talks to."""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# States the UI understands. Drivers map their own words onto these.
IDLE_STATES = {"idle", "complete", "cancelled"}
BUSY_STATES = {"printing", "paused", "starting"}


class PrinterError(Exception):
    """A printer refused or couldn't be reached. The message is shown to the user."""


@dataclass
class Temp:
    name: str
    current: float | None = None
    target: float | None = None


@dataclass
class Toolhead:
    index: int  # 0-based; shown to people as T1..T4 like the U1 screen
    type: str = ""  # material loaded, e.g. "PLA"
    color: str = ""  # #RRGGBB
    vendor: str = ""
    loaded: bool | None = None  # None = printer doesn't say
    active: bool = False


@dataclass
class Status:
    online: bool = False
    state: str = "offline"  # offline|idle|printing|paused|complete|cancelled|error|starting|not_set_up
    message: str = ""  # printer's own error or status text
    file: str = ""
    progress: float | None = None  # 0..1
    print_seconds: float | None = None
    eta_seconds: float | None = None
    layer: int | None = None
    total_layers: int | None = None
    temps: list[Temp] = field(default_factory=list)
    toolheads: list[Toolhead] = field(default_factory=list)
    updated_at: float = 0.0

    def to_json(self) -> dict:
        return asdict(self)


class Printer:
    """Base driver. Subclasses fill in ``poll`` and the actions they support."""

    can_print = False
    can_camera = False
    poll_seconds = 3.0
    offline_poll_seconds = 10.0

    def __init__(self, profile):
        self.profile = profile
        self.status = Status()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.configured = True
        self.setup_hint = ""

    # -- lifecycle
    def start(self) -> None:
        if not self.configured or self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name=f"printer-{self.profile.id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.refresh()
            wait = self.poll_seconds if self.status.online else self.offline_poll_seconds
            self._stop.wait(wait)

    def refresh(self) -> Status:
        try:
            self.status = self.poll()
        except Exception as exc:  # noqa: BLE001 - any failure means "can't reach it"
            self.status = Status(online=False, state="offline", message=_short(exc))
        self.status.updated_at = time.time()
        return self.status

    # -- to override
    def poll(self) -> Status:
        raise NotImplementedError

    def snapshot(self) -> tuple[bytes, float | None]:
        """Latest camera JPEG and the time it was taken (None if unknown)."""
        raise PrinterError("This printer has no camera support yet")

    def start_print(self, path: Path, name: str) -> None:
        raise PrinterError("Starting prints isn't supported on this printer yet")

    def action(self, name: str) -> None:
        raise PrinterError(f"'{name}' isn't supported on this printer yet")

    def to_json(self) -> dict:
        p = self.profile
        return {
            "id": p.id,
            "name": p.name,
            "color": p.color,
            "file_types": list(p.file_types),
            "configured": self.configured,
            "setup_hint": self.setup_hint,
            "can_print": self.can_print and self.configured,
            "can_camera": self.can_camera and self.configured,
            "status": self.status.to_json(),
        }


def _short(exc: Exception) -> str:
    text = str(exc) or exc.__class__.__name__
    return text if len(text) < 200 else text[:197] + "..."
