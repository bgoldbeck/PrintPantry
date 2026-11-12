"""Slicer container code (presets, project inspection, service helpers) and the
app's /api/slicer routes. None of this needs OrcaSlicer itself."""

import json
import sys
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "slicer"))

import presets as P  # noqa: E402
import project  # noqa: E402
import service  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.slicing import SlicerClient  # noqa: E402


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


@pytest.fixture()
def profiles(tmp_path):
    """A tiny copy of OrcaSlicer's Snapmaker profile tree."""
    root = tmp_path / "profiles"
    v = root / "Snapmaker"
    write(v / "machine/fdm_klipper.json", {"type": "machine", "name": "fdm_klipper", "instantiation": "false",
                                           "gcode_flavor": "klipper", "nozzle_diameter": ["0.4"]})
    write(v / "machine/fdm_U1.json", {"type": "machine", "name": "fdm_U1", "inherits": "fdm_klipper",
                                      "instantiation": "false", "nozzle_diameter": ["0.4", "0.4", "0.4", "0.4"],
                                      "default_bed_type": "Textured PEI Plate"})
    write(v / "machine/Snapmaker U1 (0.4 nozzle).json", {"type": "machine", "name": "Snapmaker U1 (0.4 nozzle)",
                                                         "inherits": "fdm_U1", "printer_model": "Snapmaker U1"})
    write(v / "machine/Snapmaker A350.json", {"type": "machine", "name": "Snapmaker A350", "printer_model": "Snapmaker A350"})
    write(v / "process/fdm_process_U1.json", {"type": "process", "name": "fdm_process_U1", "instantiation": "false",
                                              "layer_height": "0.2", "wall_loops": "2"})
    for name, lh in (("0.20 Standard @Snapmaker U1 (0.4 nozzle)", "0.2"), ("0.12 Fine @Snapmaker U1 (0.4 nozzle)", "0.12")):
        write(v / f"process/{name}.json", {"type": "process", "name": name, "inherits": "fdm_process_U1",
                                           "layer_height": lh, "compatible_printers": ["Snapmaker U1 (0.4 nozzle)"]})
    write(v / "process/0.20 Standard @A350.json", {"type": "process", "name": "0.20 Standard @A350",
                                                   "compatible_printers": ["Snapmaker A350"]})
    write(v / "filament/fdm_filament_common.json", {"type": "filament", "name": "fdm_filament_common", "instantiation": "false",
                                                    "textured_plate_temp": ["0"], "hot_plate_temp": ["0"]})
    for name, t, textured, hot in (("Snapmaker PLA @U1", "PLA", "0", "55"), ("Snapmaker PETG @U1", "PETG", "70", "70"),
                                   ("Snapmaker PLA Metal @U1", "PLA", "0", "55")):
        write(v / f"filament/{name}.json", {"type": "filament", "name": name, "inherits": "fdm_filament_common",
                                            "filament_type": [t], "textured_plate_temp": [textured],
                                            "hot_plate_temp": [hot], "compatible_printers": ["Snapmaker U1 (0.4 nozzle)"]})
    return root


@pytest.fixture()
def config(tmp_path):
    """User presets: a Snapmaker Orca printer bundle and a loose filament."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    with zipfile.ZipFile(cfg / "U1.orca_printer", "w") as zf:
        zf.writestr("bundle_structure.json", json.dumps({"printer_config": ["printer/My U1.json"]}))
        zf.writestr("printer/My U1.json", json.dumps({"name": "My U1", "from": "User",
                                                      "inherits": "Snapmaker U1 (0.4 nozzle)", "printer_settings_id": "My U1"}))
        # Made in Snapmaker's fork, whose built-in process is spelled "0.20mm".
        zf.writestr("process/My Fine.json", json.dumps({"name": "My Fine", "from": "User", "print_settings_id": "My Fine",
                                                        "inherits": "0.20mm Standard @Snapmaker U1 (0.4 nozzle)",
                                                        "wall_loops": "3"}))
    (cfg / "broken.bbscfg").write_text("not a zip")
    return cfg


@pytest.fixture()
def lib(profiles, config):
    return P.PresetLibrary(profiles, config, vendors=("Snapmaker",)).load()


# ---------------------------------------------------------------- presets

def test_flatten_merges_the_inherit_chain(lib):
    flat = lib.flatten("machine", "Snapmaker U1 (0.4 nozzle)", "Snapmaker")
    assert flat["gcode_flavor"] == "klipper"
    assert flat["nozzle_diameter"] == ["0.4"] * 4  # child wins
    assert flat["printer_model"] == "Snapmaker U1"
    assert flat["inherits"] == "" and flat["type"] == "machine"


def test_user_presets_from_bundles(lib):
    assert ("machine", "My U1") in lib.user
    flat = lib.flatten("machine", "My U1", "Snapmaker")
    assert flat["printer_model"] == "Snapmaker U1" and flat["from"] == "User"
    assert flat["inherits"] == "Snapmaker U1 (0.4 nozzle)"  # the CLI's built-in printer name
    # "0.20mm Standard" (Snapmaker Orca) resolves to "0.20 Standard" (OrcaSlicer).
    proc = lib.flatten("process", "My Fine", "Snapmaker")
    assert proc["wall_loops"] == "3" and proc["layer_height"] == "0.2"
    assert any("broken.bbscfg" in p for p in lib.problems)


def test_user_preset_without_inherits_still_names_a_builtin(profiles, tmp_path):
    """Exported bundles can drop "inherits"; the CLI still needs a built-in name."""
    cfg = tmp_path / "cfg3"
    write(cfg / "printer/Snapmaker U1 (0.4 nozzle).json", {"name": "Snapmaker U1 (0.4 nozzle)", "from": "User",
                                                           "printer_model": "Snapmaker U1", "nozzle_diameter": ["0.4"] * 4})
    write(cfg / "printer/Shop U1.json", {"name": "Shop U1", "from": "User", "printer_model": "Snapmaker U1",
                                         "nozzle_diameter": ["0.4"] * 4})
    lib = P.PresetLibrary(profiles, cfg, vendors=("Snapmaker",)).load()
    for name in ("Snapmaker U1 (0.4 nozzle)", "Shop U1"):
        flat = lib.flatten("machine", name, "Snapmaker")
        assert flat["from"] == "User"
        assert flat["inherits"] == "Snapmaker U1 (0.4 nozzle)"


def test_choices_for_a_printer(lib):
    machines = [m.name for m in lib.machines_for("Snapmaker U1", "Snapmaker")]
    assert sorted(machines) == ["My U1", "Snapmaker U1 (0.4 nozzle)"]
    procs = [p.name for p in lib.compatible("process", "Snapmaker U1 (0.4 nozzle)", "Snapmaker")]
    assert "0.20 Standard @A350" not in procs
    assert "My Fine" in procs
    fils = lib.compatible("filament", "Snapmaker U1 (0.4 nozzle)", "Snapmaker")
    assert lib.pick_filament("PLA", fils, "Snapmaker") == "Snapmaker PLA @U1"
    assert lib.pick_filament("PETG", fils, "Snapmaker") == "Snapmaker PETG @U1"
    assert lib.pick_filament("PLA+", fils, "Snapmaker") == "Snapmaker PLA @U1"
    assert lib.pick_filament("ASA", fils, "Snapmaker") == "Snapmaker PLA @U1"  # unknown: fall back to PLA
    std = [p for p in lib.compatible("process", "Snapmaker U1 (0.4 nozzle)", "Snapmaker") if p.source == "system"]
    assert P.PresetLibrary.pick_process(std) == "0.20 Standard @Snapmaker U1 (0.4 nozzle)"


def test_missing_parent_is_an_error(profiles, tmp_path):
    cfg = tmp_path / "cfg2"
    write(cfg / "process/x.json", {"name": "Orphan", "print_settings_id": "Orphan", "inherits": "Nope"})
    lib = P.PresetLibrary(profiles, cfg, vendors=("Snapmaker",)).load()
    with pytest.raises(P.PresetError, match="Nope"):
        lib.flatten("process", "Orphan", "Snapmaker")


# ---------------------------------------------------------------- project 3MF

def make_3mf(path: Path, settings: dict | None, plates: int = 1) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
        if settings is not None:
            zf.writestr("Metadata/project_settings.config", json.dumps(settings))
        zf.writestr("Metadata/model_settings.config",
                    "<config>" + "".join(f"<plate><metadata key='plater_id' value='{i + 1}'/></plate>" for i in range(plates)) + "</config>")
    return path


def test_inspect_project(tmp_path):
    p = make_3mf(tmp_path / "a.3mf", {
        "printer_model": "Bambu Lab P1S", "printer_settings_id": "Bambu Lab P1S 0.4 nozzle",
        "print_settings_id": "0.20mm Standard @BBL X1C", "curr_bed_type": "Textured PEI Plate",
        "filament_colour": ["#FF0000FF", "#00ff00"], "filament_type": ["PLA", "PETG"],
        "filament_settings_id": ["Bambu PLA Basic @BBL X1C", "Generic PETG"],
    }, plates=3)
    info = project.inspect(p)
    assert info.printer_model == "Bambu Lab P1S"
    assert info.plates == 3
    assert info.filaments == [
        {"type": "PLA", "colour": "#FF0000", "preset": "Bambu PLA Basic @BBL X1C"},
        {"type": "PETG", "colour": "#00FF00", "preset": "Generic PETG"},
    ]
    assert project.inspect(tmp_path / "x.stl").kind == "stl"


# ---------------------------------------------------------------- service

@pytest.fixture()
def svc(monkeypatch, tmp_path, profiles, config):
    library = tmp_path / "library"
    monkeypatch.setattr(service, "LIBRARY", library)
    monkeypatch.setattr(service, "PROFILES", profiles)
    monkeypatch.setattr(service, "CONFIG", config)
    monkeypatch.setattr(service, "PRESETS", service.PresetCache())
    return library


def test_output_names_and_folders(svc):
    u1, p1s = service.PRINTERS["u1"], service.PRINTERS["p1s"]
    src = svc / "Tools" / "Hook" / "3mf" / "hook_P1S.3mf"
    assert service.output_dir(src) == svc / "Tools" / "Hook" / "sliced"
    assert service.output_dir(svc / "Tools" / "loose.stl") == svc / "Tools" / "sliced"
    assert service.output_name(src, u1, None) == "hook_U1.gcode"
    assert service.output_name(src, u1, 2) == "hook_U1_plate_2.gcode"
    assert service.output_name(src, p1s, None) == "hook_P1S.gcode.3mf"
    assert service.output_name(svc / "a" / "box.stl", u1, None) == "box_U1.gcode"


def test_library_path_rejects_escapes_and_wrong_types(svc):
    make_3mf(svc / "Tools" / "a.3mf", {})
    (svc / "Tools" / "a.gcode.3mf").write_bytes(b"x")
    assert service.library_path("Tools/a.3mf").name == "a.3mf"
    for bad in ("../etc/passwd", "/etc/passwd", "Tools/missing.3mf", "Tools/a.gcode.3mf"):
        with pytest.raises(service.BadRequest):
            service.library_path(bad)


def test_options_for_a_p1s_project_on_the_u1(svc):
    make_3mf(svc / "Tools" / "hook.3mf", {
        "printer_model": "Bambu Lab P1S", "filament_colour": ["#FF0000", "#00FF00"],
        "filament_type": ["PLA", "PETG"], "filament_settings_id": ["Bambu PLA Basic @BBL X1C", "Generic PETG"],
    })
    o = service.options("u1", "Tools/hook.3mf")
    assert not o["project_matches"]
    d = o["defaults"]
    assert d["mode"] == "presets"
    assert d["machine"] == "My U1"  # your own preset comes first
    assert d["process"] == "My Fine"
    assert d["filaments"] == ["Snapmaker PLA @U1", "Snapmaker PETG @U1"]
    # PLA has no textured-plate temperature in these profiles, so the smooth plate is picked.
    assert d["bed_type"] == "High Temp Plate"
    assert o["filaments"]["Snapmaker U1 (0.4 nozzle)"][0]["beds"]["High Temp Plate"] in (55, 70)


def test_options_for_a_u1_project(svc):
    make_3mf(svc / "Toys" / "u1.3mf", {
        "printer_model": "Snapmaker U1", "printer_settings_id": "Snapmaker U1 (0.4 nozzle)",
        "print_settings_id": "0.12 Fine @Snapmaker U1 (0.4 nozzle)", "curr_bed_type": "Textured PEI Plate",
        "filament_colour": ["#FFFFFF"], "filament_type": ["PETG"], "filament_settings_id": ["Snapmaker PETG @U1"],
    })
    d = service.options("u1", "Toys/u1.3mf")["defaults"]
    assert d["mode"] == "project"
    assert d["machine"] == "Snapmaker U1 (0.4 nozzle)"
    assert d["process"] == "0.12 Fine @Snapmaker U1 (0.4 nozzle)"
    assert d["filaments"] == ["Snapmaker PETG @U1"] and d["bed_type"] == "Textured PEI Plate"


def test_submit_validates(svc, monkeypatch):
    make_3mf(svc / "Tools" / "hook.3mf", {"printer_model": "Bambu Lab P1S"})
    monkeypatch.setattr(service, "QUEUE", __import__("queue").Queue())
    with pytest.raises(service.BadRequest, match="printer preset"):
        service.submit({"path": "Tools/hook.3mf", "printer": "u1", "mode": "presets"})
    with pytest.raises(service.BadRequest, match="at most 4"):
        service.submit({"path": "Tools/hook.3mf", "printer": "u1", "machine": "m", "process": "p", "filaments": ["f"] * 5})
    job = service.submit({"path": "Tools/hook.3mf", "printer": "u1", "machine": "m", "process": "p", "filaments": ["f"]})
    assert job.state == "queued" and service.QUEUE.get_nowait() == job.id


def test_build_settings_writes_flat_presets(svc, tmp_path):
    job = service.Job(id="j", path="x", printer="u1", mode="presets", machine="Snapmaker U1 (0.4 nozzle)",
                      process="0.20 Standard @Snapmaker U1 (0.4 nozzle)", filaments=["Snapmaker PETG @U1"],
                      bed_type="Textured PEI Plate")
    info = project.ProjectInfo(kind="3mf", filaments=[{"type": "PETG", "colour": "#123456", "preset": ""}])
    work = tmp_path / "work"
    work.mkdir()
    args = service.build_settings(job, service.PRINTERS["u1"], work, info)
    assert args == ["--load-settings", "machine.json;process.json", "--load-filaments", "filament_1.json"]
    proc = json.loads((work / "process.json").read_text())
    assert proc["nozzle_diameter"] == ["0.4"] * 4 and proc["curr_bed_type"] == "Textured PEI Plate"
    fil = json.loads((work / "filament_1.json").read_text())
    assert fil["filament_colour"] == ["#123456"] and fil["inherits"] == ""
    assert not job.warnings
    assert "Snapmaker U1 (0.4 nozzle)" in proc["compatible_printers"]
    # A user printer preset: the process must list its built-in parent, or the
    # CLI refuses to switch a P1S project to it (-16).
    job.machine, job.process = "My U1", "My Fine"
    service.build_settings(job, service.PRINTERS["u1"], work, info)
    proc = json.loads((work / "process.json").read_text())
    assert {"Snapmaker U1 (0.4 nozzle)", "My U1"} <= set(proc["compatible_printers"])
    assert json.loads((work / "machine.json").read_text())["inherits"] == "Snapmaker U1 (0.4 nozzle)"
    # Wrong printer family is refused.
    job.machine = "Snapmaker A350"
    with pytest.raises(RuntimeError, match="not a Snapmaker U1"):
        service.build_settings(job, service.PRINTERS["u1"], work, info)


def test_publish_names_plates(svc, tmp_path):
    src = make_3mf(svc / "Tools" / "Hook" / "hook.3mf", {})
    out = tmp_path / "out"
    out.mkdir()
    for n in (1, 2):
        (out / f"plate_{n}.gcode").write_text(f"plate {n}")
    job = service.Job(id="j", path="Tools/Hook/hook.3mf", printer="u1", mode="project")
    written = service.publish(job, service.PRINTERS["u1"], src, out)
    assert written == ["Tools/Hook/sliced/hook_U1_plate_1.gcode", "Tools/Hook/sliced/hook_U1_plate_2.gcode"]
    assert (svc / written[1]).read_text() == "plate 2"


# ---------------------------------------------------------------- app routes

class FakeSlicer(SlicerClient):
    def __init__(self):
        super().__init__("http://slicer")
        self.calls = []

    def call(self, method, path, **kw):
        self.calls.append((method, path, kw))
        if path == "/health":
            return {"ok": True, "version": "2.4.2", "printers": [{"id": "u1", "name": "Snapmaker U1"}]}
        if path == "/jobs" and method == "POST":
            return {"id": "abc123", "state": "queued"}
        if path.startswith("/jobs/"):
            return {"id": "abc123", "state": "done", "outputs": ["Tools/sliced/x_U1.gcode"]}
        return {"defaults": {}}


@pytest.fixture()
def app_client(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("demo", ROOT / "scripts" / "make_demo_library.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    library = demo.make_demo(tmp_path / "library")
    fake = FakeSlicer()
    settings = Settings(library=library, data=tmp_path / "data", password="", scan_interval=0)
    with TestClient(create_app(settings, background_scan=False, slicer=fake)) as c:
        yield c, fake


def _file(client, kind):
    for m in client.get("/api/models").json()["models"]:
        for f in client.get(f"/api/models/{m['id']}").json()["files"]:
            if f["kind"] == kind:
                return f
    raise AssertionError(kind)


def test_app_forwards_slice_jobs(app_client):
    c, fake = app_client
    assert c.get("/api/slicer").json()["available"] is True
    proj = _file(c, "project3mf")
    res = c.post("/api/slicer/jobs", json={"file_id": proj["id"], "printer": "u1", "mode": "project"})
    assert res.status_code == 200 and res.json()["id"] == "abc123"
    method, path, kw = fake.calls[-1]
    assert (method, path) == ("POST", "/jobs") and kw["json"]["path"] == proj["path"]
    assert c.get("/api/slicer/jobs/abc123").json()["state"] == "done"
    assert c.get(f"/api/slicer/options?file_id={proj['id']}&printer=u1").status_code == 200


def test_app_refuses_unsliceable_files(app_client):
    c, _ = app_client
    sliced = _file(c, "sliced3mf")
    assert c.post("/api/slicer/jobs", json={"file_id": sliced["id"]}).status_code == 400


def test_app_without_slicer(tmp_path):
    settings = Settings(library=tmp_path / "lib", data=tmp_path / "data", password="", scan_interval=0)
    (tmp_path / "lib").mkdir()
    with TestClient(create_app(settings, background_scan=False, slicer=SlicerClient(""))) as c:
        assert c.get("/api/slicer").json() == {"available": False, "reason": "not set up"}


def test_unreachable_slicer_is_reported(tmp_path):
    client = SlicerClient("http://127.0.0.1:9", timeout=0.5)
    with pytest.raises(Exception) as exc:
        client.call("GET", "/health")
    assert "reachable" in str(exc.value.detail)
    assert isinstance(httpx.ConnectError("x"), httpx.HTTPError)


def test_per_extruder_settings_are_padded(tmp_path):
    cfg = {"nozzle_diameter": ["0.4"] * 4, "extruder_type": ["Direct Drive"]}
    assert project.pad_per_extruder(cfg)
    assert cfg["extruder_type"] == ["Direct Drive"] * 4 and cfg["nozzle_volume_type"] == ["Standard"] * 4
    assert not project.pad_per_extruder(cfg)
    src = make_3mf(tmp_path / "u1.3mf", {"printer_model": "Snapmaker U1", "nozzle_diameter": ["0.4"] * 4,
                                          "extruder_type": ["Direct Drive"], "nozzle_volume_type": ["Standard"]})
    project.copy_for_slicing(src, tmp_path / "copy.3mf")
    with zipfile.ZipFile(tmp_path / "copy.3mf") as zf:
        assert json.loads(zf.read(project.SETTINGS))["extruder_type"] == ["Direct Drive"] * 4
        assert "3D/3dmodel.model" in zf.namelist()
    ok = make_3mf(tmp_path / "p1s.3mf", {"nozzle_diameter": ["0.4"], "extruder_type": ["Direct Drive"]})
    project.copy_for_slicing(ok, tmp_path / "same.3mf")
    assert (tmp_path / "same.3mf").read_bytes() == ok.read_bytes()
