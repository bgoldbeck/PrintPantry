"""Common shape every printer driver returns, so the UI doesn't care which brand it talks to."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# States the UI understands. Drivers map their own words onto these.
IDLE_STATES = {"idle", "complete", "cancelled"}
BUSY_STATES = {"printing", "paused", "starting"}
COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}")
log = logging.getLogger("printpantry.printers")


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
    custom_color: bool = False  # colour set by hand in Print Pantry, not by the printer


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
    light: bool | None = None  # chamber light on/off; None = printer has none we can see
    updated_at: float = 0.0

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class Calibration:
    """A calibration the printer runs by itself (no test print needed)."""

    id: str
    name: str
    about: str  # what it does, shown before starting
    check: str  # what to make sure of first, ticked by the user
    minutes: int  # rough duration
    params: list[dict] = field(default_factory=list)  # {id, label, unit, default, min, max}
    toolhead: bool = False  # asks which toolhead (T1..)


class Printer:
    """Base driver. Subclasses fill in ``poll`` and the actions they support."""

    can_print = False
    can_camera = False
    calibrations: tuple[Calibration, ...] = ()
    poll_seconds = 3.0
    offline_poll_seconds = 10.0

    def __init__(self, profile):
        self.profile = profile
        self.status = Status()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.configured = True
        self.setup_hint = ""
        # Hand-set toolhead colours: {"1": {"color": "#FFD400", "seen": "<what the printer said then>"}}.
        # An override is dropped as soon as the printer reports something different
        # for that toolhead (spool swapped, edited on the screen, new RFID read).
        self.color_file: Path | None = None
        self._colors: dict[str, dict] = {}
        self._raw: dict[int, str] = {}
        # The calibration running now or last run: {id, name, state, started, finished, message}.
        self.calibrating: dict | None = None
        self._calib_lock = threading.Lock()

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
            st = self.poll()
        except Exception as exc:  # noqa: BLE001 - any failure means "can't reach it"
            st = Status(online=False, state="offline", message=_short(exc))
        self._apply_colors(st)
        st.updated_at = time.time()
        self.status = st
        return st

    # -- hand-set toolhead colours
    def load_colors(self, path: Path) -> None:
        self.color_file = path
        try:
            data = json.loads(path.read_text())
            self._colors = {k: v for k, v in data.items() if isinstance(v, dict) and COLOR_RE.fullmatch(v.get("color", ""))}
        except (OSError, ValueError):
            self._colors = {}

    def _save_colors(self) -> None:
        if not self.color_file:
            return
        tmp = self.color_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._colors))
        tmp.replace(self.color_file)

    @staticmethod
    def _sig(t: "Toolhead") -> str:
        return f"{t.type}|{t.color}|{t.vendor}|{t.loaded}"

    def _apply_colors(self, st: Status) -> None:
        if not st.online:
            return
        changed = False
        self._raw = {}
        for t in st.toolheads:
            sig = self._sig(t)
            self._raw[t.index] = sig
            ov = self._colors.get(str(t.index))
            if not ov:
                continue
            if ov.get("seen") != sig:
                log.info("%s T%d changed on the printer; dropping hand-set colour", self.profile.name, t.index + 1)
                del self._colors[str(t.index)]
                changed = True
                continue
            t.color = ov["color"].upper()
            t.custom_color = True
        if changed:
            self._save_colors()

    def set_color(self, index: int, color: str | None) -> None:
        """Set (or with None, clear) the colour shown for a toolhead."""
        if color is None:
            self._colors.pop(str(index), None)
        else:
            if not COLOR_RE.fullmatch(color):
                raise PrinterError("Colour must look like #RRGGBB")
            if index not in self._raw:
                raise PrinterError("The printer hasn't reported that toolhead yet")
            self._colors[str(index)] = {"color": color.upper(), "seen": self._raw[index]}
        self._save_colors()
        self.refresh()

    # -- to override
    def poll(self) -> Status:
        raise NotImplementedError

    def snapshot(self) -> tuple[bytes, float | None]:
        """Latest camera JPEG and the time it was taken (None if unknown)."""
        raise PrinterError("This printer has no camera support yet")

    def start_print(self, path: Path, name: str, tool_map: dict[int, int] | None = None) -> str | None:
        """Send and start a file; returns the name it got on the printer."""
        raise PrinterError("Starting prints isn't supported on this printer yet")

    def action(self, name: str) -> None:
        raise PrinterError(f"'{name}' isn't supported on this printer yet")

    # -- built-in calibrations
    @property
    def calibration_running(self) -> bool:
        return bool(self.calibrating and self.calibrating.get("state") == "running")

    def calibration(self, cid: str) -> Calibration:
        for c in self.calibrations:
            if c.id == cid:
                return c
        raise PrinterError(f"{self.profile.name} has no '{cid}' calibration")

    def start_calibration(self, cid: str, params: dict) -> dict:
        """Check the printer is free, then run the calibration in the background."""
        cal = self.calibration(cid)
        values = {}
        for p in cal.params:
            raw = params.get(p["id"], p["default"])
            try:
                v = float(raw)
            except (TypeError, ValueError):
                raise PrinterError(f"{p['label']} must be a number") from None
            if not p["min"] <= v <= p["max"]:
                raise PrinterError(f"{p['label']} must be between {p['min']} and {p['max']}")
            values[p["id"]] = int(v) if float(v).is_integer() else v
        if cal.toolhead:
            try:
                values["toolhead"] = int(params.get("toolhead") or 1)
            except (TypeError, ValueError):
                raise PrinterError("Pick a toolhead") from None
        with self._calib_lock:
            if self.calibration_running:
                raise PrinterError(f"{self.calibrating['name']} is still running")
            st = self.refresh()
            if not st.online:
                raise PrinterError(f"{self.profile.name} is offline")
            if st.state not in IDLE_STATES:
                raise PrinterError(f"{self.profile.name} is {st.state}, not ready for a calibration")
            script = self.calibration_script(cal, values)
            self.calibrating = {"id": cal.id, "name": cal.name, "state": "running", "started": time.time(),
                                "finished": None, "message": "", "minutes": cal.minutes}
        log.info("%s: starting calibration %s %s", self.profile.name, cal.id, values)
        threading.Thread(target=self._run_calibration, args=(script,), name=f"calib-{self.profile.id}",
                         daemon=True).start()
        return self.calibrating

    def _run_calibration(self, script: str) -> None:
        state = self.calibrating
        try:
            self.run_script(script)
            state.update(state="done", message="Finished")
        except Exception as exc:  # noqa: BLE001 - shown on the Printers page
            log.warning("%s: calibration %s failed: %s", self.profile.name, state["id"], exc)
            state.update(state="failed", message=_short(exc))
        state["finished"] = time.time()

    def calibration_script(self, cal: Calibration, values: dict) -> str:
        raise PrinterError(f"{cal.name} isn't supported on this printer yet")

    def run_script(self, script: str) -> None:
        raise PrinterError("Running commands isn't supported on this printer yet")

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
            "calibrations": [asdict(c) for c in self.calibrations] if self.configured else [],
            "calibrating": self.calibrating,
            "status": self.status.to_json(),
        }


def _short(exc: Exception) -> str:
    text = str(exc) or exc.__class__.__name__
    return text if len(text) < 200 else text[:197] + "..."
