"""Printer settings: each change is sent, then read back from the (fake) printer."""

import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import DEFAULT_PRINTERS, Settings
from app.main import create_app
from app.printers import PrinterError, PrinterHub
from app.printers.bambu import FLAG_AMS_REFILL, FLAG_STEP_LOSS
from app.printers.moonraker import MoonrakerPrinter

from test_bambu import p1s, report  # noqa: F401 - fixture
from test_library import library  # noqa: F401 - fixture

U1 = next(p for p in DEFAULT_PRINTERS if p.id == "u1")


class FakeU1:
    """Just enough of the U1's Klipper to answer the settings query and the commands that change it."""

    def __init__(self):
        self.dd = {"main_enable": True, "clean_bed": {"enable": True, "sensitivity": "low"},
                   "noodle": {"enable": True, "sensitivity": "low"}, "residue": {"enable": False, "sensitivity": "low"}}
        self.tc = {"auto_replenish_filament": True, "filament_entangle_detect": True, "filament_entangle_sen": "medium"}
        self.sensors = {f"e{i}_filament": True for i in range(4)}
        self.speed, self.flow, self.fan = 1.0, 1.0, 0.0
        self.scripts: list[str] = []
        self.ignore = False

    def status(self):
        st = {"defect_detection": self.dd, "print_task_config": self.tc, "print_stats": {"state": "standby"},
              "gcode_move": {"speed_factor": self.speed, "extrude_factor": self.flow},
              "fan_generic cavity_fan": {"speed": self.fan, "rpm": 0.0}}
        for n, on in self.sensors.items():
            st[f"filament_motion_sensor {n}"] = {"filament_detected": True, "enabled": on}
        return st

    def run(self, script):
        self.scripts.append(script)
        if self.ignore:
            return
        for line in script.split("\n"):
            cmd, *args = line.split()
            kv = dict(a.split("=", 1) for a in args if "=" in a)
            if cmd == "DEFECT_DETECTION_CONFIG":
                for k, v in kv.items():
                    if k == "MAIN_ENABLE":
                        self.dd["main_enable"] = v == "1"
                    else:
                        part, field = re.match(r"(CLEAN_BED|NOODLE|RESIDUE)_(ENABLE|SENSITIVITY)", k).groups()
                        self.dd[part.lower()][field.lower()] = (v == "1") if field == "ENABLE" else v
            elif cmd == "SET_PRINT_PREFERENCES":
                names = {"AUTO_REPLENISH_FILAMENT": "auto_replenish_filament",
                         "FILAMENT_ENTANGLE_DETECT": "filament_entangle_detect",
                         "FILAMENT_ENTANGLE_SEN": "filament_entangle_sen"}
                for k, v in kv.items():
                    self.tc[names[k]] = v if k.endswith("SEN") else v == "1"
            elif cmd == "SET_FILAMENT_SENSOR":
                self.sensors[kv["SENSOR"]] = kv["ENABLE"] == "1"
            elif cmd == "M220":
                self.speed = int(args[0][1:]) / 100
            elif cmd == "M221":
                self.flow = int(args[0][1:]) / 100
            elif cmd == "SET_FAN_SPEED":
                self.fan = float(kv["SPEED"])

    def handler(self, request):
        if request.url.path == "/printer/objects/query":
            return httpx.Response(200, json={"result": {"status": self.status()}})
        if request.url.path == "/printer/gcode/script":
            self.run(request.url.params["script"])
            return httpx.Response(200, json={"result": "ok"})
        return httpx.Response(404, json={"error": {"message": "Not Found"}})


@pytest.fixture
def fu1():
    return FakeU1()


@pytest.fixture
def u1s(fu1, monkeypatch):
    p = MoonrakerPrinter(U1, "http://u1.test:7125", transport=httpx.MockTransport(fu1.handler), wake_camera=lambda: None)
    monkeypatch.setattr(p, "_settings_wait", lambda: None)
    return p


def by_id(items):
    return {s.id if hasattr(s, "id") else s["id"]: s for s in items}


def test_u1_reads_settings(u1s):
    s = by_id(u1s.settings())
    assert s["ai_main"].value is True and s["ai_noodle"].value is True
    assert s["ai_noodle_sens"].value == "low" and s["ai_residue"].value is False
    assert s["runout"].value is True and s["speed"].value == 100 and s["cavity_fan"].value == 0


def test_u1_spaghetti_off_and_back(u1s, fu1):
    s = by_id(u1s.set_setting("ai_noodle", False))
    assert s["ai_noodle"].value is False
    assert fu1.scripts[-1] == "DEFECT_DETECTION_CONFIG NOODLE_ENABLE=0"
    u1s.set_setting("ai_noodle_sens", "high")
    assert fu1.dd["noodle"]["sensitivity"] == "high"


def test_u1_other_settings(u1s, fu1):
    u1s.set_setting("tangle_sens", "high")
    u1s.set_setting("auto_refill", False)
    u1s.set_setting("runout", False)
    u1s.set_setting("speed", 120)
    u1s.set_setting("flow", 95)
    u1s.set_setting("cavity_fan", 50)
    assert fu1.tc["filament_entangle_sen"] == "high" and fu1.tc["auto_replenish_filament"] is False
    assert not any(fu1.sensors.values())
    assert (fu1.speed, fu1.flow, fu1.fan) == (1.2, 0.95, 0.5)
    assert "SET_FAN_SPEED FAN=cavity_fan SPEED=0.50" in fu1.scripts


def test_u1_rejects_bad_values(u1s, fu1):
    with pytest.raises(PrinterError, match="between 50 and 200"):
        u1s.set_setting("speed", 500)
    with pytest.raises(PrinterError, match="one of"):
        u1s.set_setting("ai_noodle_sens", "medium")
    with pytest.raises(PrinterError, match="on or off"):
        u1s.set_setting("ai_main", "yes")
    fu1.dd["main_enable"] = False
    with pytest.raises(PrinterError, match="Turn on AI detection first"):
        u1s.set_setting("ai_noodle", False)
    assert fu1.scripts == []


def test_u1_unconfirmed_change_is_an_error(u1s, fu1):
    fu1.ignore = True
    with pytest.raises(PrinterError, match="didn't confirm"):
        u1s.set_setting("auto_refill", False, wait=0)


def test_settings_api(tmp_path, library, u1s, fu1):  # noqa: F811
    settings = Settings(library=library, data=tmp_path / "data", password="", scan_interval=0)
    hub = PrinterHub(settings.printers, env={}, drivers={"u1": u1s})
    with TestClient(create_app(settings, background_scan=False, printers=hub)) as c:
        printers = {p["id"]: p for p in c.get("/api/printers").json()["printers"]}
        assert printers["u1"]["has_settings"] is True
        got = by_id(c.get("/api/printers/u1/settings").json()["settings"])
        assert got["ai_noodle"]["value"] is True and got["ai_noodle"]["group"] == "AI camera detection"
        r = c.put("/api/printers/u1/settings/ai_residue", json={"value": True})
        assert r.status_code == 200 and by_id(r.json()["settings"])["ai_residue"]["value"] is True
        r = c.put("/api/printers/u1/settings/speed", json={"value": 120})
        assert by_id(r.json()["settings"])["speed"]["value"] == 120
        r = c.put("/api/printers/u1/settings/speed", json={"value": 900})
        assert r.status_code == 409 and "between" in r.json()["detail"]
        assert c.put("/api/printers/u1/settings/nope", json={"value": True}).status_code == 409


# ---------------------------------------------------------------- P1S
class P1SFirmware:
    """Answers settings commands the way Brandon's P1S did (Nov 2026)."""

    def __init__(self, drv, flag=FLAG_STEP_LOSS | FLAG_AMS_REFILL | (1 << 7)):
        self.drv = drv
        st = drv.fake.state["print"]
        st["home_flag"] = flag
        st["ams"].update(insert_flag=True, power_on_flag=True)
        st["ipcam"] = {"ipcam_record": "enable"}
        st["spd_lvl"] = 2
        self.answer_xcam = True
        drv.fake.on_publish = self.handle

    def handle(self, payload):
        st = self.drv.fake.state["print"]
        if "xcam" in payload and self.answer_xcam:
            self.drv.on_report({"xcam": {**payload["xcam"], "result": "SUCCESS"}})
        p = payload.get("print", {})
        cmd = p.get("command")
        if cmd == "print_option" and "auto_recovery" in p:
            st["home_flag"] = (st["home_flag"] & ~FLAG_STEP_LOSS) | (FLAG_STEP_LOSS if p["auto_recovery"] else 0)
        elif cmd == "print_option" and "auto_switch_filament" in p:
            st["home_flag"] = (st["home_flag"] & ~FLAG_AMS_REFILL) | (FLAG_AMS_REFILL if p["auto_switch_filament"] else 0)
        elif cmd == "ams_user_setting":
            st["ams"].update(insert_flag=p["tray_read_option"], power_on_flag=p["startup_read_option"])
            st["home_flag"] = (st["home_flag"] & ~(1 << 7)) | ((1 << 7) if p["calibrate_remain_flag"] else 0)
        elif cmd == "print_speed":
            st["spd_lvl"] = int(p["param"])
        if "camera" in payload:
            st["ipcam"]["ipcam_record"] = payload["camera"]["control"]
        # The P1S only sends the change with its next report; pushall in the driver fetches it.


@pytest.fixture
def fw(p1s, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr("app.printers.base.time.sleep", lambda s: None)
    p1s.load_saved_settings(tmp_path / "settings-p1s.json")
    return P1SFirmware(p1s)


def test_p1s_reads_settings(p1s, fw):  # noqa: F811
    s = by_id(p1s.settings())
    assert s["ai_monitor"].value is None and "doesn't report" in s["ai_monitor"].note
    assert s["step_loss"].value is True and s["ams_refill"].value is True
    assert s["ams_read_insert"].value is True and s["camera_record"].value is True
    assert s["speed"].disabled == "Only while printing"


def test_p1s_spaghetti_detection_on(p1s, fw, tmp_path):  # noqa: F811
    s = by_id(p1s.set_setting("ai_monitor", True))
    assert s["ai_monitor"].value is True and s["ai_sensitivity"].value == "medium"
    sent = [m["xcam"] for m in p1s.fake.sent if "xcam" in m][-1]
    assert sent["module_name"] == "printing_monitor" and sent["control"] is True and sent["print_halt"] is True
    assert json.loads((tmp_path / "settings-p1s.json").read_text())["printing_monitor"] is True
    s = by_id(p1s.set_setting("ai_sensitivity", "high"))
    assert s["ai_sensitivity"].value == "high"


def test_p1s_no_xcam_answer(p1s, fw, monkeypatch):  # noqa: F811
    fw.answer_xcam = False
    monkeypatch.setattr("app.printers.bambu.XCAM_ACK_WAIT", 0.01)
    with pytest.raises(PrinterError, match="didn't answer"):
        p1s.set_setting("ai_monitor", True)


def test_p1s_flags_and_ams(p1s, fw):  # noqa: F811
    s = by_id(p1s.set_setting("step_loss", False))
    assert s["step_loss"].value is False
    s = by_id(p1s.set_setting("ams_refill", False))
    assert s["ams_refill"].value is False
    s = by_id(p1s.set_setting("ams_read_insert", False))
    assert s["ams_read_insert"].value is False and s["ams_read_startup"].value is True and s["ams_remain"].value is True
    ams = [m["print"] for m in p1s.fake.sent if m.get("print", {}).get("command") == "ams_user_setting"][-1]
    assert ams == {"command": "ams_user_setting", "ams_id": -1, "startup_read_option": True,
                   "tray_read_option": False, "calibrate_remain_flag": True, "sequence_id": ams["sequence_id"]}
    s = by_id(p1s.set_setting("camera_record", False))
    assert s["camera_record"].value is False


def test_p1s_speed_only_while_printing(p1s, fw):  # noqa: F811
    with pytest.raises(PrinterError, match="Only while printing"):
        p1s.set_setting("speed", 3)
    p1s.fake.state["print"]["gcode_state"] = "RUNNING"
    p1s.fake.state["print"]["subtask_name"] = "cube.gcode.3mf"
    p1s.on_report(p1s.fake.state)
    s = by_id(p1s.set_setting("speed", 3))
    assert s["speed"].value == 3
