"""Bambu Lab printers (the P1S) in LAN-only / Developer Mode.

Status: MQTT over TLS on port 8883, user ``bblp``, password = the printer's
LAN access code. The printer publishes ``device/<serial>/report``; we ask for a
full report (``pushall``) on connect and every few minutes, and merge the
partial updates it sends in between.
Camera: the P1 series streams JPEG frames over a TLS socket on port 6000 after
an 80-byte login packet. We connect, read one frame, and hang up.
Printing: upload the sliced ``.gcode.3mf`` to the SD card over implicit FTPS
(port 990), then the MQTT ``project_file`` command with an AMS mapping (file
filament -> AMS tray, 254 = the external spool).
Toolheads: the P1S has one nozzle, so the "toolheads" Print Pantry shows are
AMS trays (index = ams_id * 4 + slot, labelled A1..A4, B1..) plus the external
spool (index 254, "Ext"). The print dialog's toolhead picker becomes the AMS
slot picker.
History: the P1S keeps no job list on LAN, so jobs are tracked here from the
state changes in the reports and kept in a small JSON file.
"""

from __future__ import annotations

import ftplib
import hashlib
import json
import logging
import socket
import ssl
import struct
import threading
import time
import zipfile
from pathlib import Path

from .base import IDLE_STATES, Calibration, Printer, PrinterError, Status, Temp, Toolhead

log = logging.getLogger("printpantry.bambu")

EXTERNAL = 254  # Bambu's tray id for the spool on the back of the printer
STATE_MAP = {
    "IDLE": "idle",
    "PREPARE": "starting",
    "SLICING": "starting",
    "RUNNING": "printing",
    "PAUSE": "paused",
    "FINISH": "complete",
    "FAILED": "failed",
}
CANCEL_CODES = {0, 50348044}  # print_error after "stop" (0300-400C = task cancelled)
PUSHALL_EVERY = 300.0  # the P1 series asks to be sparing with full reports
SNAPSHOT_CACHE = 2.0
CAMERA_PORT = 6000
MAX_FRAME = 4 * 1024 * 1024
KEEP_JOBS = 200

# Calibrations the printer runs itself, through the same "calibration" command
# Bambu Studio sends (option is a bit mask).
P1S_CALIBRATIONS = (
    Calibration(
        "bed_level", "Bed leveling",
        "Homes and probes the bed to build a new bed mesh.",
        "The bed is empty and the build plate is on.", 8,
    ),
    Calibration(
        "vibration", "Vibration compensation",
        "Shakes the toolhead and bed to measure resonance and tunes the motion to cancel it. "
        "It is loud for a few minutes.",
        "Nothing is on the bed and nothing loose is sitting on the printer.", 8,
    ),
    Calibration(
        "motor_noise", "Motor noise cancellation",
        "Measures the motors to reduce their noise while printing.",
        "Nothing is on the bed and the printer can move freely.", 10,
    ),
)
CALIBRATION_OPTION = {"bed_level": 1 << 1, "vibration": 1 << 2, "motor_noise": 1 << 3}
CALIBRATION_START_WAIT = 120.0
CALIBRATION_TIMEOUT = 3600.0


def _merge(dst: dict, src: dict) -> None:
    """Deep-merge a partial report into the full one (lists are replaced)."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = v


def _num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _color(value) -> str:
    v = str(value or "").strip().lstrip("#")
    if len(v) in (6, 8) and all(c in "0123456789abcdefABCDEF" for c in v):
        if len(v) == 8 and v[6:] == "00":
            return ""  # fully transparent = no colour reported
        return "#" + v[:6].upper()
    return ""


def error_text(code: int) -> str:
    """Bambu shows print errors as e.g. 0300-400C."""
    h = f"{code:08X}"
    return f"{h[:4]}-{h[4:]}"


def tray_label(index: int, ams_units: int) -> str:
    if index == EXTERNAL:
        return "Ext"
    return f"{'ABCD'[index // 4]}{index % 4 + 1}" if index < 16 else f"#{index}"


def ams_mapping(tool_map: dict[int, int]) -> tuple[list[int], bool]:
    """Bambu's ams_mapping: position = file filament (0-based), value = tray id,
    -1 for filaments the file doesn't use. use_ams is off when everything goes
    to the external spool."""
    if not tool_map:
        return [], False
    n = max(tool_map) + 1
    out = [-1] * n
    for f, h in tool_map.items():
        out[f] = int(h)
    use_ams = any(0 <= h < 16 for h in out)
    if not use_ams:
        return [], False
    return out, True


def plate_gcode(path: Path) -> str:
    """The plate inside a sliced .gcode.3mf, e.g. Metadata/plate_1.gcode."""
    try:
        with zipfile.ZipFile(path) as z:
            names = sorted(n for n in z.namelist() if n.startswith("Metadata/plate_") and n.endswith(".gcode"))
    except (OSError, zipfile.BadZipFile) as exc:
        raise PrinterError(f"{path.name} isn't a sliced Bambu file: {exc}") from None
    if not names:
        raise PrinterError(f"{path.name} has no sliced plate; slice it for the P1S first")
    return names[0]


# ---------------------------------------------------------------- transports
class MqttLink:
    """paho-mqtt connection to the printer. Reconnects by itself."""

    def __init__(self, host: str, serial: str, access_code: str, on_report, on_state=None):
        import paho.mqtt.client as mqtt

        self.serial = serial
        self._on_report = on_report
        self._on_state = on_state or (lambda up: None)
        self.connected = False
        self.error = ""
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"printpantry-{int(time.time())}",
                        protocol=mqtt.MQTTv311)
        c.username_pw_set("bblp", access_code)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # the printer's certificate is self-signed
        c.tls_set_context(ctx)
        c.reconnect_delay_set(2, 30)
        c.on_connect = self._connected
        c.on_disconnect = self._disconnected
        c.on_message = self._message
        self.client = c
        self.host = host

    def start(self) -> None:
        self.client.connect_async(self.host, 8883, keepalive=30)
        self.client.loop_start()

    def stop(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()

    def _connected(self, client, userdata, flags, reason, properties=None):
        if getattr(reason, "is_failure", False):
            self.error = f"Printer refused the connection ({reason}); check the access code"
            self.connected = False
            return
        self.error = ""
        self.connected = True
        client.subscribe(f"device/{self.serial}/report")
        self._on_state(True)

    def _disconnected(self, client, userdata, flags, reason, properties=None):
        self.connected = False
        if getattr(reason, "is_failure", False):
            self.error = f"Lost connection ({reason})"
        self._on_state(False)

    def _message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload)
        except ValueError:
            return
        self._on_report(data)

    def publish(self, payload: dict) -> None:
        if not self.connected:
            raise PrinterError("Not connected to the printer")
        info = self.client.publish(f"device/{self.serial}/request", json.dumps(payload))
        if info.rc != 0:
            raise PrinterError(f"Couldn't send to the printer (MQTT error {info.rc})")


class ImplicitFTPS(ftplib.FTP_TLS):
    """FTPS with TLS from the first byte (port 990), as Bambu printers use.
    The data connection must reuse the control connection's TLS session."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size

    def store(self, name: str, fh, blocksize: int = 65536) -> None:
        # storbinary's TLS unwrap hangs on the printer; close the data socket instead.
        self.voidcmd("TYPE I")
        conn = self.transfercmd(f"STOR {name}")
        try:
            while chunk := fh.read(blocksize):
                conn.sendall(chunk)
        finally:
            conn.close()
        self.voidresp()


def _tls_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def default_ftp(host: str, access_code: str):
    ftp = ImplicitFTPS(context=_tls_context(), timeout=30)
    ftp.connect(host, 990)
    ftp.login("bblp", access_code)
    ftp.prot_p()
    return ftp


def _recv_exact(sock, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise PrinterError("The camera closed the connection")
        buf += chunk
    return bytes(buf)


def default_camera(host: str, access_code: str, timeout: float = 8.0) -> bytes:
    auth = (struct.pack("<IIII", 0x40, 0x3000, 0, 0)
            + b"bblp".ljust(32, b"\0") + access_code.encode().ljust(32, b"\0"))
    with socket.create_connection((host, CAMERA_PORT), timeout=timeout) as raw:
        with _tls_context().wrap_socket(raw, server_hostname=host) as s:
            s.sendall(auth)
            size = struct.unpack("<I", _recv_exact(s, 16)[:4])[0]
            if not 0 < size < MAX_FRAME:
                raise PrinterError("The camera sent a bad frame")
            data = _recv_exact(s, size)
    if not data.startswith(b"\xff\xd8"):
        raise PrinterError("The camera sent something that isn't a picture")
    return data


# ---------------------------------------------------------------- driver
class BambuPrinter(Printer):
    can_print = True
    can_camera = True
    ams_mapping = True  # file filaments are mapped to AMS trays at print time
    calibrations = P1S_CALIBRATIONS

    def __init__(self, profile, host: str = "", serial: str = "", access_code: str = "",
                 link_factory=None, ftp_factory=None, camera=None, ledger: Path | None = None,
                 jobs_file: Path | None = None, keep_files: int = 5):
        super().__init__(profile)
        self.host, self.serial, self.access_code = host.strip(), serial.strip(), access_code.strip()
        self.ledger, self.jobs_file, self.keep_files = ledger, jobs_file, keep_files
        self._link_factory = link_factory or (lambda on_report, on_state: MqttLink(
            self.host, self.serial, self.access_code, on_report, on_state))
        self._ftp = ftp_factory or (lambda: default_ftp(self.host, self.access_code))
        self._camera = camera or (lambda: default_camera(self.host, self.access_code))
        self.link = None
        self._report: dict = {}
        self._report_lock = threading.Lock()
        self._got_report = threading.Event()
        self._pushed_at = 0.0
        self._snap: tuple[bytes, float] | None = None
        self._snap_at = 0.0
        self._cam_lock = threading.Lock()
        self._jobs: list[dict] = self._load_jobs()
        self._last_gstate = ""
        missing = [n for n, v in (("PP_P1S_HOST", self.host), ("PP_P1S_SERIAL", self.serial),
                                  ("PP_P1S_ACCESS_CODE", self.access_code)) if not v]
        if missing:
            self.configured = False
            self.setup_hint = (
                "Not connected yet: " + ", ".join(missing) + " missing. Put the P1S's IP, serial and LAN "
                "access code (Settings > WLAN on the printer) in src/.pp_p1s on the NAS and redeploy."
            )
            self.status = Status(online=False, state="not_set_up", message=self.setup_hint)

    # ------------------------------------------------------------ connection
    def _ensure_link(self):
        if self.link is None:
            self.link = self._link_factory(self.on_report, self._link_state)
            self.link.start()
        return self.link

    def _link_state(self, up: bool) -> None:
        if up:
            self._pushed_at = 0.0  # ask for a full report right away

    def stop(self) -> None:
        super().stop()
        if self.link is not None:
            try:
                self.link.stop()
            except Exception:  # noqa: BLE001
                pass

    def _send(self, payload: dict) -> None:
        self._ensure_link().publish(payload)

    def _print_cmd(self, command: str, **extra) -> None:
        self._send({"print": {"sequence_id": "0", "command": command, **extra}})

    def on_report(self, data: dict) -> None:
        p = data.get("print")
        if not isinstance(p, dict):
            return
        with self._report_lock:
            _merge(self._report, p)
        if p.get("command") == "push_status" or "gcode_state" in p:
            self._got_report.set()

    # ------------------------------------------------------------ status
    def poll(self) -> Status:
        link = self._ensure_link()
        if not link.connected:
            raise PrinterError(link.error or "Can't reach the printer over MQTT")
        now = time.monotonic()
        if now - self._pushed_at > PUSHALL_EVERY:
            self._pushed_at = now
            link.publish({"pushing": {"sequence_id": "0", "command": "pushall", "version": 1, "push_target": 1}})
            if not self._got_report.is_set():
                self._got_report.wait(5.0)
        if not self._got_report.is_set():
            raise PrinterError("Connected, waiting for the printer's first report")
        with self._report_lock:
            report = json.loads(json.dumps(self._report))
        st = self.parse(report)
        self._track_job(report, st)
        return st

    def parse(self, r: dict) -> Status:
        st = Status(online=True)
        gstate = str(r.get("gcode_state") or "IDLE").upper()
        st.state = STATE_MAP.get(gstate, "idle")
        err = int(_num(r.get("print_error"), 0) or 0)
        if gstate == "FAILED":
            if err in CANCEL_CODES:
                st.state = "cancelled"
            else:
                st.message = f"Print failed (error {error_text(err)})"
        elif err and st.state in ("printing", "paused"):
            st.message = f"Printer error {error_text(err)}"
        hms = r.get("hms") or []
        if hms and not st.message and st.state in ("printing", "paused"):
            st.message = f"{len(hms)} printer warning(s) on the screen"

        st.file = str(r.get("subtask_name") or r.get("gcode_file") or "")
        if st.state in ("printing", "paused") or (st.file and st.state in ("complete", "cancelled", "failed")):
            pct = _num(r.get("mc_percent"))
            st.progress = pct / 100 if pct is not None else None
            st.layer = int(_num(r.get("layer_num"), 0) or 0) or None
            st.total_layers = int(_num(r.get("total_layer_num"), 0) or 0) or None
            if st.state in ("printing", "paused"):
                left = _num(r.get("mc_remaining_time"))
                st.eta_seconds = left * 60 if left is not None else None
                started = _num(r.get("gcode_start_time"))
                if started and started > 1e9:
                    st.print_seconds = max(0.0, time.time() - started)

        st.temps.append(Temp("Nozzle", _num(r.get("nozzle_temper")), _num(r.get("nozzle_target_temper"))))
        st.temps.append(Temp("Bed", _num(r.get("bed_temper")), _num(r.get("bed_target_temper"))))

        for light in r.get("lights_report") or []:
            if light.get("node") == "chamber_light":
                st.light = light.get("mode") in ("on", "flashing")

        st.toolheads = self._trays(r)
        return st

    def _trays(self, r: dict) -> list[Toolhead]:
        ams = r.get("ams") or {}
        units = ams.get("ams") or []
        now = str(ams.get("tray_now", "255"))
        heads: list[Toolhead] = []
        for unit in units:
            try:
                aid = int(unit.get("id", 0))
            except (TypeError, ValueError):
                continue
            for tray in unit.get("tray") or []:
                try:
                    idx = aid * 4 + int(tray.get("id", 0))
                except (TypeError, ValueError):
                    continue
                heads.append(self._tray(idx, tray, len(units), now))
        vt = r.get("vt_tray")
        if isinstance(vt, dict) or not units:
            heads.append(self._tray(EXTERNAL, vt or {}, len(units), now))
        return sorted(heads, key=lambda t: t.index)

    @staticmethod
    def _tray(idx: int, tray: dict, units: int, now: str) -> Toolhead:
        kind = str(tray.get("tray_type") or "")
        loaded = bool(kind) if idx != EXTERNAL else (True if kind else None)
        return Toolhead(
            index=idx,
            type=kind,
            color=_color(tray.get("tray_color")) if kind else "",
            vendor=str(tray.get("tray_sub_brands") or ""),
            loaded=loaded,
            active=(now == str(idx)),
            label=tray_label(idx, units),
        )

    # ------------------------------------------------------------ job tracking (history)
    def _load_jobs(self) -> list[dict]:
        try:
            jobs = json.loads(self.jobs_file.read_text())
            return [j for j in jobs if isinstance(j, dict) and j.get("job_id")]
        except (OSError, ValueError, AttributeError):
            return []

    def _save_jobs(self) -> None:
        if not self.jobs_file:
            return
        self._jobs = self._jobs[-KEEP_JOBS:]
        tmp = self.jobs_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._jobs))
        tmp.replace(self.jobs_file)

    def _track_job(self, r: dict, st: Status) -> None:
        gstate = str(r.get("gcode_state") or "IDLE").upper()
        if gstate == self._last_gstate:
            return
        self._last_gstate = gstate
        name = st.file
        started = _num(r.get("gcode_start_time"))
        if not started or started < 1e9:
            started = None
        cur = self._jobs[-1] if self._jobs else None
        if gstate in ("PREPARE", "RUNNING", "PAUSE") and name:
            start = started or time.time()
            if cur and cur.get("status") == "in_progress" and cur.get("filename") == name \
                    and abs((cur.get("start_time") or 0) - start) < 600:
                return
            if cur and cur.get("status") == "in_progress":
                cur.update(status="interrupted", end_time=time.time())
            self._jobs.append({"job_id": str(int(start)), "filename": name, "start_time": start,
                               "end_time": None, "status": "in_progress", "metadata": {}})
            self._save_jobs()
        elif gstate in ("FINISH", "FAILED") and cur and cur.get("status") == "in_progress":
            end = time.time()
            err = int(_num(r.get("print_error"), 0) or 0)
            status = "completed" if gstate == "FINISH" else ("cancelled" if err in CANCEL_CODES else "error")
            dur = end - (cur.get("start_time") or end)
            cur.update(status=status, end_time=end, print_duration=dur, total_duration=dur)
            self._save_jobs()

    def job_history(self, limit: int = 50) -> list[dict]:
        """Jobs seen by Print Pantry, newest first, shaped like Moonraker's."""
        return [dict(j) for j in reversed(self._jobs[-limit:])]

    # ------------------------------------------------------------ camera
    def snapshot(self) -> tuple[bytes, float | None]:
        with self._cam_lock:
            now = time.monotonic()
            if self._snap and now - self._snap_at < SNAPSHOT_CACHE:
                return self._snap
            try:
                data = self._camera()
            except PrinterError:
                raise
            except OSError as exc:
                raise PrinterError(f"Camera unavailable: {exc}") from exc
            self._snap = (data, time.time())
            self._snap_at = now
            return self._snap

    # ------------------------------------------------------------ printing
    def start_print(self, path: Path, name: str, tool_map: dict[int, int] | None = None) -> str:
        if not name.lower().endswith(".gcode.3mf"):
            raise PrinterError("The P1S prints sliced .gcode.3mf files; slice it for the P1S first")
        st = self.refresh()
        if not st.online:
            raise PrinterError(f"{self.profile.name} is offline: {st.message or 'no answer'}")
        if st.state not in IDLE_STATES:
            raise PrinterError(f"{self.profile.name} is {st.state}, not ready for a new print")
        plate = plate_gcode(path)
        md5 = hashlib.md5(path.read_bytes()).hexdigest()
        remote = name
        ftp = self._ftp()
        try:
            with path.open("rb") as fh:
                ftp.store(remote, fh)
        except ftplib.all_errors as exc:
            raise PrinterError(f"Couldn't copy the file to the printer's SD card: {exc}") from exc
        finally:
            try:
                ftp.quit()
            except Exception:  # noqa: BLE001
                pass
        st = self.refresh()
        if st.state not in IDLE_STATES:
            raise PrinterError(f"File uploaded, but {self.profile.name} is now {st.state}, so it wasn't started")
        mapping, use_ams = ams_mapping(tool_map or {})
        cmd = {
            "param": plate,
            "project_id": "0",
            "profile_id": "0",
            "task_id": "0",
            "subtask_id": "0",
            "subtask_name": remote,
            "file": remote,
            "url": f"file:///sdcard/{remote}",
            "md5": md5,
            "timelapse": False,
            "bed_type": "auto",
            "bed_levelling": True,
            "flow_cali": False,
            "vibration_cali": True,
            "layer_inspect": False,
            "use_ams": use_ams,
        }
        if use_ams:
            cmd["ams_mapping"] = mapping
        log.info("%s: project_file %s plate %s ams %s", self.profile.name, remote, plate, mapping or "off")
        self._print_cmd("project_file", **cmd)
        self._wait_started()
        try:
            self.forget_old_uploads(remote)
        except Exception as exc:  # noqa: BLE001 - the print started; cleanup can wait
            log.warning("couldn't clean up old uploads on %s: %s", self.profile.name, exc)
        return remote

    def _wait_started(self, timeout: float = 20.0) -> None:
        """Wait for the printer to leave idle, or report why it refused."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(1.0)
            st = self.refresh()
            if st.state in ("starting", "printing", "paused"):
                return
            if st.state == "failed":
                raise PrinterError(f"{self.profile.name} refused the print: {st.message}")
        log.warning("%s didn't report starting within %.0fs", self.profile.name, timeout)

    # ------------------------------------------------------------ SD card cleanup
    def _read_ledger(self) -> list[str]:
        try:
            names = json.loads(self.ledger.read_text())
            return [n for n in names if isinstance(n, str)]
        except (OSError, ValueError, AttributeError):
            return []

    def forget_old_uploads(self, latest: str) -> list[str]:
        """Record ``latest``; delete our uploads beyond the newest ``keep_files`` from the SD card."""
        if self.ledger is None:
            return []
        names = [n for n in self._read_ledger() if n != latest] + [latest]
        removed: list[str] = []
        if self.keep_files > 0 and len(names) > self.keep_files:
            old, names = names[: -self.keep_files], names[-self.keep_files:]
            ftp = self._ftp()
            try:
                for name in old:
                    try:
                        ftp.delete(name)
                        removed.append(name)
                    except ftplib.error_perm as exc:
                        if str(exc).startswith("550"):  # already gone
                            removed.append(name)
                        else:
                            names.insert(0, name)
                    except ftplib.all_errors:
                        names.insert(0, name)
            finally:
                try:
                    ftp.quit()
                except Exception:  # noqa: BLE001
                    pass
        tmp = self.ledger.with_suffix(".tmp")
        tmp.write_text(json.dumps(names))
        tmp.replace(self.ledger)
        if removed:
            log.info("removed %d old uploads from %s: %s", len(removed), self.profile.name, ", ".join(removed))
        return removed

    # ------------------------------------------------------------ actions
    def action(self, name: str) -> None:
        if name in ("light_on", "light_off"):
            mode = "on" if name == "light_on" else "off"
            self._send({"system": {"sequence_id": "0", "command": "ledctrl", "led_node": "chamber_light",
                                   "led_mode": mode, "led_on_time": 500, "led_off_time": 500,
                                   "loop_times": 0, "interval_time": 0}})
            with self._report_lock:  # the printer confirms a moment later; show it now
                self._report["lights_report"] = [{"node": "chamber_light", "mode": mode}]
            self._snap_at = 0.0
            self.refresh()
            return
        if name in ("pause", "resume"):
            self._print_cmd(name)
        elif name in ("cancel", "estop"):
            # No emergency stop over LAN; stopping the print is the closest.
            self._print_cmd("stop")
            if self.calibrating and self.calibrating.get("state") == "running":
                self.calibrating.update(state="failed", message="Stopped", finished=time.time())
        else:
            raise PrinterError(f"Unknown action '{name}'")
        time.sleep(1.0)
        self.refresh()

    # ------------------------------------------------------------ calibration
    def calibration_script(self, cal: Calibration, values: dict) -> str:
        return str(CALIBRATION_OPTION[cal.id])

    def run_script(self, script: str) -> None:
        self._print_cmd("calibration", option=int(script))
        start = time.monotonic()
        busy = False
        while time.monotonic() - start < CALIBRATION_TIMEOUT:
            time.sleep(3.0)
            st = self.refresh()
            with self._report_lock:
                stage = int(_num(self._report.get("stg_cur"), 0) or 0)
            working = st.state not in IDLE_STATES or stage not in (0, 255, -1)
            if working:
                busy = True
            elif busy:
                return
            elif time.monotonic() - start > CALIBRATION_START_WAIT:
                raise PrinterError("The printer didn't start the calibration")
        raise PrinterError("The calibration didn't finish within an hour")
