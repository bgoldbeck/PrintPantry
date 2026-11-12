import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import formats
from app.config import Settings
from app.main import create_app
from app.scanner import group_key

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("demo", ROOT / "scripts" / "make_demo_library.py")
demo = importlib.util.module_from_spec(spec)
sys.modules["demo"] = demo
spec.loader.exec_module(demo)


@pytest.fixture()
def library(tmp_path):
    return demo.make_demo(tmp_path / "library")


@pytest.fixture()
def client(tmp_path, library):
    settings = Settings(library=library, data=tmp_path / "data", password="", scan_interval=0)
    app = create_app(settings, background_scan=False)
    with TestClient(app) as c:
        yield c


def by_name(client, name):
    res = client.get("/api/models", params={"q": name}).json()
    return next(m for m in res["models"] if m["name"] == name)


# ---------------------------------------------------------------- formats


def test_sliced_3mf_plates(library):
    info = formats.read_info(library / "Tools/Wrench Holder/wrench_holder_P1S_PLA.gcode.3mf")
    assert info.kind == "sliced3mf"
    assert info.printer_model == "Bambu Lab P1S"
    assert [p.index for p in info.plates] == [1, 2]
    p2 = info.plates[1]
    assert p2.seconds == 2700
    assert p2.grams == pytest.approx(20.3)
    assert [f.color for f in p2.filaments] == ["#282828", "#F0F0F0"]
    assert all(p.has_thumbnail for p in info.plates)
    assert "wrench_holder.stl" in info.objects


def test_project_3mf(library):
    info = formats.read_info(library / "Tools/Wrench Holder/wrench_holder.3mf")
    assert info.kind == "project3mf"
    assert info.plates[0].has_thumbnail
    assert "wrench_holder.stl" in info.objects


def test_u1_gcode(library):
    path = library / "Organizers/Gridfinity Bin 2x1/bin_2x1_U1_4color.gcode"
    info = formats.read_info(path)
    assert info.kind == "gcode"
    assert info.printer_model == "Snapmaker U1"
    plate = info.plates[0]
    assert plate.seconds == 2 * 3600 + 13 * 60
    assert len(plate.filaments) == 4
    assert plate.grams == pytest.approx(56.5)
    data, mime = formats.read_gcode_thumbnail(path)
    assert mime == "image/png" and data.startswith(b"\x89PNG")


def test_gcode_skips_unused_toolheads(tmp_path):
    path = tmp_path / "x.gcode"
    demo.write_u1_gcode(path, ["x.stl"], 600, [("PETG", (1, 2, 3), 5.0)])
    info = formats.read_info(path)
    assert len(info.plates[0].filaments) == 1
    assert info.plates[0].filaments[0].type == "PETG"


def test_broken_3mf_does_not_crash(tmp_path):
    bad = tmp_path / "broken.3mf"
    bad.write_bytes(b"not a zip")
    info = formats.read_info(bad)
    assert info.error


def test_group_key_strips_printer_suffixes():
    assert group_key("planter_P1S.gcode.3mf") == "planter"
    assert group_key("planter_U1.gcode") == "planter"
    assert group_key("Planter_plate_2_PETG.3mf") == "planter"
    assert group_key("planter.stl") == "planter"


# ---------------------------------------------------------------- API


def test_categories_and_icons(client):
    lib = client.get("/api/library").json()
    names = [c["name"] for c in lib["categories"]]
    assert names[-1] == "Uncategorized"
    assert {"Tools", "Organizers", "Garden", "Toys & Games", "Printer Upgrades"} <= set(names)
    tools = next(c for c in lib["categories"] if c["name"] == "Tools")
    assert tools["icon"] == "wrench"
    assert tools["model_count"] == 2


def test_model_groups_files(client):
    m = client.get(f"/api/models/{by_name(client, 'Wrench Holder')['id']}").json()
    kinds = sorted(f["kind"] for f in m["files"])
    assert kinds == ["gcode", "project3mf", "sliced3mf", "stl"]
    printers = {f["name"]: f["printer"] for f in m["files"] if f["printer"]}
    assert printers == {"wrench_holder_P1S_PLA.gcode.3mf": "p1s", "wrench_holder_U1.gcode": "u1"}
    # files in sub-folders of a model folder belong to it
    hook = client.get(f"/api/models/{by_name(client, 'Wall Hook')['id']}").json()
    assert {f["name"] for f in hook["files"]} == {"hook.stl", "hook_x4.gcode.3mf"}


def test_loose_files_grouped_by_name(client):
    planter = by_name(client, "planter")
    assert planter["counts"] == {"stl": 1, "project": 0, "sliced": 2}
    assert planter["printers"] == ["p1s", "u1"]
    label = by_name(client, "plant label")
    assert label["counts"]["sliced"] == 0


def test_thumbnails_and_raw(client):
    m = client.get(f"/api/models/{by_name(client, 'Wrench Holder')['id']}").json()
    sliced = next(f for f in m["files"] if f["kind"] == "sliced3mf")
    r = client.get(f"/api/files/{sliced['id']}/thumb", params={"plate": 2})
    assert r.status_code == 200 and r.content.startswith(b"\x89PNG")
    stl = next(f for f in m["files"] if f["kind"] == "stl")
    r = client.get(f"/api/files/{stl['id']}/raw")
    assert r.status_code == 200 and len(r.content) == stl["size"]
    r = client.get(f"/api/files/{stl['id']}/raw", params={"download": 1})
    assert "attachment" in r.headers["content-disposition"]


def test_stl_preview_upload(client):
    m = by_name(client, "Dice Tower")
    file_id = m["cover"]["file"]
    assert client.get(f"/api/files/{file_id}/thumb").status_code == 404
    png = demo.png(8, 8, (1, 2, 3))
    assert client.put(f"/api/files/{file_id}/thumb", content=png).status_code == 200
    assert client.get(f"/api/files/{file_id}/thumb").content == png
    assert client.put(f"/api/files/{file_id}/thumb", content=b"nope").status_code == 400


def test_unknown_file_404(client):
    assert client.get("/api/files/doesnotexist/raw").status_code == 404


def test_category_override(client):
    r = client.put("/api/categories/Garden", json={"icon": "flower", "color": "#123456"})
    assert r.status_code == 200
    client.post("/api/rescan")
    garden = client.get("/api/categories/Garden").json()
    assert garden["icon"] == "flower" and garden["color"] == "#123456"
    assert client.put("/api/categories/Garden", json={"icon": "<script>"}).status_code == 400


def test_category_json_file(tmp_path, library):
    (library / "Tools" / "_category.json").write_text(json.dumps({"icon": "hammer"}))
    settings = Settings(library=library, data=tmp_path / "d2", password="", scan_interval=0)
    with TestClient(create_app(settings, background_scan=False)) as c:
        tools = c.get("/api/categories/Tools").json()
        assert tools["icon"] == "hammer"


def test_search(client):
    res = client.get("/api/models", params={"q": "gridfinity"}).json()
    assert [m["name"] for m in res["models"]] == ["Gridfinity Bin 2x1"]
    res = client.get("/api/models", params={"printer": "u1"}).json()
    assert {m["name"] for m in res["models"]} == {"Wrench Holder", "Gridfinity Bin 2x1", "planter"}


def test_missing_library(tmp_path):
    settings = Settings(library=tmp_path / "nope", data=tmp_path / "d", password="", scan_interval=0)
    with TestClient(create_app(settings, background_scan=False)) as c:
        assert "not found" in c.get("/api/library").json()["error"]


def test_password_login(tmp_path, library):
    settings = Settings(library=library, data=tmp_path / "d3", password="hunter2", scan_interval=0)
    with TestClient(create_app(settings, background_scan=False)) as c:
        assert c.get("/api/library").status_code == 401
        assert c.get("/healthz").status_code == 200
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        c.post("/login", data={"password": "wrong"}, follow_redirects=False)
        assert c.get("/api/library").status_code == 401
        c.post("/login", data={"password": "hunter2"}, follow_redirects=False)
        assert c.get("/api/library").status_code == 200


def test_spa_fallback(client):
    r = client.get("/")
    assert r.status_code == 200 and "Print Pantry" in r.text
    assert client.get("/api/nope").status_code == 404
