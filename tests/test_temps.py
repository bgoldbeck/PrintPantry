"""Remembered temperatures per printer, filament preset and plate."""

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.temps import TempStore, make_router


def test_store_save_list_delete(tmp_path):
    s = TempStore(tmp_path / "t.json")
    s.save("u1", "Snapmaker PLA", "Textured PEI Plate", {"nozzle": 215, "bed": 60.4, "bed_first": None})
    s.save("u1", "Snapmaker PLA", "Cool Plate", {"bed": 35})
    s.save("p1s", "Bambu PLA Basic", "Textured PEI Plate", {"nozzle_first": 225})
    rows = s.list("u1")
    assert [(r["filament"], r["plate"]) for r in rows] == [("Snapmaker PLA", "Cool Plate"), ("Snapmaker PLA", "Textured PEI Plate")]
    assert rows[1]["nozzle"] == 215 and rows[1]["bed"] == 60 and "bed_first" not in rows[1]
    assert len(s.list()) == 3
    s.save("u1", "Snapmaker PLA", "Cool Plate", {"bed": 40})  # replaces
    assert s.list("u1")[0]["bed"] == 40
    s.delete("u1", "Snapmaker PLA", "Cool Plate")
    s.delete("u1", "Snapmaker PLA", "Textured PEI Plate")
    assert s.list("u1") == [] and "u1" not in s.read()
    with pytest.raises(HTTPException):
        s.delete("u1", "Snapmaker PLA", "Cool Plate")


@pytest.mark.parametrize("vals", [{}, {"nozzle": 400}, {"bed": -1}, {"nozzle": "hot"}])
def test_store_rejects(tmp_path, vals):
    with pytest.raises(HTTPException):
        TempStore(tmp_path / "t.json").save("u1", "PLA", "Cool Plate", vals)


class FakeSlicer:
    enabled = True

    def call(self, method, path, params=None):
        assert path == "/options" and params["path"] == ""
        f = {"name": "PLA A", "type": "PLA", "user": False, "nozzle": 220, "nozzle_first": 220,
             "beds": {"Cool Plate": 35}, "beds_first": {"Cool Plate": 35}}
        return {"printer": {"id": params["printer"]}, "bed_types": [{"name": "Cool Plate", "label": "Cool Plate"}],
                "filaments": {"M1": [f], "M2": [f, {**f, "name": "PETG B", "user": True}]}}


def test_api(tmp_path):
    app = FastAPI()
    app.include_router(make_router(TempStore(tmp_path / "t.json"), FakeSlicer()))
    c = TestClient(app)
    r = c.put("/api/slicer/temps", json={"printer": "u1", "filament": "PLA A", "plate": "Cool Plate", "bed": 45})
    assert r.status_code == 200 and r.json()["bed"] == 45
    assert c.put("/api/slicer/temps", json={"printer": "u/1", "filament": "x", "plate": "y", "bed": 45}).status_code == 400
    assert c.get("/api/slicer/temps?printer=u1").json()["temps"][0]["bed"] == 45
    fils = c.get("/api/slicer/filaments?printer=u1").json()["filaments"]
    assert [f["name"] for f in fils] == ["PETG B", "PLA A"]
    assert c.delete("/api/slicer/temps", params={"printer": "u1", "filament": "PLA A", "plate": "Cool Plate"}).json() == {"ok": True}
    assert c.get("/api/slicer/temps").json()["temps"] == []
