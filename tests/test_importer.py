import json

import httpx
import pytest
from fastapi.testclient import TestClient
from test_library import library  # noqa: F401  (fixture)

from app.config import Settings
from app.importer import Importer, ImportError_, parse_link, safe_name, suggest_category
from app.main import create_app

PRINT = {
    "id": "3161", "name": "3D BENCHY", "slug": "3d-benchy", "summary": "<p>Torture test</p>",
    "tags": [{"name": "boat"}, {"name": "test"}],
    "category": {"id": "12", "path": [{"name": "3D Printers"}, {"name": "Test Models"}]},
    "stls": [{"id": "49068", "name": "3dbenchy.stl", "fileSize": 9}],
    "otherFiles": [{"id": "7", "name": "readme.pdf", "fileSize": 3}, {"id": "8", "name": "benchy.3mf", "fileSize": 4}],
    "images": [{"filePath": "media/prints/3161/images/benchy.jpg"}],
}


def printables_handler(request: httpx.Request):
    host = request.url.host
    if host == "api.printables.com":
        body = json.loads(request.content)
        q = body["query"]
        if "getDownloadLink" in q:
            v = body["variables"]
            assert v["source"] == "model_detail" and v["modelId"] == "3161"
            name = {"49068": "3dbenchy.stl", "8": "benchy.3mf"}[v["id"]]
            return httpx.Response(200, json={"data": {"getDownloadLink": {"ok": True, "errors": None,
                                  "output": {"link": f"https://files.printables.com/x/{name}"}}}})
        if "PrintExtra" in q:
            return httpx.Response(200, json={"data": {"print": {"user": {"publicUsername": "CreativeTools"}, "license": {"name": "CC BY-ND"}}}})
        return httpx.Response(200, json={"data": {"print": PRINT}})
    if host == "files.printables.com":
        return httpx.Response(200, content=b"solid x\n" if request.url.path.endswith(".stl") else b"PK\x03\x04")
    if host == "media.printables.com":
        return httpx.Response(200, content=b"\xff\xd8\xff jpg")
    if host == "makerworld.com":
        if "/design/" in request.url.path:
            return httpx.Response(200, json={
                "id": 1000000, "title": "Lens Cap", "license": "BY-NC", "summary": "<p>Cap</p>",
                "tags": ["camera", "logitech"], "categories": [{"name": "Gadgets"}, {"name": "Tools"}],
                "designCreator": {"name": "maker"}, "defaultInstanceId": 5, "instances": [{"id": 5}],
                "designExtension": {"model_files": [{"modelName": "lens cap.3mf", "modelSize": 4}],
                                    "design_pictures": [{"url": "https://makerworld.bblmw.com/p.jpg"}]},
            })
        if "/f3mf" in request.url.path:
            if request.headers.get("authorization") != "Bearer tok":
                return httpx.Response(403, json={"code": 1, "error": "Please log in to download models."})
            return httpx.Response(200, json={"name": "lens cap.3mf", "url": "https://s3.example.com/lens.3mf"})
    if host in ("makerworld.bblmw.com", "s3.example.com"):
        return httpx.Response(200, content=b"PK\x03\x04")
    return httpx.Response(404)


@pytest.fixture()
def http():
    return httpx.Client(transport=httpx.MockTransport(printables_handler))


@pytest.fixture()
def client(tmp_path, library, http):  # noqa: F811
    settings = Settings(library=library, data=tmp_path / "data", password="", scan_interval=0)
    from app.store import Store

    store = Store(tmp_path / "imp.db")
    imp = Importer(library, store, lambda: None, lambda *a: None, lambda r: r, http=http)
    app = create_app(settings, background_scan=False, importer=imp)
    with TestClient(app) as c:
        c.imp = imp
        yield c


def test_parse_link():
    assert parse_link("https://www.printables.com/model/3161-3d-benchy") == ("printables", "3161")
    assert parse_link("printables.com/de/model/42-x/files") == ("printables", "42")
    assert parse_link("https://makerworld.com/en/models/1000000-lens-cap#profileId-5") == ("makerworld", "1000000")
    with pytest.raises(ImportError_):
        parse_link("https://thingiverse.com/thing:1")


def test_safe_name_and_category():
    assert safe_name('a/b:c*?"d.') == "a b c d"
    assert safe_name("") == "model"
    assert suggest_category(["3D Printers", "Test Models"], ["Tools", "Test Models"]) == "Test Models"
    assert suggest_category(["Household", "Kitchen"], ["Kitchen", "Tools"]) == "Kitchen"
    assert suggest_category(["Gadgets", "Tools"], ["Tool", "Toys"]) == "Tool"
    assert suggest_category(["Art"], ["Tools"]) == ""


def test_printables_preview_and_import(client, library):  # noqa: F811
    p = client.post("/api/import/preview", json={"url": "https://www.printables.com/model/3161-3d-benchy"}).json()
    assert p["title"] == "3D BENCHY" and p["author"] == "CreativeTools" and p["license"] == "CC BY-ND"
    assert [f["name"] for f in p["files"]] == ["3dbenchy.stl", "benchy.3mf"]  # pdf left out
    assert p["pictures"][0] == "https://media.printables.com/media/prints/3161/images/benchy.jpg"
    assert p["summary"] == "Torture test"

    job = client.imp.run_sync(p["url"], "Toys & Games", tags=["Gift"])
    assert job.state == "done", job.error
    folder = library / "Toys & Games" / "3D BENCHY"
    assert (folder / "3dbenchy.stl").read_bytes() == b"solid x\n"
    assert (folder / "benchy.3mf").exists()
    assert (folder / "images" / "printables-01.jpg").exists()
    meta = json.loads((folder / "pantry-source.json").read_text())
    assert meta["url"] == "https://www.printables.com/model/3161-3d-benchy" and meta["tags"] == ["boat", "test"]

    # the real app wiring: rescan + tags
    res = client.get("/api/models", params={"q": "tag:boat"}).json()["models"]
    assert [m["name"] for m in res] == ["3D BENCHY"]
    assert set(res[0]["tags"]) == {"boat", "test", "gift"}
    detail = client.get(f"/api/models/{res[0]['id']}").json()
    assert detail["source"]["site"] == "Printables" and detail["source"]["author"] == "CreativeTools"

    # importing again reuses the folder and skips files of the same size
    job2 = client.imp.run_sync(p["url"], "Toys & Games")
    assert job2.folder == "Toys & Games/3D BENCHY" and job2.skipped == ["benchy.3mf"]  # same size: not fetched again


def test_makerworld_without_and_with_token(client, library):  # noqa: F811
    url = "https://makerworld.com/en/models/1000000-lens-cap"
    p = client.post("/api/import/preview", json={"url": url}).json()
    assert p["files"] == [] and "logged-in" in p["note"] and p["tags"] == ["camera", "logitech"]
    assert p["suggested_category"] == "Tools"
    job = client.imp.run_sync(url, "Tools")
    assert job.state == "done" and job.done_files == []
    assert (library / "Tools" / "Lens Cap" / "images" / "makerworld-01.jpg").exists()

    assert client.put("/api/import/settings", json={"makerworld_token": "Bearer tok"}).json() == {"makerworld_token": True}
    p = client.post("/api/import/preview", json={"url": url}).json()
    assert [f["name"] for f in p["files"]] == ["lens cap.3mf"]
    job = client.imp.run_sync(url, "Tools")
    assert job.state == "done", job.error
    assert job.folder == "Tools/Lens Cap"
    assert (library / "Tools" / "Lens Cap" / "lens cap.3mf").read_bytes() == b"PK\x03\x04"

    client.imp.set_makerworld_token("bad")
    job = client.imp.run_sync(url, "Tools", name="Other")
    assert job.state == "error" and "expired" in job.error


def test_import_api_validation(client):
    assert client.post("/api/import", json={"url": "https://example.com/x", "category": "Tools"}).status_code == 400
    assert client.post("/api/import", json={"url": "https://www.printables.com/model/3161", "category": ""}).status_code == 400
    assert client.get("/api/import/nope").status_code == 404
