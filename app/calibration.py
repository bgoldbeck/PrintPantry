"""Filament flow ratio calibration (the Printers page's flow wizard).

1. The slicer container slices one of OrcaSlicer's flow test plates for the
   chosen printer, toolhead and filament preset (slicer/calibration.py). Each
   block on the plate prints at a different flow ratio. The file is saved in
   the library's hidden ``.calibration`` folder.
2. The app sends it to the printer with the same checks as any print
   (printer idle, "bed is clear" tick, loaded filament matches).
3. The user picks the block with the smoothest top. Its flow ratio is saved
   per printer and filament preset in ``calibration.json`` in the data volume.
4. The Slice dialog passes saved ratios to the slicer, which writes them into
   the filament preset (``filament_flow_ratio``) for that slice. The ratio can
   also be downloaded as a filament preset for the desktop slicers.

Mounted under /api/calibration by app.main.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from .printers import PrinterError
from .printers.api import preflight

log = logging.getLogger("printpantry.calibration")
CALIB_DIR = ".calibration"
KEEP_TESTS = 20
KEEP_HISTORY = 10


class FlowTestRequest(BaseModel):
    printer: str = "u1"
    toolhead: int = 1
    machine: str
    process: str
    filament: str
    bed_type: str = ""
    test: str = "yolo"
    use_saved: bool = True  # start from the saved ratio for this filament, if any


class FlowPrintRequest(BaseModel):
    bed_clear: bool = False
    accept_warnings: bool = False


class FlowPick(BaseModel):
    test_id: str
    block: str  # block name, e.g. "flowrate_m0.01"


class FlowManual(BaseModel):
    printer: str
    filament: str
    ratio: float


def key(printer: str, filament: str) -> str:
    return f"{printer}|{filament}"


class CalibrationStore:
    """calibration.json: saved flow ratios and recent flow tests."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()

    def read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        data.setdefault("flow", {})
        data.setdefault("tests", [])
        return data

    def write(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self.path)

    def flow_ratios(self, printer: str) -> dict[str, float]:
        return {v["filament"]: v["ratio"] for v in self.read()["flow"].values() if v.get("printer") == printer}

    def save_ratio(self, printer: str, filament: str, ratio: float, **info) -> dict:
        with self.lock:
            data = self.read()
            k = key(printer, filament)
            old = data["flow"].get(k)
            history = (old or {}).get("history", [])
            if old:
                history = ([{"ratio": old["ratio"], "at": old.get("at"), "test": old.get("test", "")}] + history)[:KEEP_HISTORY]
            entry = {"printer": printer, "filament": filament, "ratio": round(ratio, 4), "at": time.time(),
                     **info, "history": history}
            data["flow"][k] = entry
            self.write(data)
            return entry

    def forget(self, printer: str, filament: str) -> None:
        with self.lock:
            data = self.read()
            data["flow"].pop(key(printer, filament), None)
            self.write(data)

    def add_test(self, test: dict) -> dict:
        with self.lock:
            data = self.read()
            if not any(t["id"] == test["id"] for t in data["tests"]):
                data["tests"] = ([test] + data["tests"])[:KEEP_TESTS]
                self.write(data)
            return test

    def test(self, test_id: str) -> dict | None:
        return next((t for t in self.read()["tests"] if t["id"] == test_id), None)

    def update_test(self, test_id: str, **changes) -> dict:
        with self.lock:
            data = self.read()
            for t in data["tests"]:
                if t["id"] == test_id:
                    t.update(changes)
                    self.write(data)
                    return t
        raise HTTPException(404, "No such flow test")

    def drop_test(self, test_id: str) -> None:
        with self.lock:
            data = self.read()
            data["tests"] = [t for t in data["tests"] if t["id"] != test_id]
            self.write(data)


def block_label(ratio: float, linear: bool) -> str:
    """What is embossed on the block: +0.01 (linear tests) or +5 (percent tests)."""
    off = ratio - 1
    if linear:
        return f"{off:+.3f}".rstrip("0").rstrip(".") if abs(off) > 1e-9 else "0"
    return f"{off * 100:+.0f}" if abs(off) > 1e-9 else "0"


def test_from_job(job: dict) -> dict:
    calib = job.get("calib") or {}
    linear = bool(calib.get("linear", True))
    blocks = [{"name": b["name"], "ratio": b["ratio"], "flow": b["flow"], "label": block_label(b["ratio"], linear)}
              for b in calib.get("blocks", [])]
    return {
        "id": job["id"],
        "printer": job["printer"],
        "toolhead": calib.get("toolhead", 1),
        "filament": calib.get("filament", ""),
        "type": calib.get("type", ""),
        "test": calib.get("test", ""),
        "label": calib.get("label", ""),
        "linear": linear,
        "base": calib.get("base"),
        "blocks": blocks,
        "file": (job.get("outputs") or [""])[0],
        "created": time.time(),
        "printed": None,
        "picked": None,
    }


def preset_file(filament: str, ratio: float) -> dict:
    """A filament preset that inherits the original and changes only the flow ratio."""
    return {
        "type": "filament",
        "name": f"{filament} (flow {ratio:g})",
        "inherits": filament,
        "from": "User",
        "instantiation": "true",
        "filament_flow_ratio": [f"{ratio:g}"],
        "filament_settings_id": [f"{filament} (flow {ratio:g})"],
        "version": "2.0.0.0",
    }


def make_router(store: CalibrationStore, slicer, hub, library: Path, starting: dict) -> APIRouter:
    router = APIRouter(prefix="/api/calibration")

    def printer(pid: str):
        p = hub.get(pid)
        if not p:
            raise HTTPException(404, "No such printer")
        return p

    def test_path(test: dict) -> Path:
        rel = Path(test.get("file") or "")
        if not rel.parts or rel.parts[0] != CALIB_DIR or ".." in rel.parts or rel.is_absolute():
            raise HTTPException(404, "This test has no print file")
        path = library / rel
        if not path.is_file():
            raise HTTPException(404, "The test print file is gone; make the test again")
        return path

    @router.get("")
    def overview():
        data = store.read()
        flow = sorted(data["flow"].values(), key=lambda v: -(v.get("at") or 0))
        tests, available = [], False
        if slicer.enabled:
            try:
                tests = slicer.call("GET", "/calibration/flow").get("tests", [])
                available = any(t.get("available") for t in tests)
            except HTTPException as exc:
                log.info("slicer has no calibration support: %s", exc.detail)
        return {"flow": flow, "tests": data["tests"], "flow_tests": tests, "slicer": available}

    @router.get("/flow/options")
    def options(printer: str = "u1"):
        opts = slicer.call("GET", "/options", params={"printer": printer, "path": ""})
        return {k: opts[k] for k in ("printer", "machines", "processes", "filaments", "defaults", "bed_types") if k in opts}

    @router.post("/flow/jobs")
    def start(req: FlowTestRequest):
        body = req.model_dump(exclude={"use_saved"})
        body["flow_ratio"] = store.flow_ratios(req.printer).get(req.filament) if req.use_saved else None
        job = slicer.call("POST", "/calibration/flow", json=body)
        log.info("flow test %s: %s on %s T%d", job.get("id"), req.filament, req.printer, req.toolhead)
        return job

    @router.get("/flow/jobs/{job_id}")
    def job(job_id: str):
        if not job_id.isalnum():
            raise HTTPException(404, "No such job")
        data = slicer.call("GET", f"/jobs/{job_id}")
        if data.get("state") == "done" and data.get("calib"):
            data["test"] = store.test(job_id) or store.add_test(test_from_job(data))
        return data

    @router.get("/flow/tests/{test_id}/check")
    def check(test_id: str):
        t = store.test(test_id) or {}
        if not t:
            raise HTTPException(404, "No such flow test")
        p = printer(t["printer"])
        test_path(t)
        if p.configured:
            p.refresh()
        fil = SimpleNamespace(slot=t["toolhead"], type=t.get("type", ""), color="", grams=None)
        return {**preflight(p, None, [fil]), "printer": p.to_json(), "test": t}

    @router.post("/flow/tests/{test_id}/print")
    def print_test(test_id: str, req: FlowPrintRequest):
        t = store.test(test_id)
        if not t:
            raise HTTPException(404, "No such flow test")
        p = printer(t["printer"])
        path = test_path(t)
        if not req.bed_clear:
            raise HTTPException(400, "Confirm the bed is clear first")
        lock = starting[p.profile.id]
        if not lock.acquire(blocking=False):
            raise HTTPException(409, "A print is already being sent to this printer")
        try:
            if p.configured:
                p.refresh()
            fil = SimpleNamespace(slot=t["toolhead"], type=t.get("type", ""), color="", grams=None)
            pre = preflight(p, None, [fil])
            if pre["blockers"]:
                raise HTTPException(409, "; ".join(pre["blockers"]))
            if pre["warnings"] and not req.accept_warnings:
                raise HTTPException(409, "Confirm the toolhead warnings first: " + "; ".join(pre["warnings"]))
            if path.suffix.lower() == ".3mf" and "sliced3mf" not in p.profile.file_types:
                raise HTTPException(409, "This printer can't print that file")
            log.info("printing flow test %s on %s", path.name, p.profile.id)
            try:
                # A Bambu test is sliced for filament 1; the chosen AMS tray is picked at print time.
                tool_map = {0: t["toolhead"] - 1} if getattr(p, "ams_mapping", False) else None
                p.start_print(path, path.name, tool_map=tool_map)
            except PrinterError as exc:
                raise HTTPException(502, str(exc)) from exc
        finally:
            lock.release()
        return {"ok": True, "test": store.update_test(test_id, printed=time.time()), "printer": p.to_json()}

    @router.post("/flow/pick")
    def pick(req: FlowPick):
        t = store.test(req.test_id)
        if not t:
            raise HTTPException(404, "No such flow test")
        block = next((b for b in t["blocks"] if b["name"] == req.block), None)
        if not block:
            raise HTTPException(400, "That block isn't on this test")
        entry = store.save_ratio(t["printer"], t["filament"], block["flow"], test=t["label"], toolhead=t["toolhead"],
                                 block=block["label"], base=t["base"], type=t.get("type", ""))
        test = store.update_test(t["id"], picked=block["name"])
        edge = block["name"] in (t["blocks"][0]["name"], t["blocks"][-1]["name"])
        return {"saved": entry, "test": test, "edge": edge}

    @router.put("/flow/ratio")
    def set_ratio(req: FlowManual):
        if not 0.5 <= req.ratio <= 1.5:
            raise HTTPException(400, "The flow ratio must be between 0.5 and 1.5")
        return store.save_ratio(req.printer, req.filament, req.ratio, test="Set by hand")

    @router.delete("/flow/ratio")
    def forget(printer: str, filament: str):
        store.forget(printer, filament)
        return {"ok": True}

    @router.delete("/flow/tests/{test_id}")
    def drop(test_id: str):
        store.drop_test(test_id)
        return {"ok": True}

    @router.get("/flow/preset")
    def preset(printer: str, filament: str):
        ratio = store.flow_ratios(printer).get(filament)
        if ratio is None:
            raise HTTPException(404, "No saved flow ratio for that filament")
        data = preset_file(filament, ratio)
        name = "".join(c if c.isalnum() or c in " -_.()@" else "_" for c in data["name"]) + ".json"
        return Response(json.dumps(data, indent=4), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    return router
