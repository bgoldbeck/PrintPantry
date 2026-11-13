"""Klipper printers through Moonraker's HTTP API. Used for the Snapmaker U1.

Status: ``/printer/objects/query`` every few seconds.
Camera: the U1's stock firmware only refreshes ``camera/monitor.jpg`` while
someone has asked for it recently, with the Snapmaker-specific websocket call
``camera.start_monitor``. We send that call at most every ``WAKE_EVERY``
seconds while the camera is being viewed, then fetch the file over HTTP.
Printing: upload to the ``gcodes`` root without starting, check the printer is
still idle, then ``/printer/print/start``.
Cleanup: files Print Pantry uploaded are listed in a small ledger in the data
volume; only the newest ``keep_files`` of them stay on the printer, so its
storage doesn't fill up. Files put there any other way are never touched.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from .base import IDLE_STATES, Calibration, Printer, PrinterError, Setting, Status, Temp, Toolhead

log = logging.getLogger("printpantry.moonraker")

EXTRUDERS = ("extruder", "extruder1", "extruder2", "extruder3")
OBJECTS = (
    "webhooks",
    "print_stats",
    "virtual_sdcard",
    "display_status",
    "heater_bed",
    "toolhead",
    "print_task_config",
    "led%20cavity_led",  # the U1's white chamber light
    *EXTRUDERS,
)
STATE_MAP = {
    "standby": "idle",
    "printing": "printing",
    "paused": "paused",
    "complete": "complete",
    "cancelled": "cancelled",
    "error": "error",
}
ACTIONS = {"pause", "resume", "cancel"}
# Settings (checked against the U1's own Klipper code, Snapmaker/u1-klipper, and
# tried on Brandon's U1 in Nov 2026). Each is read back after it is set.
RUNOUT_SENSORS = tuple(f"e{i}_filament" for i in range(4))
SETTINGS_QUERY = "&".join((
    "defect_detection", "print_task_config", "gcode_move", "fan_generic%20cavity_fan", "print_stats",
    *(f"filament_motion_sensor%20{n}" for n in RUNOUT_SENSORS),
))
AI_GROUP = "AI camera detection"
FIL_GROUP = "Filament"
LIVE_GROUP = "While printing"
LIGHT = "cavity_led"
LIGHT_GCODE = {"light_on": f"SET_LED LED={LIGHT} WHITE=1", "light_off": f"SET_LED LED={LIGHT} WHITE=0"}
# Calibrations the U1's own firmware provides (checked against its Klipper
# config, Nov 2026). Klipper runs a script to the end even if nobody waits, so
# a calibration keeps going if the app restarts.
U1_CALIBRATIONS = (
    Calibration(
        "bed_mesh", "Bed mesh",
        "Homes, heats the bed, lets it settle for 2 minutes, then probes a 13 × 13 mesh "
        "(AUTO_BED_MESH_CALIBRATE) and saves it as the default mesh.",
        "The bed is empty and the build plate is on.", 12,
        params=[{"id": "bed", "label": "Bed temperature", "unit": "°C", "default": 60, "min": 0, "max": 110}],
    ),
    Calibration(
        "input_shaper", "Input shaper",
        "Homes, then shakes the toolhead on X and Y to measure resonance (SHAPER_CALIBRATE) "
        "and saves the best shaper. It is loud for a few minutes.",
        "Nothing is on the bed and nothing loose is sitting on the printer.", 6,
    ),
    Calibration(
        "toolhead_offsets", "Toolhead offsets (T1–T4)",
        "Warms all four nozzles to 140 °C, homes, then probes each toolhead to measure its "
        "X, Y and Z offset (XYZ_OFFSET_CALIBRATE_ALL) so the four toolheads line up.",
        "The bed is empty and all four nozzle tips are clean, with no blobs of filament.", 12,
    ),
)
CALIBRATION_GCODE = {
    "bed_mesh": "G28\nAUTO_BED_MESH_CALIBRATE TEMP={bed}\nBED_MESH_PROFILE SAVE=default\nSAVE_CONFIG RESTART=0\nM140 S0",
    "input_shaper": "G28\nSHAPER_CALIBRATE",
    "toolhead_offsets": "_EXTRUDER_OFFSET_ACTION_PREHEAT\nG28\nXYZ_OFFSET_CALIBRATE_ALL\n_EXTRUDER_OFFSET_ACTION_EXIT",
}
CALIBRATION_TIMEOUT = 3600.0
WAKE_EVERY = 20.0  # seconds between camera.start_monitor calls while someone watches
SNAPSHOT_CACHE = 1.5  # seconds a fetched frame is reused for other viewers


class RpcError(PrinterError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def ws_rpc(base_url: str, method: str, params: dict, api_key: str = "", timeout: float = 5.0,
           need_reply: bool = False):
    """One JSON-RPC call over Moonraker's websocket. Some Snapmaker methods
    (camera.*, server.files.start_local_print) only exist on this transport."""
    from websockets.sync.client import connect

    parts = urlsplit(base_url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    url = f"{scheme}://{parts.netloc}/websocket"
    if api_key:
        url += f"?token={api_key}"
    msg = {"jsonrpc": "2.0", "method": method, "params": params, "id": 4242}
    with connect(url, open_timeout=timeout, close_timeout=1) as ws:
        ws.send(json.dumps(msg))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                reply = json.loads(ws.recv(timeout=max(0.1, deadline - time.monotonic())))
            except TimeoutError:
                break
            if reply.get("id") == 4242:
                if "error" in reply:
                    err = reply["error"]
                    text = err.get("message", err) if isinstance(err, dict) else err
                    raise RpcError(f"{method} refused: {text}", err.get("code") if isinstance(err, dict) else None)
                return reply.get("result")
    if need_reply:
        raise PrinterError(f"No answer to {method} from the printer")
    return None


def default_wake_camera(base_url: str, api_key: str = "", timeout: float = 5.0) -> None:
    """Ask the U1 to keep refreshing camera/monitor.jpg (Snapmaker websocket method)."""
    ws_rpc(base_url, "camera.start_monitor", {"domain": "lan", "interval": 0}, api_key, timeout)


def _color(value) -> str:
    """print_task_config colours come as RRGGBBAA hex (or #RRGGBB); return #RRGGBB."""
    if not isinstance(value, str):
        return ""
    v = value.strip().lstrip("#")
    if v.lower().startswith("0x"):
        v = v[2:]
    if len(v) in (6, 8) and all(c in "0123456789abcdefABCDEF" for c in v):
        return "#" + v[:6].upper()
    return ""


def _at(seq, i, default=None):
    return seq[i] if isinstance(seq, list) and i < len(seq) else default


class MoonrakerPrinter(Printer):
    can_print = True
    can_camera = True
    calibrations = U1_CALIBRATIONS

    def __init__(self, profile, base_url: str, api_key: str = "", transport=None, wake_camera=None,
                 ledger: Path | None = None, keep_files: int = 5, rpc=None):
        super().__init__(profile)
        self.ledger = ledger
        self.keep_files = keep_files
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        headers = {"X-Api-Key": api_key} if api_key else {}
        self.http = httpx.Client(base_url=self.base_url, headers=headers, timeout=5.0, transport=transport)
        self._wake = wake_camera or (lambda: default_wake_camera(self.base_url, api_key))
        self._rpc = rpc or (lambda method, params, timeout=5.0: ws_rpc(
            self.base_url, method, params, api_key, timeout, need_reply=True))
        self._task_cfg: dict = {}
        self._woke_at = 0.0
        self._snap: tuple[bytes, float | None] | None = None
        self._snap_at = 0.0
        self._lock = threading.Lock()
        self._meta: dict[str, dict] = {}

    # ------------------------------------------------------------ status
    def _get(self, path: str, **params):
        r = self.http.get(path, params=params or None)
        if r.status_code >= 400:
            raise PrinterError(_error_text(r))
        return r.json().get("result")

    def _post(self, path: str, timeout: float | None = None, **kwargs):
        r = self.http.post(path, timeout=timeout or self.http.timeout, **kwargs)
        if r.status_code >= 400:
            raise PrinterError(_error_text(r))
        return r.json().get("result")

    def poll(self) -> Status:
        query = "&".join(OBJECTS)
        res = self._get(f"/printer/objects/query?{query}") or {}
        return self.parse(res.get("status", {}))

    def parse(self, s: dict) -> Status:
        st = Status(online=True)
        hooks = s.get("webhooks", {})
        ps = s.get("print_stats", {})
        if hooks.get("state", "ready") != "ready":
            st.state = "error" if hooks.get("state") in ("error", "shutdown") else "starting"
            st.message = (hooks.get("state_message") or "").strip()
        else:
            st.state = STATE_MAP.get(ps.get("state", "standby"), "idle")
            st.message = (ps.get("message") or "").strip()

        st.file = ps.get("filename") or ""
        if st.state in ("printing", "paused") or (st.file and st.state in ("complete", "cancelled", "error")):
            prog = s.get("display_status", {}).get("progress")
            if not prog:
                prog = s.get("virtual_sdcard", {}).get("progress")
            st.progress = float(prog) if prog is not None else None
            st.print_seconds = ps.get("print_duration")
            info = ps.get("info") or {}
            st.layer = info.get("current_layer")
            st.total_layers = info.get("total_layer")
            if st.state in ("printing", "paused"):
                st.eta_seconds = self._eta(st)

        active = (s.get("toolhead") or {}).get("extruder", "")
        for i, name in enumerate(EXTRUDERS):
            e = s.get(name)
            if e is None:
                continue
            st.temps.append(Temp(f"T{i + 1}", e.get("temperature"), e.get("target")))
        bed = s.get("heater_bed")
        if bed is not None:
            st.temps.append(Temp("Bed", bed.get("temperature"), bed.get("target")))

        led = s.get(f"led {LIGHT}")
        if led is not None:
            data = led.get("color_data") or []
            st.light = any(v and v > 0 for v in (data[0] if data else []))

        cfg = s.get("print_task_config") or {}
        self._task_cfg = cfg
        types = cfg.get("filament_type") or []
        count = max(len(types), sum(1 for n in EXTRUDERS if n in s))
        for i in range(count):
            exist = _at(cfg.get("filament_exist"), i)
            st.toolheads.append(
                Toolhead(
                    index=i,
                    type=str(_at(types, i, "") or ""),
                    color=_color(_at(cfg.get("filament_color_rgba"), i) or _at(cfg.get("filament_color"), i)),
                    vendor=str(_at(cfg.get("filament_vendor"), i, "") or ""),
                    loaded=bool(exist) if exist is not None else None,
                    active=(active == EXTRUDERS[i]) if i < len(EXTRUDERS) else False,
                )
            )
        return st

    def _eta(self, st: Status) -> float | None:
        p, done = st.progress, st.print_seconds
        est = None
        if st.file:
            meta = self._meta.get(st.file)
            if meta is None:
                try:
                    meta = self._get("/server/files/metadata", filename=st.file) or {}
                except Exception:  # noqa: BLE001
                    meta = {}
                self._meta = {st.file: meta}
            est = meta.get("estimated_time")
        if est and p is not None:
            return max(0.0, est * (1 - p))
        if p and p > 0.02 and done:
            return max(0.0, done / p - done)
        return None

    # ------------------------------------------------------------ camera
    def snapshot(self) -> tuple[bytes, float | None]:
        with self._lock:
            now = time.monotonic()
            if self._snap and now - self._snap_at < SNAPSHOT_CACHE:
                return self._snap
            if now - self._woke_at > WAKE_EVERY:
                self._woke_at = now
                try:
                    self._wake()
                except Exception as exc:  # noqa: BLE001 - still try the file, it may be fresh
                    log.warning("camera wake failed: %s", exc)
            r = self.http.get("/server/files/camera/monitor.jpg")
            if r.status_code == 404:
                raise PrinterError("The camera hasn't produced a picture yet")
            if r.status_code >= 400:
                raise PrinterError(_error_text(r))
            taken = None
            lm = r.headers.get("last-modified")
            if lm:
                try:
                    taken = parsedate_to_datetime(lm).timestamp()
                except (TypeError, ValueError):
                    taken = None
            self._snap = (r.content, taken)
            self._snap_at = now
            return self._snap

    # ------------------------------------------------------------ printing
    def start_print(self, path: Path, name: str, tool_map: dict[int, int] | None = None) -> str:
        st = self.refresh()
        if not st.online:
            raise PrinterError(f"{self.profile.name} is offline: {st.message or 'no answer'}")
        if st.state not in IDLE_STATES:
            raise PrinterError(f"{self.profile.name} is {st.state}, not ready for a new print")
        with path.open("rb") as fh:
            res = self._post(
                "/server/files/upload",
                timeout=600.0,
                files={"file": (name, fh, "application/octet-stream")},
                data={"root": "gcodes", "print": "false"},
            ) or {}
        remote = (res.get("item") or {}).get("path") or name
        # Someone may have started something from the touchscreen during the upload.
        st = self.refresh()
        if st.state not in IDLE_STATES:
            raise PrinterError(f"File uploaded, but {self.profile.name} is now {st.state}, so it wasn't started")
        if tool_map:
            self._start_mapped(remote, tool_map)
        else:
            self._post("/printer/print/start", params={"filename": remote})
        self.refresh()
        try:
            self.forget_old_uploads(remote)
        except Exception as exc:  # noqa: BLE001 - the print started; cleanup can wait
            log.warning("couldn't clean up old uploads on %s: %s", self.profile.name, exc)
        return remote

    # ------------------------------------------------------------ job history
    def job_history(self, limit: int = 50) -> list[dict]:
        """Moonraker's job history, newest first (every print, however it was started)."""
        jobs: list[dict] = []
        start = 0
        while len(jobs) < limit:
            page = min(50, limit - len(jobs))
            res = self._get("/server/history/list", limit=page, start=start, order="desc") or {}
            got = res.get("jobs") or []
            jobs.extend(got)
            if len(got) < page:
                break
            start += len(got)
        return jobs

    def _start_mapped(self, remote: str, tool_map: dict[int, int]) -> None:
        """Start the way Snapmaker Orca does: tell the U1 which head prints each of
        the file's filaments and which heads are used (its start G-code only loads
        and primes used heads), then server.files.start_local_print with the map.
        The firmware applies the map to every T/M104/M109 in the file and resets
        it after the print. Firmware before 1.3.0 lacks start_local_print, so
        fall back to Moonraker's plain start there."""
        pairs = sorted((int(f), int(h)) for f, h in tool_map.items() if 0 <= int(h) <= 3 and 0 <= int(f) <= 31)
        used = sorted({h for _, h in pairs})
        script = "\n".join(
            [f"SET_PRINT_EXTRUDER_MAP CONFIG_EXTRUDER={f} MAP_EXTRUDER={h}" for f, h in pairs]
            + [f"SET_PRINT_USED_EXTRUDERS EXTRUDERS={','.join(map(str, used))}"]
        )
        self._post("/printer/gcode/script", timeout=30.0, params={"script": script})
        cfg = self._task_cfg
        options = {
            "bed_level": int(cfg.get("auto_bed_leveling") is True),
            "flow_calibrate": int(cfg.get("flow_calibrate") is True),
            "time_lapse_camera": int(cfg.get("time_lapse_camera") is True),
            "map_table": [list(p) for p in pairs],
        }
        params = {"type": "gcode", "path": remote, "print_plate": 1, "options": options}
        try:
            res = self._rpc("server.files.start_local_print", params, timeout=60.0)
        except RpcError as exc:
            if exc.code != -32601:
                raise
            log.info("start_local_print missing on %s; using plain start", self.profile.name)
            self._post("/printer/print/start", params={"filename": remote})
            return
        if isinstance(res, dict) and res.get("state") not in (None, "success"):
            raise PrinterError(f"{self.profile.name} refused the print: {res.get('message') or res}")

    # ------------------------------------------------------------ cleanup
    def _read_ledger(self) -> list[str]:
        try:
            names = json.loads(self.ledger.read_text())
            return [n for n in names if isinstance(n, str)]
        except (OSError, ValueError, AttributeError):
            return []

    def forget_old_uploads(self, latest: str) -> list[str]:
        """Record ``latest`` as uploaded; delete our uploads beyond the newest ``keep_files``."""
        if self.ledger is None:
            return []
        names = [n for n in self._read_ledger() if n != latest] + [latest]
        removed: list[str] = []
        if self.keep_files > 0:
            old, names = names[: -self.keep_files], names[-self.keep_files:]
            for name in old:
                if name == latest:
                    continue
                r = self.http.delete(f"/server/files/gcodes/{quote(name)}")
                if r.status_code in (200, 404):
                    removed.append(name)
                else:  # e.g. in use; try again next time
                    names.insert(0, name)
                    log.warning("couldn't delete %s from %s: %s", name, self.profile.name, _error_text(r))
        tmp = self.ledger.with_suffix(".tmp")
        tmp.write_text(json.dumps(names))
        tmp.replace(self.ledger)
        if removed:
            log.info("removed %d old uploads from %s: %s", len(removed), self.profile.name, ", ".join(removed))
        return removed

    # ------------------------------------------------------------ settings
    def settings(self) -> list[Setting]:
        res = self._get(f"/printer/objects/query?{SETTINGS_QUERY}") or {}
        st = res.get("status", {})
        out: list[Setting] = []
        dd = st.get("defect_detection")
        if isinstance(dd, dict):
            main = dd.get("main_enable")
            off = "" if main else "Turn on AI detection first"
            sub = lambda k: (dd.get(k) or {})  # noqa: E731
            out += [
                Setting("ai_main", AI_GROUP, "AI detection", "toggle", bool(main),
                        "The camera watches each print for the problems below and pauses the print when it sees one."),
                Setting("ai_noodle", AI_GROUP, "Spaghetti detection", "toggle", bool(sub("noodle").get("enable")),
                        "Pauses the print when filament turns into a tangle of loose strands.", disabled=off),
                Setting("ai_noodle_sens", AI_GROUP, "Spaghetti sensitivity", "choice", sub("noodle").get("sensitivity"),
                        "High catches problems sooner but pauses for false alarms more often.",
                        options=[{"value": "low", "label": "Low"}, {"value": "high", "label": "High"}], disabled=off),
                Setting("ai_clean_bed", AI_GROUP, "Check the bed is clear", "toggle", bool(sub("clean_bed").get("enable")),
                        "Looks for objects left on the bed before a print starts.", disabled=off),
                Setting("ai_residue", AI_GROUP, "Residue detection", "toggle", bool(sub("residue").get("enable")),
                        "Looks for leftover filament on the bed during the print.", disabled=off),
            ]
        tc = st.get("print_task_config")
        if isinstance(tc, dict) and "auto_replenish_filament" in tc:
            out += [
                Setting("auto_refill", FIL_GROUP, "Auto refill", "toggle", bool(tc.get("auto_replenish_filament")),
                        "When a spool runs out, carry on with another toolhead that has the same filament."),
                Setting("tangle", FIL_GROUP, "Tangle detection", "toggle", bool(tc.get("filament_entangle_detect")),
                        "Pauses the print when the filament stops feeding, for example a tangled spool."),
                Setting("tangle_sens", FIL_GROUP, "Tangle sensitivity", "choice", tc.get("filament_entangle_sen"),
                        options=[{"value": v, "label": v.title()} for v in ("low", "medium", "high")],
                        disabled="" if tc.get("filament_entangle_detect") else "Turn on tangle detection first"),
            ]
        sensors = [st.get(f"filament_motion_sensor {n}") for n in RUNOUT_SENSORS]
        sensors = [x for x in sensors if isinstance(x, dict)]
        if sensors:
            out.append(Setting("runout", FIL_GROUP, "Runout sensors", "toggle", all(x.get("enabled") for x in sensors),
                               "Pauses the print when a toolhead runs out of filament.",
                               note="Turns itself back on when the printer restarts."))
        gm = st.get("gcode_move") or {}
        if "speed_factor" in gm:
            out += [
                Setting("speed", LIVE_GROUP, "Speed", "number", round(gm["speed_factor"] * 100), min=50, max=200,
                        step=5, unit="%", note="Goes back to 100% when the next print starts."),
                Setting("flow", LIVE_GROUP, "Flow", "number", round(gm.get("extrude_factor", 1) * 100), min=75,
                        max=125, step=1, unit="%", note="Applies to the current print."),
            ]
        fan = st.get("fan_generic cavity_fan")
        if isinstance(fan, dict):
            out.append(Setting("cavity_fan", LIVE_GROUP, "Chamber fan", "number", round((fan.get("speed") or 0) * 100),
                               min=0, max=100, step=10, unit="%",
                               note="The print's G-code can change it again."))
        return out

    def write_setting(self, s: Setting, value) -> None:
        on = int(bool(value)) if isinstance(value, bool) else None
        script = {
            "ai_main": f"DEFECT_DETECTION_CONFIG MAIN_ENABLE={on}",
            "ai_noodle": f"DEFECT_DETECTION_CONFIG NOODLE_ENABLE={on}",
            "ai_noodle_sens": f"DEFECT_DETECTION_CONFIG NOODLE_SENSITIVITY={value}",
            "ai_clean_bed": f"DEFECT_DETECTION_CONFIG CLEAN_BED_ENABLE={on}",
            "ai_residue": f"DEFECT_DETECTION_CONFIG RESIDUE_ENABLE={on}",
            "auto_refill": f"SET_PRINT_PREFERENCES AUTO_REPLENISH_FILAMENT={on}",
            "tangle": f"SET_PRINT_PREFERENCES FILAMENT_ENTANGLE_DETECT={on}",
            "tangle_sens": f"SET_PRINT_PREFERENCES FILAMENT_ENTANGLE_SEN={value}",
            "runout": "\n".join(f"SET_FILAMENT_SENSOR SENSOR={n} ENABLE={on}" for n in RUNOUT_SENSORS),
            "speed": f"M220 S{value}",
            "flow": f"M221 S{value}",
        }.get(s.id)
        if s.id == "cavity_fan":
            script = f"SET_FAN_SPEED FAN=cavity_fan SPEED={value / 100:.2f}"
        if not script:
            raise PrinterError(f"{s.label} can't be changed")
        # Klipper runs it between moves, so it can wait a few seconds mid-print.
        self._post("/printer/gcode/script", timeout=20.0, params={"script": script})

    # ------------------------------------------------------------ calibration
    def calibration_script(self, cal: Calibration, values: dict) -> str:
        return CALIBRATION_GCODE[cal.id].format(**values)

    def run_script(self, script: str) -> None:
        # Moonraker answers when the whole script has finished.
        self._post("/printer/gcode/script", timeout=CALIBRATION_TIMEOUT, params={"script": script})
        self.refresh()

    def emergency_stop(self) -> None:
        """Stop the motors and heaters now, then restart the firmware so it is usable again."""
        self._post("/printer/emergency_stop")
        time.sleep(2)
        try:
            self._post("/printer/firmware_restart", timeout=20.0)
        except PrinterError as exc:
            log.warning("firmware restart after emergency stop failed: %s", exc)
        if self.calibrating and self.calibrating.get("state") == "running":
            self.calibrating.update(state="failed", message="Stopped", finished=time.time())
        self.refresh()

    def action(self, name: str) -> None:
        if name == "estop":
            self.emergency_stop()
            return
        if name in LIGHT_GCODE:
            # Klipper runs this between moves, so it can wait a few seconds mid-print.
            self._post("/printer/gcode/script", timeout=20.0, params={"script": LIGHT_GCODE[name]})
            self._snap_at = 0.0  # next snapshot request fetches a new frame, not the cached one
            self._woke_at = 0.0  # and makes sure the camera is awake
            self.refresh()
            return
        if name not in ACTIONS:
            raise PrinterError(f"Unknown action '{name}'")
        self._post(f"/printer/print/{name}")
        self.refresh()


def _error_text(r: httpx.Response) -> str:
    try:
        err = r.json().get("error", {})
        msg = err.get("message") if isinstance(err, dict) else str(err)
    except ValueError:
        msg = r.text[:200]
    return f"Printer said {r.status_code}: {msg or r.reason_phrase}"
