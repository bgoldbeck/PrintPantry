"""Process settings overrides (Slice dialog) and saved presets."""

import json
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "slicer"))

import project  # noqa: E402
import service  # noqa: E402

from app import process_settings as PS  # noqa: E402
from tests.test_slicer import _file, app_client, svc, profiles, config  # noqa: E402,F401


def test_catalog_has_orca_tabs_and_basics():
    data = json.loads(PS.CATALOG_PATH.read_text())
    assert [t["name"] for t in data["tabs"]] == ["Quality", "Strength", "Speed", "Support", "Multimaterial", "Others"]
    cat = PS.catalog()
    for key in ("layer_height", "wall_loops", "sparse_infill_density", "sparse_infill_pattern", "outer_wall_speed",
                "enable_support", "support_type", "brim_type", "top_shell_layers"):
        assert key in cat and not cat[key].get("advanced"), key
    assert cat["seam_slope_type"].get("advanced")
    # Nothing that runs programs or holds G-code.
    assert not any(service.NO_OVERRIDE.search(k) for k in cat)


def test_normalise_values():
    n = PS.normalise({"wall_loops": 4, "sparse_infill_density": "15", "enable_support": True,
                      "outer_wall_speed": "120.50", "seam_gap": "10%", "support_type": "tree(auto)"})
    assert n == {"wall_loops": "4", "sparse_infill_density": "15%", "enable_support": "1",
                 "outer_wall_speed": "120.5", "seam_gap": "10%", "support_type": "tree(auto)"}
    for bad in ({"post_process": "x"}, {"wall_loops": "2.5"}, {"wall_loops": "-1"}, {"support_type": "lots"},
                {"sparse_infill_density": "150"}, {"layer_height": "abc"}):
        with pytest.raises(HTTPException):
            PS.normalise(bad)


def test_carry_over_skips_rejected_values():
    got = PS.carry_over({"wall_loops": "5", "raft_first_layer_expansion": "-1", "post_process": ["x"],
                         "sparse_infill_density": "25%", "printer_model": "X1C"})
    assert got == {"wall_loops": "5", "sparse_infill_density": "25%"}


def test_presets_store(tmp_path):
    store = PS.ProcessPresets(tmp_path / "p.json")
    store.save("Strong", "u1", "0.20 Standard", "U1", {"wall_loops": "4"})
    store.save("Fast", "p1s", "0.20mm Standard @BBL X1C", "P1S", {"outer_wall_speed": "200"})
    assert [p["name"] for p in store.list()] == ["Fast", "Strong"]  # shared by both printers
    assert store.list()[1]["overrides"] == {"wall_loops": "4"}
    with pytest.raises(HTTPException):
        store.save("Empty", "u1", "", "", {})
    store.delete("Fast")
    assert [p["name"] for p in store.list()] == ["Strong"]


def test_apply_overrides_keeps_list_shape():
    cfg = {"wall_loops": "2", "outer_wall_speed": ["60", "60"], "different_settings_to_system": ["a", "", ""]}
    project.apply_overrides(cfg, {"wall_loops": "4", "outer_wall_speed": "100", "brim_width": "5"})
    assert cfg["wall_loops"] == "4" and cfg["outer_wall_speed"] == ["100", "100"] and cfg["brim_width"] == "5"
    assert cfg["different_settings_to_system"][0] == ""


def test_service_rejects_unsafe_overrides():
    assert service.clean_overrides({"wall_loops": "4"}) == {"wall_loops": "4"}
    for bad in ({"post_process": "rm -rf /"}, {"machine_start_gcode": "G28"}, {"filename_format": "x"},
                {"wall_loops": "1\nG28"}, {"Wall": "1"}):
        with pytest.raises(service.BadRequest):
            service.clean_overrides(bad)


def test_build_settings_applies_overrides(svc, tmp_path):
    job = service.Job(id="j", path="x", printer="u1", mode="presets", machine="Snapmaker U1 (0.4 nozzle)",
                      process="0.20 Standard @Snapmaker U1 (0.4 nozzle)", filaments=["Snapmaker PETG @U1"],
                      bed_type="Textured PEI Plate", overrides={"wall_loops": "5", "sparse_infill_density": "30%"})
    work = tmp_path / "work"
    work.mkdir()
    service.build_settings(job, service.PRINTERS["u1"], work, project.ProjectInfo(kind="stl"))
    proc = json.loads((work / "process.json").read_text())
    assert proc["wall_loops"] == "5" and proc["sparse_infill_density"] == "30%" and proc["layer_height"] == "0.2"
    assert "wall_loops=5" in job.log


def test_process_values(svc):
    v = service.process_values("u1", "0.20 Standard @Snapmaker U1 (0.4 nozzle)")
    assert v["values"]["wall_loops"] == "2" and v["values"]["layer_height"] == "0.2"


def test_app_overrides_and_presets(app_client):
    c, fake = app_client
    stl = _file(c, "stl")
    res = c.post("/api/slicer/jobs", json={"file_id": stl["id"], "printer": "u1", "machine": "m", "process": "p",
                                           "filaments": ["f"], "overrides": {"wall_loops": 4, "enable_support": True}})
    assert res.status_code == 200
    assert fake.calls[-1][2]["json"]["overrides"] == {"wall_loops": "4", "enable_support": "1"}
    bad = c.post("/api/slicer/jobs", json={"file_id": stl["id"], "printer": "u1", "overrides": {"post_process": "x"}})
    assert bad.status_code == 400
    assert c.put("/api/slicer/process-presets", json={"name": "Strong", "printer": "u1", "process": "p",
                                                      "overrides": {"wall_loops": "4"}}).status_code == 200
    got = c.get("/api/slicer/process-presets").json()["presets"]
    assert got[0]["name"] == "Strong" and got[0]["printer"] == "u1"
    assert c.delete("/api/slicer/process-presets?name=Strong").status_code == 200
    assert c.get("/api/slicer/process-presets").json()["presets"] == []


def test_app_author_profile_carries_project_settings(app_client):
    c, fake = app_client
    proj = _file(c, "project3mf")
    orig = fake.call

    def call(method, path, **kw):
        if path == "/process":
            fake.calls.append((method, path, kw))
            return {"source": "project", "values": {"wall_loops": "6", "seam_gap": "-1", "layer_height": "0.16"}}
        return orig(method, path, **kw)
    fake.call = call
    res = c.post("/api/slicer/jobs", json={"file_id": proj["id"], "printer": "u1", "mode": "presets", "machine": "m",
                                           "process": "p", "filaments": ["f"], "author_profile": True,
                                           "overrides": {"wall_loops": "3"}})
    assert res.status_code == 200
    sent = fake.calls[-1][2]["json"]
    assert sent["overrides"] == {"wall_loops": "3", "layer_height": "0.16"}  # user wins; bad -1 dropped
    assert "author_profile" not in sent
    vals = c.get(f"/api/slicer/process?printer=u1&file_id={proj['id']}&mode=project").json()["values"]
    assert vals == {"wall_loops": "6", "seam_gap": "-1", "layer_height": "0.16"}
