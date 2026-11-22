import json
import struct
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import guides as guides_mod
from app.config import Settings
from app.guides import basic_guide, clean_markdown, stl_size
from app.main import create_app


def write_box_stl(path, dx=10.0, dy=20.0, dz=5.0):
    """A closed box as a binary STL (12 triangles)."""
    v = [(x, y, z) for x in (0, dx) for y in (0, dy) for z in (0, dz)]
    faces = [(0, 2, 1), (1, 2, 3), (4, 5, 6), (5, 7, 6), (0, 1, 4), (1, 5, 4),
             (2, 6, 3), (3, 6, 7), (0, 4, 2), (2, 4, 6), (1, 3, 5), (3, 7, 5)]
    out = bytearray(80) + struct.pack("<I", len(faces))
    for a, b, c in faces:
        out += struct.pack("<12fH", 0, 0, 0, *v[a], *v[b], *v[c], 0)
    path.write_bytes(bytes(out))


def printables(request: httpx.Request):
    q = json.loads(request.content)["query"]
    if "PrintDescription" in q:
        return httpx.Response(200, json={"data": {"print": {"description": "<p>Glue it.</p><ul><li>2 pins</li></ul>"}}})
    if "PrintSettings" in q:
        return httpx.Response(200, json={"data": {"print": {"nozzleDiameters": ["0.40"], "layerHeights": ["0.20"],
                                                            "materials": [{"name": "PLA"}], "numPieces": 3}}})
    if "Comments" in q:
        return httpx.Response(200, json={"data": {"comments": [{"text": "Sand the alignment pegs a bit before gluing them."},
                                                                {"text": "nice"}]}})
    return httpx.Response(404)


@pytest.fixture()
def library(tmp_path):
    lib = tmp_path / "library"
    folder = lib / "Tools" / "Tape Dispenser"
    folder.mkdir(parents=True)
    write_box_stl(folder / "Spindle 1in.stl", 25.4, 73.6, 73.6)
    write_box_stl(folder / "Pin.stl")
    (folder / "pantry-source.json").write_text(json.dumps({
        "site": "Printables", "id": "129838", "url": "https://www.printables.com/model/129838",
        "title": "Tape Dispenser", "author": "Miguel M.", "summary": "Modular."}))
    return lib


@pytest.fixture()
def client(tmp_path, library):
    settings = Settings(library=library, data=tmp_path / "data", password="", scan_interval=0)
    app = create_app(settings, background_scan=False)
    app.state.guides.http = httpx.Client(transport=httpx.MockTransport(printables))
    with TestClient(app) as c:
        c.app_ = app
        c.model_id = c.get("/api/models", params={"q": "Tape"}).json()["models"][0]["id"]
        yield c


def wait(client, job):
    for _ in range(200):
        j = client.get(f"/api/guide-jobs/{job['id']}").json()
        if j["state"] != "running":
            return j
        time.sleep(0.02)
    raise AssertionError("guide job never finished")


def test_stl_size(tmp_path):
    p = tmp_path / "box.stl"
    write_box_stl(p, 10, 20, 5)
    assert stl_size(p) == {"x": 10.0, "y": 20.0, "z": 5.0, "cm3": 1.0}
    (tmp_path / "ascii.stl").write_text("solid x\nendsolid x\n")
    assert stl_size(tmp_path / "ascii.stl") is None


def test_clean_markdown():
    assert clean_markdown("```markdown\n# T\nbody\n```") == "# T\nbody\n"
    assert clean_markdown("Here you go:\n\n# T\nbody") == "# T\nbody\n"


def test_basic_guide_without_key(client, library):
    assert client.get("/api/ai/settings").json()["has_key"] is False
    assert client.get(f"/api/models/{client.model_id}/guide").json()["markdown"] == ""
    job = client.post(f"/api/models/{client.model_id}/guide/generate", json={}).json()
    assert wait(client, job)["state"] == "done"
    g = client.get(f"/api/models/{client.model_id}/guide").json()
    assert g["by"] == "basic"
    md = g["markdown"]
    assert md.startswith("# Tape Dispenser") and "Glue it." in md and "• 2 pins" in md
    assert "| Spindle 1in.stl | 25.4 x 73.6 x 73.6 |" in md
    assert "| Layer height | 0.20 |" in md and "Sand the alignment pegs" in md and "- nice" not in md
    folder = library / "Tools" / "Tape Dispenser"
    assert (folder / "pantry-guide.md").read_text() == md
    # the full description is kept for next time
    assert json.loads((folder / "pantry-source.json").read_text())["description"] == "Glue it.\n• 2 pins"
    # .md files don't show up as model files
    files = client.get(f"/api/models/{client.model_id}").json()["files"]
    assert all(not f["name"].endswith(".md") for f in files)


def test_ai_guide_with_request(client, monkeypatch):
    seen = {}

    def fake_writer(key, model, facts, request, current):
        seen.update(key=key, model=model, request=request, current=current, text=facts.as_text())
        return "# Guide\n\n## Parts to print\n| File | Qty |\n", "claude-opus-5-5"

    client.app_.state.guides.writer = fake_writer
    s = client.put("/api/ai/settings", json={"api_key": " sk-ant-test-1234 "}).json()
    assert s == {"has_key": True, "key_hint": "…1234", "model": "claude-opus-5-5", "models": list(guides_mod.MODELS)}
    assert client.put("/api/ai/settings", json={"model": "gpt"}).status_code == 400

    client.put(f"/api/models/{client.model_id}/guide", json={"markdown": "# Old\n"})
    job = client.post(f"/api/models/{client.model_id}/guide/generate", json={"request": "Only the 1 inch size"}).json()
    assert wait(client, job)["state"] == "done"
    assert seen["key"] == "sk-ant-test-1234" and seen["request"] == "Only the 1 inch size"
    assert seen["current"] == "# Old\n"
    assert "Pin.stl (stl) | 10.0 x 20.0 x 5.0 mm" in seen["text"] and "<maker_comments>" in seen["text"]
    g = client.get(f"/api/models/{client.model_id}/guide").json()
    assert g["by"] == "ai" and g["ai_model"] == "claude-opus-5-5" and g["markdown"].startswith("# Guide")


def test_ai_error_is_reported(client):
    def broken(*a):
        raise guides_mod.GuideError("Anthropic refused the API key. Check it under AI settings.")

    client.app_.state.guides.writer = broken
    client.put("/api/ai/settings", json={"api_key": "bad"})
    job = client.post(f"/api/models/{client.model_id}/guide/generate", json={}).json()
    j = wait(client, job)
    assert j["state"] == "error" and "refused the API key" in j["error"]


def test_edit_and_clear(client):
    g = client.put(f"/api/models/{client.model_id}/guide", json={"markdown": "# Mine\n\nHello"}).json()
    assert g["markdown"] == "# Mine\n\nHello\n" and g["by"] == "edited"
    assert client.put(f"/api/models/{client.model_id}/guide", json={"markdown": "  "}).json()["markdown"] == ""
    assert client.get("/api/models/nope/guide").status_code == 404


def test_basic_guide_without_source():
    f = guides_mod.Facts(name="Clip", category="Tools", files=[{"name": "clip.stl", "kind": "stl"}])
    md = basic_guide(f)
    assert md.startswith("# Clip") and "| clip.stl | | |" in md
