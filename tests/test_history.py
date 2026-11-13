"""Print history: Moonraker job sync, app-started prints, editing, photos, hooks."""

import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.history import _norm_job_name, job_filament
from app.main import create_app
from app.printers import PrinterHub
from app.printers.moonraker import MoonrakerPrinter

from test_library import by_name, library  # noqa: F401
from test_printers import JPEG, U1, FakeMoonraker


def job(job_id, filename, status="completed", start=None, duration=600.0, used=1000.0, total=1000.0, weights=(3.0,)):
    start = start if start is not None else time.time() - 700
    return {
        "job_id": job_id,
        "filename": filename,
        "status": status,
        "start_time": start,
        "end_time": None if status == "in_progress" else start + duration + 30,
        "print_duration": duration,
        "total_duration": duration + 30,
        "filament_used": used,
        "metadata": {
            "filament_weight": list(weights),
            "filament_used_mm": [total * (w / sum(weights)) for w in weights],
            "filament_total": total,
            "filament_type": ";".join(["PLA"] * len(weights)),
            "filament_colour": ";".join(["#FF0000FF"] * len(weights)),
        },
    }


class HistoryMoonraker(FakeMoonraker):
    def __init__(self):
        super().__init__()
        self.jobs: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/server/history/list":
            start = int(request.url.params.get("start", 0))
            limit = int(request.url.params.get("limit", 50))
            jobs = sorted(self.jobs, key=lambda j: j["start_time"], reverse=True)[start:start + limit]
            return httpx.Response(200, json={"result": {"count": len(self.jobs), "jobs": jobs}})
        return super().handler(request)


@pytest.fixture()
def hfake():
    return HistoryMoonraker()


@pytest.fixture()
def happ(tmp_path, library, hfake):  # noqa: F811
    u1 = MoonrakerPrinter(U1, "http://u1.test:7125", transport=httpx.MockTransport(hfake.handler),
                          wake_camera=lambda: None, rpc=hfake.rpc)
    settings = Settings(library=library, data=tmp_path / "data", password="", scan_interval=0)
    hub = PrinterHub(settings.printers, env={}, drivers={"u1": u1})
    app = create_app(settings, background_scan=False, printers=hub)
    with TestClient(app) as c:
        c.app_ = app
        yield c


def test_job_name_normalised():
    assert _norm_job_name("Body7_PLA_6m30s.gcode") == "body7"
    assert _norm_job_name("10pc Plant Tag Jig_PLA_1h36m.gcode") == "10pc plant tag jig"


def test_job_filament_maps_toolheads_and_skips_unused():
    meta = {"filament_weight": [0, 5.0, 0, 2.0], "filament_used_mm": [0, 1600, 0, 640],
            "filament_type": "PLA;PETG;PLA;TPU", "filament_colour": "#000000;#00FF00;#000000;#FFFFFF"}
    fil = job_filament(meta, {1: 2, 3: 0})
    assert [(f["slot"], f["toolhead"], f["type"], f["grams"]) for f in fil] == [(1, 2, "PETG", 5.0), (3, 0, "TPU", 2.0)]
    assert fil[0]["color"] == "#00FF00"


def test_first_sync_imports_past_jobs_as_backfill(happ, hfake):
    hfake.jobs = [
        job("000001", "bin_2x1_U1_4color.gcode", start=1000.0),
        job("000002", "planter_U1_PETG_4h10m.gcode", status="cancelled", start=2000.0, used=250.0),
        job("000003", "mystery_PLA_5m.gcode", status="klippy_shutdown", start=3000.0),
    ]
    assert happ.post("/api/history/sync").json()["jobs"] == 3
    res = happ.get("/api/history").json()
    assert res["total"] == 3
    rows = {r["job_id"]: r for r in res["prints"]}
    assert all(r["backfill"] for r in rows.values())
    assert rows["000001"]["status"] == "success" and rows["000001"]["progress"] == 1.0
    assert rows["000002"]["status"] == "cancelled" and rows["000002"]["progress"] == 0.25
    assert rows["000003"]["status"] == "failed"
    # matched to library models by file name
    assert rows["000001"]["model_id"] == by_name(happ, "Gridfinity Bin 2x1")["id"]
    assert rows["000001"]["file_id"]
    assert rows["000002"]["model_id"] == next(m["id"] for m in happ.get("/api/models", params={"q": "planter"}).json()["models"])
    assert rows["000003"]["model_id"] is None
    assert res["stats"]["success"] == 1 and res["stats"]["failed"] == 1
    # no photos for old jobs
    assert all(r["photo_url"] is None for r in rows.values())
    # syncing again doesn't duplicate
    happ.post("/api/history/sync")
    assert happ.get("/api/history").json()["total"] == 3


def test_app_print_is_matched_to_its_job_and_photographed(happ, hfake):
    happ.post("/api/history/sync")  # first (empty) sync
    m = happ.get(f"/api/models/{by_name(happ, 'Gridfinity Bin 2x1')['id']}").json()
    f = next(x for x in m["files"] if x["kind"] == "gcode")
    r = happ.post("/api/printers/u1/print", json={"file_id": f["id"], "bed_clear": True, "accept_warnings": True})
    assert r.status_code == 200, r.text
    rows = happ.get("/api/history", params={"model_id": m["id"]}).json()["prints"]
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "printing" and row["source"] == "app" and row["file"] == "bin_2x1_U1_4color.gcode"
    assert row["tool_map"] and row["filament"]

    finished = []
    happ.app_.state.history.on_finish(finished.append)
    hfake.jobs = [job("00000A", "bin_2x1_U1_4color.gcode", start=row["started"] + 5, weights=(1.0, 2.0, 3.0, 4.0))]
    happ.post("/api/history/sync")
    rows = happ.get("/api/history").json()["prints"]
    assert len(rows) == 1  # merged, not a second row
    row = rows[0]
    assert row["job_id"] == "00000A" and row["status"] == "success" and not row["backfill"]
    assert row["photo_url"]
    assert happ.get(row["photo_url"]).content == JPEG
    # listener told once, with physical toolheads after the map
    assert len(finished) == 1 and finished[0]["id"] == row["id"]
    tm = row["tool_map"]
    assert [x["toolhead"] for x in finished[0]["filament"]] == [tm.get(i, i) for i in range(4)]
    happ.post("/api/history/sync")
    assert len(finished) == 1


def test_mark_failed_with_note_survives_sync(happ, hfake):
    hfake.jobs = [job("000001", "bin_2x1_U1_4color.gcode")]
    happ.post("/api/history/sync")
    pid = happ.get("/api/history").json()["prints"][0]["id"]
    r = happ.patch(f"/api/history/{pid}", json={"status": "failed", "note": "came off the bed"})
    assert r.status_code == 200 and r.json()["status"] == "failed" and r.json()["status_by_user"]
    assert happ.patch(f"/api/history/{pid}", json={"status": "bogus"}).status_code == 400
    happ.post("/api/history/sync")
    row = happ.get(f"/api/history/{pid}").json()
    assert row["status"] == "failed" and row["note"] == "came off the bed"


def test_photo_upload_and_set_usage(happ, hfake):
    hfake.jobs = [job("000001", "bin_2x1_U1_4color.gcode")]
    happ.post("/api/history/sync")
    pid = happ.get("/api/history").json()["prints"][0]["id"]
    assert happ.put(f"/api/history/{pid}/photo", content=b"not a picture").status_code == 400
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 20
    r = happ.put(f"/api/history/{pid}/photo", content=png)
    assert r.status_code == 200
    assert happ.get(r.json()["photo_url"]).content == png
    h = happ.app_.state.history
    h.set_usage(pid, grams=12.5, cost=0.31, filament=[{"toolhead": 0, "grams": 12.5, "spool_id": 1}])
    res = happ.get("/api/history").json()
    assert res["prints"][0]["grams"] == 12.5 and res["stats"]["cost"] == pytest.approx(0.31)
    assert res["prints"][0]["usage"]["filament"][0]["spool_id"] == 1


def test_listener_retried_until_it_works(happ, hfake):
    hfake.jobs = [job("000001", "bin_2x1_U1_4color.gcode")]
    calls = []

    def flaky(rec):
        calls.append(rec["id"])
        if len(calls) == 1:
            raise RuntimeError("spools busy")

    happ.app_.state.history.on_finish(flaky)
    happ.post("/api/history/sync")
    happ.post("/api/history/sync")
    happ.post("/api/history/sync")
    assert len(calls) == 2


def test_hide_empty_cancels(happ, hfake):
    hfake.jobs = [job("000001", "bin_2x1_U1_4color.gcode", status="cancelled", duration=0.0, used=0.0, start=1.0),
                  job("000002", "bin_2x1_U1_4color.gcode", start=2.0)]
    happ.post("/api/history/sync")
    assert happ.get("/api/history").json()["total"] == 2
    assert happ.get("/api/history", params={"hide_empty": True}).json()["total"] == 1
