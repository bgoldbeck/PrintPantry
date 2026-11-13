"""Calibration: flow test plates (slicer side), saved flow ratios and test prints
(app side), and the printer's own calibrations. No OrcaSlicer or printer needed."""

import json
import sys
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "slicer"))

import calibration as C  # noqa: E402
import service  # noqa: E402

from app.calibration import block_label, preset_file  # noqa: E402
from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.printers import PrinterHub  # noqa: E402

from test_printers import U1, FakeMoonraker  # noqa: E402
from app.printers.moonraker import MoonrakerPrinter  # noqa: E402
from test_slicer import config, profiles, svc  # noqa: E402,F401  pytest fixtures

NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"


def plate(path: Path, names: list[str]) -> Path:
    """A miniature OrcaSlicer flow test plate: one 10 mm square block per name."""
    objs, items = [], []
    for i, name in enumerate(names):
        x = i * 20
        verts = "".join(f'<vertex x="{x + dx}" y="{dy}" z="{dz}"/>' for dz in (0, 2) for dx, dy in ((0, 0), (10, 0), (10, 10), (0, 10)))
        objs.append(f'<object id="{2 * i + 1}" name="{name}" type="model"><mesh><vertices>{verts}</vertices>'
                    f'<triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object>')
        items.append(f'<item objectid="{2 * i + 1}"/>')
    model = (f'<?xml version="1.0" encoding="utf-8"?><model xmlns="{NS}" unit="millimeter"><resources>{"".join(objs)}'
             f'</resources><build>{"".join(items)}</build></model>')
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", model)
        z.writestr("[Content_Types].xml", "<Types/>")
    return path


# ---------------------------------------------------------------- slicer side

def test_block_ratios():
    assert C.block_ratio("flowrate_m5", linear=False) == 0.95
    assert C.block_ratio("flowrate_20", linear=False) == 1.2
    assert C.block_ratio("flowrate_m0.01", linear=True) == 0.99
    assert C.block_ratio("flowrate_0.035", linear=True) == 1.035
    assert C.block_ratio("flowrate_0", linear=True) == 1.0
    assert C.block_ratio("text_label", linear=True) is None


def test_build_flow_project_sets_per_block_flow_and_centres(tmp_path):
    src = plate(tmp_path / "Orca-LinearFlow.3mf", ["flowrate_0", "flowrate_m0.01", "flowrate_0.01"])
    dst = tmp_path / "out.3mf"
    blocks = C.build_flow_project(src, dst, C.FLOW_TESTS["yolo"], 1, center=(135, 135))
    assert [b["ratio"] for b in blocks] == [0.99, 1.0, 1.01]
    with zipfile.ZipFile(dst) as z:
        cfg = z.read("Metadata/model_settings.config").decode()
        model = z.read("3D/3dmodel.model").decode()
    assert 'key="print_flow_ratio" value="0.99"' in cfg
    assert 'key="top_surface_pattern" value="monotonic"' in cfg
    assert 'key="extruder" value="1"' in cfg
    # Blocks span x 0..50, y 0..10: moved so the middle (25, 5) lands on (135, 135).
    assert 'transform="1 0 0 0 1 0 0 0 1 110.0000 130.0000 0"' in model


def test_speeds_respect_volumetric_limit():
    s = C.speeds(0.4, 0.2, 12, {"internal_solid_infill_speed": ["250"], "top_surface_speed": "100"}, coarse=False)
    assert int(s["top_surface_speed"]) == 100  # already slow enough
    assert int(s["internal_solid_infill_speed"]) == int(12 / C.mm3_per_mm(0.48, 0.2))


def test_bed_center():
    assert C.bed_center({"printable_area": ["0x0", "270x0", "270x270", "0x270"]}) == (135, 135)
    assert C.bed_center({}) == (128.0, 128.0)


def test_retarget_moves_objects_and_fills_filament_lists(tmp_path):
    src = tmp_path / "proj.3mf"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("Metadata/model_settings.config",
                   '<config><object id="1"><metadata key="extruder" value="1"/></object></config>')
        z.writestr("Metadata/project_settings.config", json.dumps({
            "filament_settings_id": ["PLA"] * 4, "filament_colour": ["#FFF"], "nozzle_temperature": ["220"],
            "nozzle_diameter": ["0.4"] * 4, "layer_height": "0.2"}))
    C.retarget(src, tmp_path / "out.3mf", 3, ["nozzle_temperature"])
    with zipfile.ZipFile(tmp_path / "out.3mf") as z:
        assert 'value="3"' in z.read("Metadata/model_settings.config").decode()
        cfg = json.loads(z.read("Metadata/project_settings.config"))
    assert cfg["filament_colour"] == ["#FFF"] * 4 and cfg["nozzle_temperature"] == ["220"] * 4
    assert cfg["layer_height"] == "0.2"
    with pytest.raises(C.CalibrationError):
        C.retarget(src, tmp_path / "x.3mf", 5, [])


def test_submit_flow_validates(svc, monkeypatch, profiles):  # noqa: F811
    monkeypatch.setattr(service, "QUEUE", __import__("queue").Queue())
    plate(profiles.parent / "calib" / "filament_flow" / "Orca-LinearFlow.3mf", ["flowrate_0"])
    base = {"printer": "u1", "machine": "m", "process": "p", "filament": "Snapmaker PLA @U1"}
    with pytest.raises(service.BadRequest, match="T5"):
        service.submit_flow({**base, "toolhead": 5})
    with pytest.raises(service.BadRequest, match="Unknown flow test"):
        service.submit_flow({**base, "test": "nope"})
    with pytest.raises(service.BadRequest, match="between"):
        service.submit_flow({**base, "flow_ratio": 3})
    with pytest.raises(service.BadRequest, match="no flowrate-test-pass1"):
        service.submit_flow({**base, "test": "coarse"})
    job = service.submit_flow({**base, "toolhead": 2, "flow_ratio": 0.97})
    assert job.calib["toolhead"] == 2 and job.calib["base"] == 0.97 and job.calib["linear"]
    assert service.QUEUE.get_nowait() == job.id


def test_saved_flow_ratio_goes_into_the_filament(svc, tmp_path):  # noqa: F811
    job = service.Job(id="j", path="x", printer="u1", mode="presets", machine="Snapmaker U1 (0.4 nozzle)",
                      process="0.20 Standard @Snapmaker U1 (0.4 nozzle)", filaments=["Snapmaker PETG @U1"],
                      flow_ratios=service.flow_ratios({"Snapmaker PETG @U1": "0.9731", "bad": 7}))
    assert job.flow_ratios == {"Snapmaker PETG @U1": 0.9731}
    work = tmp_path / "w"
    work.mkdir()
    service.build_settings(job, service.PRINTERS["u1"], work, service.project.ProjectInfo(kind="3mf"))
    assert json.loads((work / "filament_1.json").read_text())["filament_flow_ratio"] == ["0.9731"]
    assert "0.9731 (calibrated)" in job.log


# ---------------------------------------------------------------- app side

class FakeSlicer:
    enabled = True

    def __init__(self, test_file: str):
        self.calls = []
        self.test_file = test_file

    def call(self, method, path, **kw):
        self.calls.append((method, path, kw))
        if path == "/calibration/flow" and method == "GET":
            return {"tests": [{"id": "yolo", "label": "Standard", "hint": "", "available": True}]}
        if path == "/calibration/flow":
            return {"id": "f00d", "state": "queued"}
        if path == "/options":
            return {"printer": {"id": "u1"}, "machines": [], "processes": {}, "filaments": {}, "defaults": {},
                    "bed_types": [], "problems": []}
        if path == "/jobs/f00d":
            blocks = [{"id": str(i), "name": n, "ratio": r, "flow": round(0.98 * r, 4)}
                      for i, (n, r) in enumerate((("flowrate_m0.01", 0.99), ("flowrate_0", 1.0), ("flowrate_0.01", 1.01)))]
            return {"id": "f00d", "state": "done", "printer": "u1", "outputs": [self.test_file],
                    "calib": {"kind": "flow", "test": "yolo", "label": "Standard", "linear": True, "toolhead": 2,
                              "filament": "Snapmaker PLA @U1", "type": "PLA", "base": 0.98, "blocks": blocks}}
        if path == "/jobs" and method == "POST":
            return {"id": "abc", "state": "queued"}
        return {}


@pytest.fixture()
def fake():
    return FakeMoonraker()


@pytest.fixture()
def calib(tmp_path, fake):
    library = tmp_path / "library"
    test_file = ".calibration/u1/flow-yolo-T2-PLA_U1.gcode"
    (library / test_file).parent.mkdir(parents=True)
    (library / test_file).write_text("G28\n")
    u1 = MoonrakerPrinter(U1, "http://u1.test:7125", transport=httpx.MockTransport(fake.handler), wake_camera=lambda: None)
    settings = Settings(library=library, data=tmp_path / "data", password="", scan_interval=0)
    hub = PrinterHub(settings.printers, env={}, drivers={"u1": u1})
    slicer = FakeSlicer(test_file)
    with TestClient(create_app(settings, background_scan=False, printers=hub, slicer=slicer)) as c:
        yield c, slicer, fake, u1


def test_flow_wizard_end_to_end(calib):
    c, slicer, fake, _ = calib
    assert c.get("/api/calibration").json()["slicer"] is True
    assert c.get("/api/calibration/flow/options?printer=u1").status_code == 200
    job = c.post("/api/calibration/flow/jobs", json={"printer": "u1", "toolhead": 2, "machine": "m", "process": "p",
                                                     "filament": "Snapmaker PLA @U1"}).json()
    assert slicer.calls[-1][2]["json"]["flow_ratio"] is None  # nothing saved yet
    done = c.get(f"/api/calibration/flow/jobs/{job['id']}").json()
    test = done["test"]
    assert [b["label"] for b in test["blocks"]] == ["-0.01", "0", "+0.01"]
    assert c.get("/api/calibration").json()["tests"][0]["id"] == "f00d"

    # T2 has PETG loaded in the fake printer, the test is PLA: a warning to tick.
    chk = c.get("/api/calibration/flow/tests/f00d/check").json()
    assert not chk["blockers"] and "T2 has PETG" in chk["warnings"][0]
    assert c.post("/api/calibration/flow/tests/f00d/print", json={"bed_clear": False}).status_code == 400
    assert c.post("/api/calibration/flow/tests/f00d/print", json={"bed_clear": True}).status_code == 409
    res = c.post("/api/calibration/flow/tests/f00d/print", json={"bed_clear": True, "accept_warnings": True})
    assert res.status_code == 200 and res.json()["test"]["printed"]
    assert fake.started

    picked = c.post("/api/calibration/flow/pick", json={"test_id": "f00d", "block": "flowrate_0.01"}).json()
    assert picked["saved"]["ratio"] == round(0.98 * 1.01, 4) and picked["edge"] is True
    flow = c.get("/api/calibration").json()["flow"]
    assert flow[0]["filament"] == "Snapmaker PLA @U1" and flow[0]["block"] == "+0.01"

    # The next test starts from the saved ratio, and slicing passes it on.
    c.post("/api/calibration/flow/jobs", json={"printer": "u1", "machine": "m", "process": "p", "filament": "Snapmaker PLA @U1"})
    assert slicer.calls[-1][2]["json"]["flow_ratio"] == round(0.98 * 1.01, 4)

    preset = c.get("/api/calibration/flow/preset?printer=u1&filament=Snapmaker PLA @U1")
    assert preset.status_code == 200 and "attachment" in preset.headers["content-disposition"]
    assert preset.json()["filament_flow_ratio"] == [f"{round(0.98 * 1.01, 4):g}"]

    assert c.put("/api/calibration/flow/ratio", json={"printer": "u1", "filament": "X", "ratio": 2}).status_code == 400
    c.put("/api/calibration/flow/ratio", json={"printer": "u1", "filament": "Snapmaker PLA @U1", "ratio": 0.95})
    flow = c.get("/api/calibration").json()["flow"]
    assert flow[0]["ratio"] == 0.95 and flow[0]["history"][0]["ratio"] == round(0.98 * 1.01, 4)
    c.delete("/api/calibration/flow/ratio?printer=u1&filament=Snapmaker PLA @U1")
    assert c.get("/api/calibration").json()["flow"] == []


def test_slice_jobs_carry_saved_flow_ratios(calib, tmp_path):
    from app.calibration import CalibrationStore

    c, slicer, _, _ = calib
    c.put("/api/calibration/flow/ratio", json={"printer": "u1", "filament": "Snapmaker PLA @U1", "ratio": 0.96})
    store = CalibrationStore(tmp_path / "data" / "calibration.json")
    assert store.flow_ratios("u1") == {"Snapmaker PLA @U1": 0.96}
    assert store.flow_ratios("p1s") == {}


def test_test_file_must_stay_in_calibration_folder(calib):
    c, _, _, _ = calib
    assert c.get("/api/calibration/flow/tests/nope/check").status_code == 404


def test_block_labels_and_preset():
    assert block_label(0.99, True) == "-0.01" and block_label(1.035, True) == "+0.035"
    assert block_label(0.95, False) == "-5" and block_label(1.0, False) == "0"
    p = preset_file("Snapmaker PLA @U1", 0.97)
    assert p["inherits"] == "Snapmaker PLA @U1" and p["filament_flow_ratio"] == ["0.97"]


# ---------------------------------------------------------------- printer calibrations

def test_printer_calibration_needs_confirmation_and_idle(calib):
    c, _, fake, u1 = calib
    cals = c.get("/api/printers/u1").json()["calibrations"]
    assert {x["id"] for x in cals} == {"bed_mesh", "input_shaper", "toolhead_offsets"}
    assert c.post("/api/printers/u1/calibrations/bed_mesh", json={}).status_code == 400
    assert c.post("/api/printers/u1/calibrations/nope", json={"confirmed": True}).status_code == 409
    assert c.post("/api/printers/u1/calibrations/bed_mesh",
                  json={"confirmed": True, "params": {"bed": 500}}).status_code == 409
    res = c.post("/api/printers/u1/calibrations/bed_mesh", json={"confirmed": True, "params": {"bed": 65}})
    assert res.status_code == 200
    u1_thread_wait(u1)
    assert any("AUTO_BED_MESH_CALIBRATE TEMP=65" in g for g in fake.gcode)
    assert u1.calibrating["state"] == "done"

    fake.state = "printing"
    assert c.post("/api/printers/u1/calibrations/input_shaper", json={"confirmed": True}).status_code == 409


def test_print_blocked_while_calibrating(calib):
    c, _, fake, u1 = calib
    u1.calibrating = {"id": "bed_mesh", "name": "Bed mesh", "state": "running"}
    chk = c.get("/api/calibration/flow/jobs/f00d")  # registers the test
    assert chk.status_code == 200
    pre = c.get("/api/calibration/flow/tests/f00d/check").json()
    assert any("Bed mesh" in b for b in pre["blockers"])


def u1_thread_wait(u1, timeout=5.0):
    import time

    end = time.time() + timeout
    while u1.calibration_running and time.time() < end:
        time.sleep(0.02)
