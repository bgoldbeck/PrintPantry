"""Bambu P1S driver against a fake MQTT link, FTPS and camera (no printer needed)."""

import ftplib
import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from app.config import DEFAULT_PRINTERS
from app.printers import PrinterHub, PrinterError
from app.printers import bambu
from app.printers.api import default_tool_map, preflight
from app.printers.bambu import BambuPrinter, ams_mapping

P1S = next(p for p in DEFAULT_PRINTERS if p.id == "p1s")
JPEG = b"\xff\xd8\xff\xe0p1sframe"


def report(**over):
    r = {
        "command": "push_status",
        "gcode_state": "IDLE",
        "print_error": 0,
        "mc_percent": 0,
        "mc_remaining_time": 0,
        "nozzle_temper": 24.5, "nozzle_target_temper": 0,
        "bed_temper": 23.0, "bed_target_temper": 0,
        "subtask_name": "", "gcode_file": "",
        "lights_report": [{"node": "chamber_light", "mode": "off"}],
        "ams": {"ams": [{"id": "0", "tray": [
            {"id": "0", "tray_type": "PLA", "tray_color": "FFFFFFFF", "tray_sub_brands": "PLA Basic"},
            {"id": "1", "tray_type": "PETG", "tray_color": "FF0000FF", "tray_sub_brands": "PETG HF"},
            {"id": "2", "tray_type": "PLA", "tray_color": "00AE42FF"},
            {"id": "3"},
        ]}], "tray_now": "255"},
        "vt_tray": {"id": "254", "tray_type": "", "tray_color": "00000000"},
    }
    r.update(over)
    return {"print": r}


class FakeLink:
    def __init__(self, drv):
        self.drv = drv
        self.connected = False
        self.error = ""
        self.sent: list[dict] = []
        self.on_publish = None

    def start(self):
        self.connected = True

    def stop(self):
        self.connected = False

    def publish(self, payload):
        self.sent.append(payload)
        if "pushing" in payload:
            self.drv.on_report(self.state)
        if self.on_publish:
            self.on_publish(payload)


class FakeFTP:
    def __init__(self, store):
        self.files = store

    def store(self, name, fh):
        self.files[name] = fh.read()

    def delete(self, name):
        if name not in self.files:
            raise ftplib.error_perm("550 not found")
        del self.files[name]

    def quit(self):
        pass


def sliced(tmp_path, name="cube_P1S.gcode.3mf"):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Metadata/plate_1.gcode", "G28\n")
        z.writestr("Metadata/plate_1.json", "{}")
    return path


@pytest.fixture
def p1s(tmp_path, monkeypatch):
    monkeypatch.setattr(bambu.time, "sleep", lambda s: None)
    files: dict[str, bytes] = {}
    holder = {}

    def factory(on_report, on_state):
        link = FakeLink(drv)
        link.state = report()
        holder["link"] = link
        return link

    drv = BambuPrinter(P1S, "192.168.1.51", "01P00TEST", "12345678", link_factory=factory,
                       ftp_factory=lambda: FakeFTP(files), camera=lambda: JPEG,
                       ledger=tmp_path / "uploads-p1s.json", jobs_file=tmp_path / "jobs-p1s.json", keep_files=2)
    drv.files = files
    drv._ensure_link()
    drv.fake = holder["link"]
    return drv


def test_not_configured_without_secrets():
    drv = BambuPrinter(P1S, "192.168.1.51", "", "")
    assert not drv.configured
    assert drv.status.state == "not_set_up"
    assert "PP_P1S_SERIAL" in drv.setup_hint


def test_hub_builds_bambu_from_env(tmp_path):
    hub = PrinterHub(DEFAULT_PRINTERS, env={"PP_P1S_HOST": "h", "PP_P1S_SERIAL": "s", "PP_P1S_ACCESS_CODE": "c"},
                     data=tmp_path)
    drv = hub.get("p1s")
    assert isinstance(drv, BambuPrinter) and drv.configured
    assert drv.ledger == tmp_path / "uploads-p1s.json"


def test_status_from_report(p1s):
    st = p1s.refresh()
    assert st.online and st.state == "idle"
    assert [t.name for t in st.temps] == ["Nozzle", "Bed"]
    assert st.light is False
    labels = [(t.index, t.label, t.type, t.loaded) for t in st.toolheads]
    assert labels == [(0, "A1", "PLA", True), (1, "A2", "PETG", True), (2, "A3", "PLA", True),
                      (3, "A4", "", False), (254, "Ext", "", None)]
    assert st.toolheads[1].color == "#FF0000"
    assert {"pushing": {"sequence_id": "0", "command": "pushall", "version": 1, "push_target": 1}} in p1s.fake.sent


def test_partial_updates_merge(p1s):
    p1s.refresh()
    p1s.on_report({"print": {"gcode_state": "RUNNING", "mc_percent": 42, "mc_remaining_time": 30,
                             "subtask_name": "cube_P1S.gcode.3mf", "layer_num": 10, "total_layer_num": 100}})
    st = p1s.refresh()
    assert st.state == "printing" and st.progress == 0.42 and st.eta_seconds == 1800
    assert st.layer == 10 and st.file == "cube_P1S.gcode.3mf"
    assert len(st.toolheads) == 5  # AMS info kept from the full report


def test_failed_and_cancelled(p1s):
    p1s.refresh()
    p1s.on_report(report(gcode_state="FAILED", print_error=50348044, subtask_name="a.gcode.3mf"))
    assert p1s.refresh().state == "cancelled"
    p1s.on_report(report(gcode_state="FAILED", print_error=0x07008011, subtask_name="a.gcode.3mf"))
    st = p1s.refresh()
    assert st.state == "failed" and "0700-8011" in st.message


def test_offline_when_not_connected(p1s):
    p1s.fake.connected = False
    p1s.fake.error = "refused"
    st = p1s.refresh()
    assert not st.online and st.message == "refused"


def test_light_and_actions(p1s):
    p1s.refresh()
    p1s.action("light_on")
    assert p1s.fake.sent[-1]["system"]["led_mode"] == "on"
    assert p1s.status.light is True
    p1s.action("pause")
    assert p1s.fake.sent[-1] == {"print": {"sequence_id": "0", "command": "pause"}}
    p1s.action("cancel")
    assert p1s.fake.sent[-1]["print"]["command"] == "stop"
    with pytest.raises(PrinterError):
        p1s.action("dance")


def test_snapshot_cached(p1s):
    data, taken = p1s.snapshot()
    assert data == JPEG and taken


def test_ams_mapping():
    assert ams_mapping({0: 2, 2: 0}) == ([2, -1, 0], True)
    assert ams_mapping({0: 254}) == ([], False)
    assert ams_mapping({}) == ([], False)


def test_start_print_uploads_and_maps(p1s, tmp_path):
    p1s.refresh()

    def started(payload):
        if payload.get("print", {}).get("command") == "project_file":
            p1s.on_report({"print": {"gcode_state": "PREPARE", "subtask_name": payload["print"]["subtask_name"],
                                     "gcode_start_time": "1763000000"}})
    p1s.fake.on_publish = started
    path = sliced(tmp_path)
    remote = p1s.start_print(path, path.name, tool_map={0: 2})
    assert remote == path.name and path.name in p1s.files
    cmd = next(m["print"] for m in p1s.fake.sent if m.get("print", {}).get("command") == "project_file")
    assert cmd["param"] == "Metadata/plate_1.gcode"
    assert cmd["url"] == f"file:///sdcard/{path.name}"
    assert cmd["ams_mapping"] == [2] and cmd["use_ams"] is True
    assert p1s.status.state == "starting"
    jobs = p1s.job_history()
    assert jobs[0]["filename"] == path.name and jobs[0]["status"] == "in_progress"
    assert jobs[0]["start_time"] == 1763000000

    p1s.on_report({"print": {"gcode_state": "FINISH", "mc_percent": 100}})
    p1s.refresh()
    assert p1s.job_history()[0]["status"] == "completed"
    assert json.loads((tmp_path / "jobs-p1s.json").read_text())[0]["status"] == "completed"


def test_start_print_rejects_plain_gcode(p1s, tmp_path):
    p = tmp_path / "x.gcode"
    p.write_text("G28")
    with pytest.raises(PrinterError):
        p1s.start_print(p, p.name)


def test_old_uploads_removed(p1s):
    for n in ("a.gcode.3mf", "b.gcode.3mf", "c.gcode.3mf"):
        p1s.files[n] = b"x"
        p1s.forget_old_uploads(n)
    assert "a.gcode.3mf" not in p1s.files and set(p1s.files) == {"b.gcode.3mf", "c.gcode.3mf"}


def test_preflight_uses_tray_labels(p1s):
    p1s.refresh()
    fl = SimpleNamespace(slot=1, type="PETG", color="#FF0000", grams=5)
    f = SimpleNamespace(kind="sliced3mf", printer="p1s", name="cube.gcode.3mf",
                        info=SimpleNamespace(plates=[SimpleNamespace(filaments=[fl])]))
    pre = preflight(p1s, f)
    assert pre["tool_map"] == {0: 1}  # PETG is in A2
    assert pre["tools"][0]["label"] == "A2" and pre["ok"] and not pre["warnings"]
    pre = preflight(p1s, f, tool_map={0: 0})
    assert "A1 has PLA" in pre["warnings"][0]


def test_single_head_fallback():
    heads = {254: SimpleNamespace(index=254, type="", loaded=None)}
    fl = SimpleNamespace(slot=1, type="PLA")
    assert default_tool_map([fl], heads) == {0: 254}


def test_calibration_waits_for_finish(p1s, monkeypatch):
    p1s.refresh()
    states = iter(["RUNNING", "RUNNING", "IDLE"])

    def step(payload):
        if payload.get("print", {}).get("command") == "calibration":
            assert payload["print"]["option"] == 2
    p1s.fake.on_publish = step
    orig = p1s.refresh

    def refresh():
        try:
            p1s.on_report({"print": {"gcode_state": next(states)}})
        except StopIteration:
            pass
        return orig()
    monkeypatch.setattr(p1s, "refresh", refresh)
    p1s.run_script(p1s.calibration_script(p1s.calibration("bed_level"), {}))
