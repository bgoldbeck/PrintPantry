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
# "failed" is a Bambu print that stopped with an error; the printer is free again.
IDLE_STATES = {"idle", "complete", "cancelled", "failed"}
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
    label: str = ""  # shown name when not "T<index+1>", e.g. "A1" or "Ext" for Bambu AMS trays


@dataclass
class Status:
    online: bool = False
    state: str = "offline"  # offline|idle|printing|paused|complete|cancelled|failed|error|starting|not_set_up
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


@dataclass
class Setting:
    """One printer setting shown in the Settings dialog, with the value read back from the printer."""

    id: str
    group: str
    label: str
    kind: str  # "toggle" | "choice" | "number"
    value: object = None  # None = the printer doesn't report it (yet)
    about: str = ""
    options: list = field(default_factory=list)  # choice: [{"value": ..., "label": ...}]
    min: float | None = None
    max: float | None = None
    step: float | None = None
    unit: str = ""
    note: str = ""  # shown under the control, e.g. "Resets when the next print starts"
    disabled: str = ""  # why it can't be changed right now ("" = it can)


def check_value(s: Setting, value):
    """Coerce a value from the browser to the setting's type, or raise PrinterError."""
    if s.disabled:
        raise PrinterError(s.disabled)
    if s.kind == "toggle":
        if not isinstance(value, bool):
            raise PrinterError(f"{s.label} must be on or off")
        return value
    if s.kind == "choice":
        allowed = [o["value"] for o in s.options]
        if value not in allowed:
            raise PrinterError(f"{s.label} must be one of: " + ", ".join(str(a) for a in allowed))
        return value
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise PrinterError(f"{s.label} must be a number") from None
    if (s.min is not None and v < s.min) or (s.max is not None and v > s.max):
        raise PrinterError(f"{s.label} must be between {s.min:g} and {s.max:g}")
    return int(v) if v.is_integer() else v


def same_value(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) < 0.6
    return a == b


class Printer:
    """Base driver. Subclasses fill in ``poll`` and the actions they support."""

    can_print = False
    can_camera = False
    ams_mapping = False  # True: file filaments are mapped to trays at print time (Bambu AMS)
    calibrations: tuple[Calibration, ...] = ()
    poll_seconds = 3.0
    settings_wait = 8.0  # seconds to wait for a changed setting to show up
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
        self.light_auto = False
        self.light_auto_file: Path | None = None
        # Settings the printer accepts but doesn't report back, as last confirmed by the printer.
        self.settings_file: Path | None = None
        self.saved_settings: dict = {}
        self._settings_lock = threading.Lock()

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
        if getattr(self, "light_auto", False) and st.online and st.light is not None:
            if st.state in ("printing", "starting") and not st.light:
                try:
                    self.action("light_on")
                    st.light = True
                except Exception:
                    pass
            elif st.state in ("idle", "complete", "cancelled", "failed") and st.light:
                try:
                    self.action("light_off")
                    st.light = False
                except Exception:
                    pass
        st.updated_at = time.time()
        self.status = st
        return st

    # -- hand-set toolhead colours
    def load_light_auto(self, path: Path) -> None:
        self.light_auto_file = path
        try:
            data = json.loads(path.read_text())
            self.light_auto = bool(data.get("auto", False))
        except (OSError, ValueError):
            self.light_auto = False

    def set_light_auto(self, val: bool) -> None:
        self.light_auto = bool(val)
        if self.light_auto_file:
            try:
                tmp = self.light_auto_file.with_suffix(".tmp")
                tmp.write_text(json.dumps({"auto": self.light_auto}))
                tmp.replace(self.light_auto_file)
            except OSError:
                pass

    def load_saved_settings(self, path: Path) -> None:
        self.settings_file = path
        try:
            data = json.loads(path.read_text())
            self.saved_settings = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self.saved_settings = {}

    def save_settings(self, **values) -> None:
        self.saved_settings.update(values, saved_at=time.time())
        if self.settings_file:
            try:
                tmp = self.settings_file.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.saved_settings))
                tmp.replace(self.settings_file)
            except OSError as exc:
                log.warning("couldn't save %s: %s", self.settings_file, exc)

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

    # -- printer settings
    def settings(self) -> list[Setting]:
        """The settings this printer has, each read back from the printer now."""
        return []

    def write_setting(self, s: Setting, value) -> None:
        raise PrinterError(f"{s.label} can't be changed on this printer")

    def set_setting(self, sid: str, value, wait: float | None = None) -> list[Setting]:
        """Change one setting, then read it back until the printer shows the new value."""
        if not self.configured:
            raise PrinterError(self.setup_hint or f"{self.profile.name} isn't set up")
        with self._settings_lock:
            current = {s.id: s for s in self.settings()}
            s = current.get(sid)
            if s is None:
                raise PrinterError(f"{self.profile.name} has no setting '{sid}'")
            value = check_value(s, value)
            log.info("%s: setting %s = %r", self.profile.name, sid, value)
            self.write_setting(s, value)
            deadline = time.monotonic() + (wait if wait is not None else self.settings_wait)
            while True:
                now = {x.id: x for x in self.settings()}
                got = now[sid].value if sid in now else None
                if same_value(got, value):
                    return list(now.values())
                if time.monotonic() > deadline:
                    raise PrinterError(f"{self.profile.name} didn't confirm {s.label} (it still reports {got!r})")
                self._settings_wait()

    def _settings_wait(self) -> None:
        time.sleep(0.5)

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
            "light_auto": getattr(self, "light_auto", False),
            "has_settings": self.configured and type(self).settings is not Printer.settings,
        }


def _short(exc: Exception) -> str:
    text = str(exc) or exc.__class__.__name__
    return text if len(text) < 200 else text[:197] + "..."
