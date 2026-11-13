"""Printer control against a fake Moonraker (no real printer needed)."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import DEFAULT_PRINTERS, Settings
from app.main import create_app
from app.printers import PrinterHub
from app.printers.api import same_material
from app.printers.moonraker import MoonrakerPrinter

from test_library import by_name  # noqa: F401  (also loads the demo library helpers)
from test_library import library  # noqa: F401  pytest fixture

U1 = next(p for p in DEFAULT_PRINTERS if p.id == "u1")
JPEG = b"\xff\xd8\xff\xe0fakejpeg"


class FakeMoonraker:
    def __init__(self):
        self.state = "standby"
        self.types = ["PLA", "PETG", "PLA", "TPU"]
        self.exist = [True, True, True, False]
        self.calls: list[str] = []
        self.uploaded: list[tuple[str, int]] = []
        self.started: list[str] = []
        self.deleted: list[str] = []
        self.wakes = 0
        self.light = 0.0
        self.gcode: list[str] = []

    def status(self):
        return {
            "webhooks": {"state": "ready", "state_message": "Printer is ready"},
            "print_stats": {
                "state": self.state,
                "filename": "bin.gcode" if self.state != "standby" else "",
                "print_duration": 600.0,
                "message": "",
                "info": {"current_layer": 12, "total_layer": 80},
            },
            "virtual_sdcard": {"progress": 0.25},
            "display_status": {"progress": 0.25},
            "heater_bed": {"temperature": 60.2, "target": 60.0},
            "led cavity_led": {"color_data": [[0.0, 0.0, 0.0, self.light]]},
            "toolhead": {"extruder": "extruder1"},
            "extruder": {"temperature": 219.6, "target": 220.0},
            "extruder1": {"temperature": 30.0, "target": 0.0},
            "extruder2": {"temperature": 29.0, "target": 0.0},
            "extruder3": {"temperature": 28.0, "target": 0.0},
            "print_task_config": {
                "filament_type": self.types,
                "filament_vendor": ["Snapmaker", "Generic", "Polymaker", ""],
                "filament_color_rgba": ["FF0000FF", "00FF00FF", "0000FFFF", "FFFFFFFF"],
                "filament_exist": self.exist,
            },
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        if path == "/printer/objects/query":
            return httpx.Response(200, json={"result": {"status": self.status()}})
        if path == "/server/files/metadata":
            return httpx.Response(200, json={"result": {"estimated_time": 4000}})
        if path == "/server/files/camera/monitor.jpg":
            return httpx.Response(200, content=JPEG, headers={"Last-Modified": "Thu, 12 Nov 2026 21:00:00 GMT"})
        if path == "/server/files/upload":
            body = request.read()
            assert b'name="print"\r\n\r\nfalse' in body
            self.uploaded.append(("bin.gcode", len(body)))
            return httpx.Response(201, json={"result": {"item": {"path": "bin_2x1_U1_4color.gcode", "root": "gcodes"}}})
        if request.method == "DELETE" and path.startswith("/server/files/gcodes/"):
            self.deleted.append(path.split("/server/files/gcodes/", 1)[1])
            return httpx.Response(200, json={"result": {"item": {}}})
        if path == "/printer/gcode/script":
            script = request.url.params["script"]
            self.gcode.append(script)
            self.light = 1.0 if "WHITE=1" in script else 0.0
            return httpx.Response(200, json={"result": "ok"})
        if path == "/printer/print/start":
            self.started.append(request.url.params["filename"])
            self.state = "printing"
            return httpx.Response(200, json={"result": "ok"})
        if path in ("/printer/print/pause", "/printer/print/cancel", "/printer/print/resume"):
            self.state = {"pause": "paused", "cancel": "cancelled", "resume": "printing"}[path.rsplit("/", 1)[1]]
            return httpx.Response(200, json={"result": "ok"})
        return httpx.Response(404, json={"error": {"code": 404, "message": "Not Found"}})


@pytest.fixture()
def fake():
    return FakeMoonraker()


@pytest.fixture()
def u1(fake):
    def wake():
        fake.wakes += 1

    return MoonrakerPrinter(U1, "http://u1.test:7125", transport=httpx.MockTransport(fake.handler), wake_camera=wake)


@pytest.fixture()
def pclient(tmp_path, library, u1):  # noqa: F811
    settings = Settings(library=library, data=tmp_path / "data", password="", scan_interval=0)
    hub = PrinterHub(settings.printers, env={}, drivers={"u1": u1})
    app = create_app(settings, background_scan=False, printers=hub)
    with TestClient(app) as c:
        yield c


def u1_file(client, model="Gridfinity Bin 2x1"):
    m = client.get(f"/api/models/{by_name(client, model)['id']}").json()
    return next(f for f in m["files"] if f["printer"] == "u1")


# ---------------------------------------------------------------- driver


def test_status_parsing(u1, fake):
    fake.state = "printing"
    st = u1.refresh()
    assert st.online and st.state == "printing"
    assert st.progress == 0.25 and st.layer == 12 and st.total_layers == 80
    assert st.eta_seconds == pytest.approx(3000)
    assert [t.name for t in st.temps] == ["T1", "T2", "T3", "T4", "Bed"]
    assert st.temps[0].target == 220.0
    heads = st.toolheads
    assert [h.type for h in heads] == ["PLA", "PETG", "PLA", "TPU"]
    assert heads[0].color == "#FF0000"
    assert heads[1].active and not heads[0].active
    assert heads[3].loaded is False


def test_offline_when_unreachable():
    def boom(request):
        raise httpx.ConnectError("no route to host")

    p = MoonrakerPrinter(U1, "http://u1.test:7125", transport=httpx.MockTransport(boom), wake_camera=lambda: None)
    st = p.refresh()
    assert not st.online and st.state == "offline" and "no route" in st.message


def test_klippy_error_reported(u1, fake):
    orig = fake.status

    def broken():
        s = orig()
        s["webhooks"] = {"state": "shutdown", "state_message": "MCU 'mcu' shutdown: Timer too close"}
        return s

    fake.status = broken
    st = u1.refresh()
    assert st.state == "error" and "Timer too close" in st.message


def test_snapshot_wakes_camera_once(u1, fake):
    data, taken = u1.snapshot()
    assert data == JPEG and taken and taken > 1.7e9
    u1._snap_at = 0  # expire the short cache
    u1.snapshot()
    assert fake.wakes == 1  # second call within WAKE_EVERY doesn't re-wake


def test_same_material():
    assert same_material("PLA", "PLA+")
    assert same_material("PLA", "PLA SnapSpeed")
    assert same_material("PETG", "")
    assert not same_material("PLA", "PETG")


# ---------------------------------------------------------------- API


def test_list_printers(pclient):
    data = pclient.get("/api/printers").json()["printers"]
    ids = {p["id"]: p for p in data}
    assert ids["u1"]["can_print"] and ids["u1"]["can_camera"]
    assert not ids["p1s"]["configured"] and ids["p1s"]["status"]["state"] == "not_set_up"


def test_snapshot_endpoint(pclient):
    r = pclient.get("/api/printers/u1/snapshot")
    assert r.status_code == 200 and r.content == JPEG
    assert r.headers["content-type"] == "image/jpeg"
    assert "x-frame-time" in r.headers
    assert pclient.get("/api/printers/p1s/snapshot").status_code == 404


def test_check_flags_toolhead_mismatch(pclient):
    f = u1_file(pclient)
    chk = pclient.get("/api/printers/u1/check", params={"file_id": f["id"]}).json()
    assert chk["ok"]
    assert len(chk["tools"]) == len(f["plates"][0]["filaments"])
    # The demo 4-colour file vs. the fake's loaded filaments: T4 (4th head) is empty.
    assert any("T4" in w for w in chk["warnings"])


def test_print_needs_bed_clear_and_ack(pclient, fake):
    f = u1_file(pclient)
    r = pclient.post("/api/printers/u1/print", json={"file_id": f["id"]})
    assert r.status_code == 400
    r = pclient.post("/api/printers/u1/print", json={"file_id": f["id"], "bed_clear": True})
    assert r.status_code == 409 and "toolhead" in r.json()["detail"]
    assert not fake.uploaded


def test_print_uploads_then_starts(pclient, fake):
    fake.types = ["PLA", "PLA", "PLA", "PLA"]
    fake.exist = [True, True, True, True]
    f = u1_file(pclient, "Wrench Holder")
    chk = pclient.get("/api/printers/u1/check", params={"file_id": f["id"]}).json()
    body = {"file_id": f["id"], "bed_clear": True, "accept_warnings": bool(chk["warnings"])}
    r = pclient.post("/api/printers/u1/print", json=body)
    assert r.status_code == 200, r.text
    assert fake.uploaded and fake.started == ["bin_2x1_U1_4color.gcode"]
    order = [c for c in fake.calls if "upload" in c or "start" in c]
    assert order == ["POST /server/files/upload", "POST /printer/print/start"]
    assert r.json()["printer"]["status"]["state"] == "printing"


def test_print_refused_while_busy(pclient, fake):
    fake.state = "printing"
    f = u1_file(pclient)
    r = pclient.post("/api/printers/u1/print", json={"file_id": f["id"], "bed_clear": True, "accept_warnings": True})
    assert r.status_code == 409 and "printing" in r.json()["detail"]
    assert not fake.uploaded


def test_wrong_printer_file_blocked(pclient):
    m = pclient.get(f"/api/models/{by_name(pclient, 'Wrench Holder')['id']}").json()
    p1s_file = next(f for f in m["files"] if f["printer"] == "p1s")
    chk = pclient.get("/api/printers/u1/check", params={"file_id": p1s_file["id"]}).json()
    assert not chk["ok"]


def test_pause_and_cancel(pclient, fake):
    fake.state = "printing"
    assert pclient.post("/api/printers/u1/pause").json()["status"]["state"] == "paused"
    assert pclient.post("/api/printers/u1/cancel").json()["status"]["state"] == "cancelled"
    assert pclient.post("/api/printers/u1/explode").status_code == 404


def test_unconfigured_u1_from_env():
    hub = PrinterHub(DEFAULT_PRINTERS, env={})
    u = hub.get("u1")
    assert not u.configured and "PP_U1_URL" in u.setup_hint
    hub = PrinterHub(DEFAULT_PRINTERS, env={"PP_U1_URL": "http://192.168.1.52:7125"})
    assert isinstance(hub.get("u1"), MoonrakerPrinter)


def test_json_roundtrip(u1):
    u1.refresh()
    json.dumps(u1.to_json())


def test_old_uploads_are_removed(fake, tmp_path):
    ledger = tmp_path / "uploads-u1.json"
    ledger.write_text(json.dumps(["a.gcode", "b c.gcode", "bin_2x1_U1_4color.gcode"]))
    p = MoonrakerPrinter(U1, "http://u1.test:7125", transport=httpx.MockTransport(fake.handler),
                         wake_camera=lambda: None, ledger=ledger, keep_files=2)
    src = tmp_path / "x.gcode"
    src.write_text("G28")
    p.start_print(src, "bin.gcode")
    # Re-uploading a name moves it to the newest end; only our two newest stay.
    assert fake.deleted == ["a.gcode"]
    assert json.loads(ledger.read_text()) == ["b c.gcode", "bin_2x1_U1_4color.gcode"]
    p.forget_old_uploads("new.gcode")
    assert fake.deleted == ["a.gcode", "b c.gcode"]


def test_keep_all_uploads_when_zero(fake, tmp_path):
    ledger = tmp_path / "uploads-u1.json"
    p = MoonrakerPrinter(U1, "http://u1.test:7125", transport=httpx.MockTransport(fake.handler),
                         wake_camera=lambda: None, ledger=ledger, keep_files=0)
    for n in range(4):
        p.forget_old_uploads(f"{n}.gcode")
    assert fake.deleted == [] and len(json.loads(ledger.read_text())) == 4


def test_light_toggle(pclient, fake, u1):
    u1.refresh()
    assert pclient.get("/api/printers/u1").json()["status"]["light"] is False
    r = pclient.post("/api/printers/u1/light_on")
    assert r.status_code == 200 and r.json()["status"]["light"] is True
    assert fake.gcode == ["SET_LED LED=cavity_led WHITE=1"]
    assert pclient.post("/api/printers/u1/light_off").json()["status"]["light"] is False


def test_hand_set_colour_sticks_until_spool_changes(tmp_path, fake, u1):
    u1.load_colors(tmp_path / "colors-u1.json")
    u1.refresh()
    u1.set_color(1, "#ffd400")
    t1 = u1.status.toolheads[1]
    assert t1.color == "#FFD400" and t1.custom_color
    # survives a restart
    again = MoonrakerPrinter(U1, "http://u1.test:7125", transport=httpx.MockTransport(fake.handler), wake_camera=lambda: None)
    again.load_colors(tmp_path / "colors-u1.json")
    assert again.refresh().toolheads[1].color == "#FFD400"
    # a different spool on the printer drops the override
    fake.types = ["PLA", "PETG", "PLA", "TPU"]
    fake.types[1] = "PETG-CF"
    assert not u1.refresh().toolheads[1].custom_color
    assert u1.status.toolheads[1].color == "#00FF00"


def test_colour_api(pclient, u1):
    u1.refresh()
    r = pclient.put("/api/printers/u1/toolheads/0/color", json={"color": "#123456"})
    assert r.status_code == 200 and r.json()["status"]["toolheads"][0]["color"] == "#123456"
    assert pclient.put("/api/printers/u1/toolheads/0/color", json={"color": "yellow"}).status_code == 400
    r = pclient.put("/api/printers/u1/toolheads/0/color", json={"color": None})
    assert r.json()["status"]["toolheads"][0]["custom_color"] is False
