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


def test_type_folders_are_merged(tmp_path):
    """library/stls/x.stl + library/3mf/x.3mf (Brandon's layout) -> one model."""
    lib = tmp_path / "lib"
    demo.write_stl(lib / "stls" / "Pot Ladle Clip-Pot Ladle Clip.stl", demo.SHAPES["cube"]())
    demo.write_project_3mf(lib / "3mf" / "Pot Ladle Clip-Pot Ladle Clip.3mf", ["Pot Ladle Clip-Pot Ladle Clip.stl"], (1, 2, 3))
    demo.write_stl(lib / "Kitchen" / "Spoon Rest" / "stl" / "rest.stl", demo.SHAPES["cube"]())
    demo.write_u1_gcode(lib / "Kitchen" / "Spoon Rest" / "gcode" / "rest_U1.gcode", ["rest.stl"], 60, [("PLA", (1, 2, 3), 1.0)])
    settings = Settings(library=lib, data=tmp_path / "d", password="", scan_interval=0)
    with TestClient(create_app(settings, background_scan=False)) as c:
        cats = [x["name"] for x in c.get("/api/library").json()["categories"]]
        assert cats == ["Kitchen", "Uncategorized"]
        unc = c.get("/api/categories/Uncategorized").json()["models"]
        assert len(unc) == 1 and unc[0]["counts"]["stl"] == 1 and unc[0]["counts"]["project"] == 1
        kitchen = c.get("/api/categories/Kitchen").json()["models"]
        assert [m["name"] for m in kitchen] == ["Spoon Rest"] and kitchen[0]["counts"]["sliced"] == 1


# ---------------------------------------------------------------- STLs and their 3MFs


def test_stl_without_3mf_is_a_viewable_model(client):
    for name in ("plant label", "Dice Tower", "loose part"):
        m = client.get(f"/api/models/{by_name(client, name)['id']}").json()
        stls = [f for f in m["files"] if f["kind"] == "stl"]
        assert len(stls) == 1
        assert m["stl_links"] == {stls[0]["id"]: {"projects": [], "sliced": []}}
        assert client.get(f"/api/files/{stls[0]['id']}/raw").status_code == 200


def test_stl_with_several_3mfs(tmp_path):
    lib = tmp_path / "lib"
    obj = "Pot Ladle Clip-Pot Ladle Clip.stl"
    demo.write_stl(lib / "stls" / obj, demo.SHAPES["cube"]())
    demo.write_project_3mf(lib / "3mf" / "Pot Ladle Clip-Pot Ladle Clip.3mf", [obj], (1, 2, 3))
    demo.write_project_3mf(lib / "3mf" / "Pot Ladle Clip-Pot Ladle Clip x4.3mf", [obj], (1, 2, 3))
    # named differently, but the object inside is the STL
    demo.write_project_3mf(lib / "3mf" / "ladle clips for mum.3mf", [obj], (1, 2, 3))
    demo.write_stl(lib / "stls" / "lonely.stl", demo.SHAPES["cube"]())
    settings = Settings(library=lib, data=tmp_path / "d", password="", scan_interval=0)
    with TestClient(create_app(settings, background_scan=False)) as c:
        models = c.get("/api/categories/Uncategorized").json()["models"]
        assert sorted((m["name"], m["counts"]["stl"], m["counts"]["project"]) for m in models) == [
            ("Pot Ladle Clip-Pot Ladle Clip", 1, 3),
            ("lonely", 1, 0),
        ]
        clip = c.get(f"/api/models/{next(m['id'] for m in models if m['counts']['project'])}").json()
        [links] = clip["stl_links"].values()
        assert len(links["projects"]) == 3


def test_stl_links_with_several_stls(tmp_path):
    lib = tmp_path / "lib"
    folder = lib / "Organizers" / "Bin"
    demo.write_stl(folder / "bin.stl", demo.SHAPES["cube"]())
    demo.write_stl(folder / "lid.stl", demo.SHAPES["cube"]())
    demo.write_project_3mf(folder / "bin.3mf", ["bin.stl"], (1, 2, 3))
    demo.write_project_3mf(folder / "bin and lid.3mf", ["bin.stl", "lid.stl"], (1, 2, 3))
    demo.write_project_3mf(folder / "lid_tall.3mf", ["something else"], (1, 2, 3))
    settings = Settings(library=lib, data=tmp_path / "d", password="", scan_interval=0)
    with TestClient(create_app(settings, background_scan=False)) as c:
        mid = c.get("/api/categories/Organizers").json()["models"][0]["id"]
        m = c.get(f"/api/models/{mid}").json()
        name = {f["id"]: f["name"] for f in m["files"]}
        got = {name[s]: sorted(name[p] for p in l["projects"]) for s, l in m["stl_links"].items()}
        assert got == {"bin.stl": ["bin and lid.3mf", "bin.3mf"], "lid.stl": ["bin and lid.3mf", "lid_tall.3mf"]}


def test_slicer_download_link_works_without_login(tmp_path, library):
    settings = Settings(library=library, data=tmp_path / "d4", password="hunter2", scan_interval=0)
    with TestClient(create_app(settings, background_scan=False)) as c:
        c.post("/login", data={"password": "hunter2"}, follow_redirects=False)
        mid = c.get("/api/models", params={"q": "Wrench"}).json()["models"][0]["id"]
        stl = next(f for f in c.get(f"/api/models/{mid}").json()["files"] if f["kind"] == "stl")
        link = c.post(f"/api/files/{stl['id']}/link").json()["path"]
        assert link.startswith("/api/dl/") and link.endswith("/wrench_holder.stl") and "?" not in link
        c.cookies.clear()
        assert c.post(f"/api/files/{stl['id']}/link").status_code == 401  # making links needs login
        r = c.get(link)
        assert r.status_code == 200 and r.content[:5] != b"<!doc"
        token = link.split("/")[3]
        fid, exp, sig = token.split(".")
        assert c.get(f"/api/dl/{fid}.{exp}.{'0' * len(sig)}/x.stl").status_code == 403
        assert c.get(f"/api/dl/{fid}.1.{sig}/x.stl").status_code == 403


def test_slicer_preference(client):
    assert client.get("/api/prefs").json()["slicer"] == "snapmaker"
    assert client.put("/api/prefs", json={"slicer": "bambu"}).json()["slicer"] == "bambu"
    assert client.get("/api/prefs").json()["slicer"] == "bambu"
    assert client.put("/api/prefs", json={"slicer": "cura"}).status_code == 400


def test_prune_thumbs(tmp_path):
    from types import SimpleNamespace

    from app.main import prune_thumbs

    (tmp_path / "abc-100-5.png").write_bytes(b"x")
    (tmp_path / "abc-90-5.png").write_bytes(b"x")
    (tmp_path / "gone-1-1.png").write_bytes(b"x")
    stl = SimpleNamespace(id="abc", mtime=100.7, size=5)
    assert prune_thumbs(tmp_path, [stl]) == 2
    assert [p.name for p in tmp_path.iterdir()] == ["abc-100-5.png"]
