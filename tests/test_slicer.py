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


def test_bambu_export_strings_become_lists(profiles, tmp_path):
    """Bambu Studio exports some list settings as "" (e.g. filament_notes);
    OrcaSlicer reads that as an empty list and aborts (invalid size found)."""
    write(profiles / "Snapmaker/filament/Snapmaker PLA @U1.json",
          {"type": "filament", "name": "Snapmaker PLA @U1", "filament_type": ["PLA"], "filament_notes": [""],
           "compatible_printers": ["Snapmaker U1 (0.4 nozzle)"]})
    cfg = tmp_path / "cfg4"
    write(cfg / "filament/Shop PLA.json", {"name": "Shop PLA", "from": "User", "inherits": "Snapmaker PLA @U1",
                                           "filament_notes": "", "filament_type": ["PLA"], "note_text": "x"})
    lib = P.PresetLibrary(profiles, cfg, vendors=("Snapmaker",)).load()
    flat = lib.flatten("filament", "Shop PLA", "Snapmaker")
    assert flat["filament_notes"] == [""]
    assert flat["note_text"] == "x"  # not a list in the built-ins: left alone


def test_filament_keys_only_some_have_are_dropped():
    fils = [{"name": "a", "filament_type": ["PLA"], "filament_cooling_before_tower": ["0"]},
            {"name": "b", "filament_type": ["PETG"]}]
    assert service.common_filament_keys(fils) == ["filament_cooling_before_tower"]
    assert all(set(f) == {"name", "filament_type"} for f in fils)
    one = [{"name": "a", "x": ["1"]}]
    assert service.common_filament_keys(one) == [] and one[0]["x"] == ["1"]


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
    # PLA has no textured-plate temperature in these profiles; it borrows the smooth plate's, so the textured default stays.
    assert d["bed_type"] == "Textured PEI Plate"
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


def test_full_reslice_replaces_outdated_outputs(svc, tmp_path):
    src = make_3mf(svc / "Tools" / "Hook" / "hook.3mf", {})
    sliced = svc / "Tools" / "Hook" / "sliced"
    sliced.mkdir(parents=True)
    for name in ("hook_U1_plate_1.gcode", "hook_U1_plate_3.gcode", "hook_P1S.gcode.3mf", "other_U1.gcode"):
        (sliced / name).write_text("old")
    out = tmp_path / "out"
    out.mkdir()
    (out / "plate_1.gcode").write_text("new")
    job = service.Job(id="j", path="Tools/Hook/hook.3mf", printer="u1", mode="project")
    service.publish(job, service.PRINTERS["u1"], src, out)
    assert sorted(p.name for p in sliced.iterdir()) == ["hook_P1S.gcode.3mf", "hook_U1.gcode", "other_U1.gcode"]

    # Slicing one plate leaves the other outputs alone.
    (sliced / "hook_U1_plate_2.gcode").write_text("old")
    (out / "plate_1.gcode").rename(out / "plate_3.gcode")
    job.plate = 3
    service.publish(job, service.PRINTERS["u1"], src, out)
    assert {"hook_U1.gcode", "hook_U1_plate_2.gcode", "hook_U1_plate_3.gcode"} <= {p.name for p in sliced.iterdir()}


def test_sweep_work_removes_leftovers(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "WORK", tmp_path / "pp-slicer")
    (service.WORK / "abc-123" / "out").mkdir(parents=True)
    service.sweep_work()
    assert list(service.WORK.iterdir()) == []


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
        if path == "/objects":
            return {"items": [{"key": "f0"}], "overlapping": False}
        return {"defaults": {}}

    def raw(self, method, path, **kw):
        self.calls.append((method, path, kw))
        return b"\0\0\0\0"


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


def test_rejected_bambu_values_are_dropped(tmp_path):
    err = ("Param values in 3mf/config error: \n"
           "raft_first_layer_expansion: -1 not in range [0.000000,3.4e38]\n"
           "tree_support_wall_count: -1 not in range [0,2]\n")
    assert service.invalid_keys(err) == ["raft_first_layer_expansion", "tree_support_wall_count"]
    path = tmp_path / "p.3mf"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(project.SETTINGS, json.dumps({"raft_first_layer_expansion": "-1", "layer_height": "0.2"}))
        zf.writestr("3D/3dmodel.model", "<model/>")
    assert project.drop_keys(path, ["raft_first_layer_expansion", "nope"]) == ["raft_first_layer_expansion"]
    with zipfile.ZipFile(path) as zf:
        assert json.loads(zf.read(project.SETTINGS)) == {"layer_height": "0.2"}
        assert zf.read("3D/3dmodel.model") == b"<model/>"
    assert project.drop_keys(path, ["nope"]) == []


def test_process_preset_is_written_into_the_project():
    config = {"wall_loops": "2", "print_settings_id": "0.20mm Standard @BBL X1C",
              "different_settings_to_system": ["wall_loops", "", ""]}
    project.apply_process(config, {"name": "Standard Quality", "wall_loops": "3", "inherits": "x"})
    assert config["wall_loops"] == "3" and "name" not in config and "inherits" not in config
    assert config["print_settings_id"] == "0.20mm Standard @BBL X1C"
    assert config["different_settings_to_system"] == ["", "", ""]


def test_fill_plate_temps():
    fil = {"filament_type": ["PLA"], "textured_plate_temp": ["0"], "hot_plate_temp": ["55"],
           "hot_plate_temp_initial_layer": ["60"]}
    filled = service.fill_plate_temps(fil)
    assert fil["textured_plate_temp"] == ["55"] and fil["textured_plate_temp_initial_layer"] == ["60"]
    assert "hot_plate_temp" not in filled
    bare = {"filament_type": ["PETG"]}
    service.fill_plate_temps(bare)
    assert bare["textured_plate_temp"] == ["70"]


def test_app_objects_and_stls_together(app_client):
    c, fake = app_client
    stl = _file(c, "stl")
    model = next(m for m in c.get("/api/models").json()["models"]
                 if any(f["id"] == stl["id"] for f in c.get(f"/api/models/{m['id']}").json()["files"]))
    others = [f for f in c.get(f"/api/models/{model['id']}").json()["files"] if f["kind"] == "stl" and f["id"] != stl["id"]]
    extra = ",".join(f["id"] for f in others)
    assert c.get(f"/api/slicer/objects?file_id={stl['id']}&extra={extra}").json()["items"][0]["key"] == "f0"
    assert fake.calls[-1][2]["json"]["paths"][0] == stl["path"]
    assert c.get(f"/api/slicer/objects/mesh?file_id={stl['id']}").content == b"\0\0\0\0"
    res = c.post("/api/slicer/jobs", json={"file_id": stl["id"], "printer": "u1", "filaments": ["A", "B"],
                                            "assign": {"f0": 2}, "extra_files": [f["id"] for f in others]})
    assert res.status_code == 200
    body = fake.calls[-1][2]["json"]
    assert body["assign"] == {"f0": 2} and "extra_files" not in body
    assert body.get("extra_paths", []) == [f["path"] for f in others]
    proj = _file(c, "project3mf")
    assert c.get(f"/api/slicer/objects?file_id={proj['id']}&extra={stl['id']}").status_code == 200
    assert c.get(f"/api/slicer/objects?file_id={stl['id']}&extra={proj['id']}").status_code == 400


def test_delete_files(app_client):
    c, _ = app_client
    sliced = _file(c, "sliced3mf")
    stl = _file(c, "stl")
    assert c.delete(f"/api/files/{stl['id']}").status_code == 200
    assert c.delete(f"/api/files/{sliced['id']}").status_code == 200
    assert c.delete(f"/api/files/{sliced['id']}").status_code == 404


def test_replace_stl_keeps_id_and_name(app_client):
    import struct

    c, _ = app_client
    stl = _file(c, "stl")
    tri = struct.pack("<12fH", *([0.0] * 3), 0, 0, 0, 10, 0, 0, 0, 10, 0, 0)
    body = b"\0" * 80 + struct.pack("<I", 2) + tri * 2
    r = c.put(f"/api/files/{stl['id']}/content", content=body)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == stl["id"]
    assert c.get(f"/api/files/{stl['id']}/raw").content == body
    again = _file(c, "stl")
    assert again["id"] == stl["id"] and again["name"] == stl["name"] and again["size"] == len(body)
    # Not an STL, or not an STL slot: refused, file untouched.
    assert c.put(f"/api/files/{stl['id']}/content", content=b"hello").status_code == 400
    assert c.get(f"/api/files/{stl['id']}/raw").content == body
    sliced = _file(c, "sliced3mf")
    assert c.put(f"/api/files/{sliced['id']}/content", content=body).status_code == 400


def test_add_stl_to_model(app_client):
    import struct

    c, _ = app_client
    tri = struct.pack("<12fH", *([0.0] * 3), 0, 0, 0, 10, 0, 0, 0, 10, 0, 0)
    body = b"\0" * 80 + struct.pack("<I", 1) + tri
    models = c.get("/api/models").json()["models"]
    folder = next(m for m in models if "#" not in m["id"] and
                  any(f["kind"] == "stl" for f in c.get(f"/api/models/{m['id']}").json()["files"]))
    r = c.post(f"/api/models/{folder['id']}/stl?name=Extra part", content=body)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["name"] == "Extra part.stl" and j["model_id"] == folder["id"]
    files = c.get(f"/api/models/{folder['id']}").json()["files"]
    assert any(f["id"] == j["id"] and f["kind"] == "stl" for f in files)
    assert c.get(f"/api/files/{j['id']}/raw").content == body
    # Same name again: refused. Not an STL: refused, nothing left behind.
    assert c.post(f"/api/models/{folder['id']}/stl?name=Extra part.stl", content=body).status_code == 409
    assert c.post(f"/api/models/{folder['id']}/stl?name=junk.stl", content=b"hello").status_code == 400
    names = [f["name"] for f in c.get(f"/api/models/{folder['id']}").json()["files"]]
    assert "junk.stl" not in names
    assert c.post("/api/models/nope/stl?name=a.stl", content=body).status_code == 404


def test_add_stl_to_loose_model_makes_folder(app_client):
    import struct

    c, _ = app_client
    tri = struct.pack("<12fH", *([0.0] * 3), 0, 0, 0, 10, 0, 0, 0, 10, 0, 0)
    body = b"\0" * 80 + struct.pack("<I", 1) + tri
    root = c.app.state.settings.library
    cat = next(d for d in root.iterdir() if d.is_dir() and not d.name.startswith("."))
    (cat / "Lonely Widget.stl").write_bytes(body)
    c.app.state.scanner.scan()
    m = next(m for m in c.get("/api/models").json()["models"] if "lonely widget" in m["name"].lower())
    r = c.post(f"/api/models/{m['id']}/stl?name=Lid.stl", content=body)
    assert r.status_code == 200, r.text
    assert (cat / "Lonely Widget" / "Lid.stl").is_file()
    assert (cat / "Lonely Widget" / "Lonely Widget.stl").is_file()
    assert not (cat / "Lonely Widget.stl").exists()
    names = {f["name"] for f in c.get(f"/api/models/{r.json()['model_id']}").json()["files"]}
    assert names == {"Lonely Widget.stl", "Lid.stl"}
