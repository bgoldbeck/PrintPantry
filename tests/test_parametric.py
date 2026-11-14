"""Parametric models: reading OpenSCAD / FreeCAD parameters, saved parameter
sets, the app's /api/parametric routes and the slicer container's render
jobs. OpenSCAD and FreeCAD themselves are faked."""

import json
import stat
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "slicer"))

import fonts
import project
import render
import service

from app import parametric as PM
from app.config import Settings
from app.main import create_app
from app.scanner import make_id
from app.slicing import SlicerClient

SCAD = """// Parametric box
/* [Size] */
// Box width (mm)
width = 40; // [10:1:200]
depth = 30; // [10:200]
height = 2.5;
/* [Style] */
style = "round"; // [round, square]
size = 2; // [1:Small, 2:Medium, 3:Large]
label = "Hi \\"there\\"";
hollow = true;
offset = [1, 2.5, 3];
computed = width * 2;
/* [Hidden] */
$fn = 64;
module box() {
  inner = 3;
  cube([width, depth, height]);
}
after = 9;
box();
"""


def test_scad_customizer_comments():
    params = {p["name"]: p for p in PM.parse_scad(SCAD)["params"]}
    assert list(params) == ["width", "depth", "height", "style", "size", "label", "hollow", "offset"]
    w = params["width"]
    assert (w["type"], w["value"], w["min"], w["step"], w["max"]) == ("slider", 40, 10, 1, 200)
    assert w["description"] == "Box width (mm)" and w["group"] == "Size"
    assert params["depth"]["type"] == "slider" and "step" not in params["depth"]
    assert params["height"]["type"] == "number"
    assert params["style"]["options"] == [{"value": "round", "label": "round"}, {"value": "square", "label": "square"}]
    assert params["size"]["options"][2] == {"value": 3, "label": "Large"} and params["size"]["group"] == "Style"
    assert params["label"]["value"] == 'Hi "there"'
    assert params["hollow"]["type"] == "bool" and params["offset"]["value"] == [1, 2.5, 3]


def _fcstd(path: Path) -> Path:
    doc = """<?xml version='1.0' encoding='utf-8'?>
<Document SchemaVersion="4" ProgramVersion="1.1R" FileVersion="1">
  <Objects Count="6">
    <Object type="Spreadsheet::Sheet" name="Params" id="1" />
    <Object type="Spreadsheet::Sheet" name="Fit" id="2" />
    <Object type="Part::Box" name="Plate" id="3" />
    <Object type="PartDesign::Body" name="Sign" id="4" />
    <Object type="Part::MultiFuse" name="Text" id="5" />
    <Object type="App::Origin" name="Origin" id="6" />
  </Objects>
  <ObjectData Count="6">
    <Object name="Params"><Properties Count="2">
      <Property name="Label" type="App::PropertyString"><String value="Params"/></Property>
      <Property name="cells" type="Spreadsheet::PropertySheet"><Cells Count="9" xlink="1">
        <Cell address="A1" content="&apos;Parameter" />
        <Cell address="A2" content="&apos;Sign text" />
        <Cell address="B2" content="&apos;Peach Jam" alias="Text" />
        <Cell address="C2" content="&apos;Single line" />
        <Cell address="B3" content="&apos;C:/Windows/Fonts/arialbd.ttf" alias="FontFile" />
        <Cell address="B4" content="12" alias="TextSize" />
        <Cell address="B5" content="1" alias="AutoWidth" />
        <Cell address="C5" content="&apos;1 = width follows the text" />
        <Cell address="B6" content="=TextSize * 2" alias="Twice" />
        <Cell address="B7" content="3 mm" alias="Thickness" />
      </Cells></Property>
    </Properties></Object>
    <Object name="Fit"><Properties Count="1">
      <Property name="cells" type="Spreadsheet::PropertySheet"><Cells Count="1">
        <Cell address="B1" content="=Params.TextSize + 1" alias="Width" />
      </Cells></Property>
    </Properties></Object>
    <Object name="Plate"><Properties Count="1">
      <Property name="Visibility" type="App::PropertyBool"><Bool value="true"/></Property>
    </Properties></Object>
    <Object name="Sign"><Properties Count="3">
      <Property name="Label" type="App::PropertyString"><String value="Sign plate"/></Property>
      <Property name="Visibility" type="App::PropertyBool"><Bool value="true"/></Property>
      <Property name="BaseFeature" type="App::PropertyLink"><Link value="Plate"/></Property>
    </Properties></Object>
    <Object name="Text"><Properties Count="2">
      <Property name="Visibility" type="App::PropertyBool"><Bool value="true"/></Property>
      <Property name="ExpressionEngine" type="App::PropertyExpressionEngine"><ExpressionEngine count="1">
        <Expression path="Height" expression="Sign.Shape.BoundBox.ZMax"/></ExpressionEngine></Property>
    </Properties></Object>
    <Object name="Origin"><Properties Count="1">
      <Property name="Visibility" type="App::PropertyBool"><Bool value="true"/></Property>
    </Properties></Object>
  </ObjectData>
</Document>"""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Document.xml", doc)
    return path


def test_fcstd_sheet_aliases_and_parts(tmp_path):
    info = PM.parse_fcstd(_fcstd(tmp_path / "Sign.FCStd"))
    params = {p["name"]: p for p in info["params"]}
    assert list(params) == ["Text", "FontFile", "TextSize", "AutoWidth", "Thickness"]  # formulas left out
    assert params["Text"]["label"] == "Sign text" and params["Text"]["description"] == "Single line"
    assert params["Text"]["cell"] == "B2" and params["Text"]["sheet"] == "Params"
    assert params["FontFile"]["type"] == "font"
    assert params["TextSize"] == {**params["TextSize"], "type": "number", "value": 12}
    assert params["AutoWidth"]["type"] == "bool" and params["AutoWidth"]["value"] is True
    assert params["Thickness"]["unit"] == "mm"
    # Plate is consumed by the Sign body; an expression link doesn't consume.
    assert [p["name"] for p in info["parts"]] == ["Sign", "Text"]
    assert info["parts"][0]["label"] == "Sign plate"


def test_parameter_sets_round_trip(tmp_path):
    src = tmp_path / "box.scad"
    src.write_text(SCAD)
    PM.save_preset(src, "scad", "Wide", {"width": 120, "hollow": False, "offset": [1, 2]})
    saved = json.loads((tmp_path / "box.json").read_text())  # OpenSCAD's own file
    assert saved["parameterSets"]["Wide"] == {"width": "120", "hollow": "false", "offset": "[1, 2]"}
    assert saved["fileFormatVersion"] == "1"
    assert PM.load_presets(src, "scad")["Wide"]["width"] == "120"
    PM.save_preset(src, "scad", "Wide", None)
    assert PM.load_presets(src, "scad") == {}
    fc = tmp_path / "Sign.FCStd"
    PM.save_preset(fc, "fcstd", "Jam", {"Text": "Jam"})
    assert (tmp_path / "Sign.FCStd.params.json").is_file()


def test_coerce_checks_types():
    params = PM.parse_scad(SCAD)["params"]
    out = PM.coerce(params, {"width": "55", "hollow": "false", "offset": "1, 2", "style": "square", "nope": 1})
    assert out == {"width": 55, "hollow": False, "offset": [1.0, 2.0], "style": "square"}
    with pytest.raises(Exception):
        PM.coerce(params, {"width": "wide"})
    assert PM.safe_variant('Peach/Jam: "big"') == "PeachJam big" and PM.safe_variant("") == "custom"


# ---------------------------------------------------------------- fonts

def test_fonts_found_by_name_or_look_alike(tmp_path):
    d = tmp_path / "fonts"
    d.mkdir()
    for n in ("Pacifico-Regular.ttf", "LiberationSans-Bold.ttf", "LiberationSans-Regular.ttf", "DejaVuSans.ttf"):
        (d / n).write_bytes(b"x")
    idx = fonts.index([d])
    assert fonts.resolve("C:/Windows/Fonts/Pacifico-Regular.ttf", idx) == (str(d / "Pacifico-Regular.ttf"), "")
    path, note = fonts.resolve("C:\\Windows\\Fonts\\arialbd.ttf", idx)
    assert path.endswith("LiberationSans-Bold.ttf") and "arialbd.ttf" in note
    assert fonts.resolve("C:/Windows/Fonts/comic.ttf", idx)[0].endswith("DejaVuSans.ttf")


# ---------------------------------------------------------------- render jobs

def test_output_folder():
    lib = Path("/lib")
    assert render.out_folder(lib, "Tools/Box/box.scad") == Path("/lib/Tools/Box/generated")
    assert render.out_folder(lib, "Tools/Box/source/box.scad") == Path("/lib/Tools/Box/source/generated")
    assert render.out_folder(lib, "Tools/box.scad") == Path("/lib/Tools")  # loose file: beside it
    assert render.out_folder(lib, "Tools/files/box.scad") == Path("/lib/Tools/files")
    assert render.scad_literal('a "b"') == '"a \\"b\\""' and render.scad_literal([1, 2.5]) == "[1, 2.5]"
    assert render.scad_literal(True) == "true"


@pytest.fixture()
def fake_openscad(tmp_path, monkeypatch):
    tool = tmp_path / "openscad"
    tool.write_text("#!/bin/sh\n"
                    'if [ "$1" = "--version" ]; then echo "OpenSCAD version 2026.11.0"; exit 0; fi\n'
                    f'echo "$@" > {tmp_path}/args\n'
                    'echo "Rendering Polygon Mesh using Manifold..."\n'
                    'case "$*" in *empty*) echo "WARNING: Current top level object is empty."; exit 1;; esac\n'
                    'head -c 400 /dev/zero > "$2"\n')
    tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(render, "OPENSCAD", str(tool))
    monkeypatch.setattr(render, "_versions", {})
    return tmp_path / "args"


def _job(path, **r):
    return SimpleNamespace(id="j1", path=path, render=r, log="", warnings=[], message="", outputs=[], started=None)


def test_scad_render_writes_into_generated(tmp_path, fake_openscad):
    lib = tmp_path / "library"
    (lib / "Tools/Box").mkdir(parents=True)
    (lib / "Tools/Box/box.scad").write_text(SCAD)
    job = _job("Tools/Box/box.scad", kind="scad", variant="Wide", values={"width": 120, "style": "square"})
    render.run(job, lib, tmp_path / "work")
    assert job.outputs == ["Tools/Box/generated/box - Wide.stl"]
    assert (lib / job.outputs[0]).stat().st_size == 400
    args = fake_openscad.read_text()
    assert "--backend manifold" in args and "-D width=120" in args and '-D style="square"' in args
    assert not list((tmp_path / "work").iterdir())  # work folder cleaned up


def test_scad_render_failure_is_explained(tmp_path, fake_openscad):
    lib = tmp_path / "library"
    (lib / "Tools").mkdir(parents=True)
    (lib / "Tools/empty.scad").write_text("x = 1;")
    job = _job("Tools/empty.scad", kind="scad", variant="custom", values={})
    with pytest.raises(RuntimeError, match="empty"):
        render.run(job, lib, tmp_path / "work")


def test_service_validates_render_jobs(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    (lib / "Signs").mkdir(parents=True)
    _fcstd(lib / "Signs/Sign.FCStd")
    (lib / "Signs/x.stl").write_bytes(b"solid")
    monkeypatch.setattr(service, "LIBRARY", lib)
    queued = []
    monkeypatch.setattr(service, "queue_job", lambda job: queued.append(job) or job)
    with pytest.raises(service.BadRequest):
        service.submit_render({"path": "Signs/x.stl", "kind": "scad"})
    with pytest.raises(service.BadRequest):
        service.submit_render({"path": "Signs/Sign.FCStd", "kind": "fcstd", "parts": []})
    job = service.submit_render({"path": "Signs/Sign.FCStd", "kind": "fcstd", "parts": ["Sign"],
                                 "combine": ["Sign", "Text"], "values": {"Text": "Jam"}})
    assert job.render["combine"] == ["Sign", "Text"] and job.mode == "render" and queued == [job]


def test_plain_3mf_counts_filaments_from_parts(tmp_path):
    path = tmp_path / "sign.3mf"
    cfg = ('<config><object id="3"><metadata key="extruder" value="1"/>'
           '<part id="1"><metadata key="extruder" value="1"/></part>'
           '<part id="2"><metadata key="extruder" value="2"/></part></object></config>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", "<model/>")
        z.writestr("Metadata/model_settings.config", cfg)
    info = project.inspect(path)
    assert len(info.filaments) == 2 and info.printer_model == ""


# ---------------------------------------------------------------- app routes

class FakeRenderer(SlicerClient):
    def __init__(self):
        super().__init__("http://slicer")
        self.calls = []

    def call(self, method, path, **kw):
        self.calls.append((method, path, kw))
        if path == "/render/tools":
            return {"openscad": "2026.11", "freecad": "1.1.4", "fonts": ["Pacifico-Regular.ttf"]}
        if path == "/render":
            return {"id": "r1", "state": "queued"}
        if path.startswith("/jobs/"):
            return {"id": "r1", "state": "done", "outputs": ["Tools/Box/generated/box - Wide.stl"]}
        return {}


@pytest.fixture()
def app_client(tmp_path):
    lib = tmp_path / "library"
    (lib / "Tools/Box").mkdir(parents=True)
    (lib / "Tools/Box/box.scad").write_text(SCAD)
    (lib / "Signs/Shelf Sign").mkdir(parents=True)
    _fcstd(lib / "Signs/Shelf Sign/Sign.FCStd")
    fake = FakeRenderer()
    settings = Settings(library=lib, data=tmp_path / "data", password="", scan_interval=0)
    with TestClient(create_app(settings, background_scan=False, slicer=fake)) as c:
        yield c, fake, lib


def _find(c, name):
    for m in c.get("/api/models").json()["models"]:
        for f in c.get(f"/api/models/{m['id']}").json()["files"]:
            if f["name"] == name:
                return m, f
    raise AssertionError(name)


def test_models_with_only_a_parametric_source_show_up(app_client):
    c, _, _ = app_client
    m, f = _find(c, "box.scad")
    assert f["kind"] == "scad" and m["counts"]["parametric"] == 1
    _, f = _find(c, "Sign.FCStd")
    assert f["kind"] == "fcstd"


def test_app_describes_and_renders_scad(app_client):
    c, fake, lib = app_client
    _, f = _find(c, "box.scad")
    d = c.get(f"/api/parametric/{f['id']}").json()
    assert d["tools"]["openscad"] == "2026.11" and d["params"][0]["name"] == "width" and d["parts"] == []
    r = c.put(f"/api/parametric/{f['id']}/presets", json={"name": "Wide", "values": {"width": 150}})
    assert r.json()["presets"]["Wide"] == {"width": "150"}
    assert (lib / "Tools/Box/box.json").is_file()
    r = c.post("/api/parametric/jobs", json={"file_id": f["id"], "values": {"width": "150"}, "variant": "Wide"})
    assert r.status_code == 200
    _, path, kw = fake.calls[-1]
    assert path == "/render" and kw["json"]["values"] == {"width": 150} and kw["json"]["kind"] == "scad"
    done = c.get("/api/parametric/jobs/r1").json()
    assert done["output_ids"] == [make_id("Tools/Box/generated/box - Wide.stl")]


def test_app_renders_freecad_parts(app_client):
    c, fake, _ = app_client
    _, f = _find(c, "Sign.FCStd")
    r = c.post("/api/parametric/jobs", json={"file_id": f["id"], "values": {"Text": "Jam", "AutoWidth": False},
                                             "parts": ["Text", "Bogus"], "combine": ["Sign", "Text"]})
    assert r.status_code == 200
    body = fake.calls[-1][2]["json"]
    assert body["parts"] == ["Text"] and body["combine"] == ["Sign", "Text"]
    assert body["cells"] == {"Text": ["Params", "B2"], "AutoWidth": ["Params", "B5"]}
    assert body["values"] == {"Text": "Jam", "AutoWidth": False}
    r = c.post("/api/parametric/jobs", json={"file_id": f["id"], "parts": [], "combine": ["Sign"]})
    assert r.status_code == 400


def test_other_files_are_refused(app_client, tmp_path):
    c, _, lib = app_client
    (lib / "Tools/Box/box.stl").write_bytes(b"solid x\nendsolid x\n")
    c.post("/api/rescan")
    import time
    for _ in range(50):
        if not c.get("/api/library").json()["scanning"]:
            break
        time.sleep(0.05)
    _, f = _find(c, "box.stl")
    assert c.get(f"/api/parametric/{f['id']}").status_code == 400
