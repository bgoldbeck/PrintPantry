"""Slicing: routes that hand project 3MFs and STLs to the slicer container.

The slicer (slicer/service.py, OrcaSlicer's command line) runs as a second
container on the same compose network; PP_SLICER_URL points at it. When it is
not set or not reachable, the routes say so and the UI hides the Slice buttons.
Mounted under /api/slicer by app.main.
"""

from __future__ import annotations

import logging
import os
import threading
import time

import httpx
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from . import process_settings as PS

log = logging.getLogger("printpantry.slicing")
SLICEABLE = ("project3mf", "stl")


class SliceRequest(BaseModel):
    file_id: str
    printer: str = "u1"
    mode: str = "presets"  # "project": the 3MF's own settings; "presets": the ones picked below
    machine: str = ""
    process: str = ""
    filaments: list[str] = []
    bed_type: str = ""
    plate: int = 0
    extra_files: list[str] = []  # more STLs of the model, sliced together with file_id
    assign: dict[str, int] = {}  # object key -> filament slot (1-based); see slicer/objects.py
    colours: list[str] = []  # colour per filament slot, e.g. what the toolhead has loaded
    keep_positions: bool = True  # STLs together keep their places (colours of one model)
    copies: int = 1  # the plate this many times (the slicer arranges them)
    overrides: dict = {}  # process setting -> value (see process_settings.py)
    # Per filament slot: nozzle / nozzle_first / bed / bed_first in °C (only the changed ones).
    temps: list[dict] = []
    author_profile: bool = False  # presets mode: carry the 3MF's own process settings over first
    # Send the result to the printer when it's sliced (the user ticked "bed is clear").
    print_after: bool = False
    # STLs without a saved print orientation are turned to the least overhang.
    auto_orient: bool = True


class ProcessPresetBody(BaseModel):
    printer: str
    name: str
    process: str = ""
    machine: str = ""
    overrides: dict = {}


class SlicerClient:
    def __init__(self, base_url: str, timeout: float = 15.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.base)

    def _request(self, method: str, path: str, timeout: float | None = None, **kw) -> httpx.Response:
        if not self.enabled:
            raise HTTPException(503, "Slicing isn't set up (PP_SLICER_URL is empty)")
        try:
            return httpx.request(method, self.base + path, timeout=timeout or self.timeout, **kw)
        except httpx.HTTPError as exc:
            raise HTTPException(503, f"The slicer isn't reachable: {exc}") from exc

    def raw(self, method: str, path: str, timeout: float | None = None, **kw) -> bytes:
        res = self._request(method, path, timeout, **kw)
        if res.status_code >= 400:
            try:
                detail = res.json().get("detail")
            except ValueError:
                detail = res.text[:300]
            raise HTTPException(res.status_code, detail or "Slicer error")
        return res.content

    def call(self, method: str, path: str, timeout: float | None = None, **kw) -> dict:
        res = self._request(method, path, timeout, **kw)
        try:
            data = res.json()
        except ValueError:
            data = {"detail": res.text[:300]}
        if res.status_code >= 400:
            raise HTTPException(res.status_code, data.get("detail") or "Slicer error")
        return data


def make_router(client: SlicerClient, resolve, rescan, flow_ratios=None,
                presets: PS.ProcessPresets | None = None, hub=None, on_start=None,
                orientation=None) -> APIRouter:
    """``resolve(file_id)`` -> (LibFile, Path) or 404; ``rescan()`` scans the library (blocking);
    ``flow_ratios(printer)`` -> {filament preset: calibrated flow ratio};
    ``presets``: saved process settings per printer;
    ``hub`` / ``on_start``: for "print when sliced" (see printers.api.send_print);
    ``orientation(file_id)`` -> the STL's saved print rotation or None."""
    flow_ratios = flow_ratios or (lambda printer: {})
    orientation = orientation or (lambda file_id: None)
    router = APIRouter(prefix="/api/slicer")
    rescanned: set[str] = set()
    lock = threading.Lock()

    def sliceable(file_id: str):
        f, _ = resolve(file_id)
        if f.kind not in SLICEABLE:
            raise HTTPException(400, f"{f.name} can't be sliced; pick a project 3MF or an STL")
        return f

    @router.get("")
    def status():
        if not client.enabled:
            return {"available": False, "reason": "not set up"}
        try:
            return {"available": True, **client.call("GET", "/health")}
        except HTTPException as exc:
            return {"available": False, "reason": exc.detail}

    @router.get("/options")
    def options(file_id: str, printer: str = "u1"):
        f = sliceable(file_id)
        opts = client.call("GET", "/options", params={"printer": printer, "path": f.relpath})
        opts["calibrated"] = flow_ratios(printer)
        return opts

    def object_files(file_id: str, extra: str) -> list:
        files = [sliceable(file_id)]
        for x in [x for x in extra.split(",") if x]:
            e, _ = resolve(x)
            if e.kind != "stl":
                raise HTTPException(400, "Only STLs can be added to the plate")
            files.append(e)
        return files

    def saved_orient(files) -> dict:
        """{relpath: rotation} of the STLs that have a saved print orientation."""
        out = {}
        for f in files:
            m = orientation(f.id) if f.kind == "stl" else None
            if m:
                out[f.relpath] = m
        return out

    def objects_body(file_id: str, extra: str, auto: int) -> dict:
        files = object_files(file_id, extra)
        return {"paths": [f.relpath for f in files], "orient": saved_orient(files), "auto_orient": bool(auto)}

    @router.get("/objects")
    def objects(file_id: str, extra: str = "", auto: int = 0):
        """The objects / parts / STL bodies that can each get their own filament."""
        return client.call("POST", "/objects", timeout=120, json=objects_body(file_id, extra, auto))

    @router.get("/objects/mesh")
    def objects_mesh(file_id: str, extra: str = "", auto: int = 0):
        data = client.raw("POST", "/objects/mesh", timeout=120, json=objects_body(file_id, extra, auto))
        return Response(data, media_type="application/octet-stream", headers={"Cache-Control": "no-store"})

    @router.post("/jobs")
    def start(req: SliceRequest):
        f = sliceable(req.file_id)
        body = req.model_dump(exclude={"file_id", "extra_files", "author_profile"})
        body["path"] = f.relpath
        overrides = PS.normalise(req.overrides)
        if req.author_profile and req.mode == "presets" and f.kind == "project3mf":
            # The author's settings (e.g. a MakerWorld profile) on top of this
            # printer's process preset, and the user's changes on top of those.
            own = client.call("GET", "/process", params={"printer": req.printer, "mode": "project", "path": f.relpath})
            overrides = {**PS.carry_over(own.get("values", {})), **overrides}
        body["overrides"] = overrides
        files = object_files(req.file_id, ",".join(req.extra_files))
        if req.extra_files:
            body["extra_paths"] = [x.relpath for x in files[1:]]
        body["orient"] = saved_orient(files)
        if req.mode == "presets":
            saved = flow_ratios(req.printer)
            body["flow_ratios"] = {n: saved[n] for n in req.filaments if n in saved}
        body.pop("print_after", None)
        printer = hub.get(req.printer) if (req.print_after and hub) else None
        if req.print_after and not (printer and printer.configured and printer.can_print):
            raise HTTPException(400, "That printer can't be sent prints from here")
        job = client.call("POST", "/jobs", json=body)
        log.info("slice job %s: %s for %s", job.get("id"), f.relpath, req.printer)
        if printer:
            printer.sending = {"state": "slicing", "file": f.name, "file_id": f.id, "job": job.get("id"),
                               "started": time.time(), "message": ""}
            threading.Thread(target=print_when_sliced, args=(printer, job.get("id")),
                             name=f"slice-print-{job.get('id')}", daemon=True).start()
        return job

    def print_when_sliced(printer, job_id: str, every: float = 2.0, give_up: float = 3 * 3600) -> None:
        """Wait for a slice job in the background, then send what it made to the printer."""
        from .printers.api import SendRefused, send_print
        from .scanner import make_id

        def failed(message: str) -> None:
            log.warning("print after slice %s: %s", job_id, message)
            printer.sending = {**(printer.sending or {}), "state": "failed", "finished": time.time(), "message": message}

        end = time.time() + give_up
        data: dict = {}
        while time.time() < end:
            try:
                data = client.call("GET", f"/jobs/{job_id}")
            except HTTPException as exc:
                data = {"state": "?", "message": exc.detail}
            if data.get("state") in ("done", "failed"):
                break
            time.sleep(every)
        if (printer.sending or {}).get("job") != job_id:
            return  # another send replaced this one
        if data.get("state") != "done":
            return failed(f"Slicing failed: {data.get('message') or 'it took too long'}")
        outputs = data.get("outputs") or []
        if len(outputs) != 1:
            return failed(f"Sliced {len(outputs)} plates; pick the one to print from the model page")
        with lock:
            rescanned.add(job_id)
        rescan()
        try:
            f, path = resolve(make_id(outputs[0]))
        except HTTPException:
            return failed(f"Couldn't find {outputs[0]} in the library after slicing")
        try:
            send_print(hub, printer, f, path, on_start=on_start)
        except SendRefused as exc:
            failed(f"Sliced, but not printed: {exc}")
        except Exception as exc:  # noqa: BLE001 - shown on the printer card
            failed(f"Sliced, but not printed: {exc}")

    @router.get("/orient")
    def auto_orient(file_id: str):
        """What auto-orient would do with this STL (a rotation, see /api/files/{id}/orientation)."""
        f, _ = resolve(file_id)
        if f.kind != "stl":
            raise HTTPException(400, "Only STLs can be turned for printing")
        return client.call("POST", "/orient", timeout=120, json={"path": f.relpath})

    @router.get("/process")
    def process_values(printer: str = "u1", process: str = "", file_id: str = "", mode: str = "presets"):
        """The values the process settings panel starts from: the picked
        process preset's, or the project's own (mode "project")."""
        params = {"printer": printer, "name": process, "mode": "project" if mode == "project" else "presets"}
        if mode == "project":
            params["path"] = sliceable(file_id).relpath
        data = client.call("GET", "/process", params=params)
        cat = PS.catalog()
        vals = data.get("values", {})
        data["values"] = {k: (v[0] if isinstance(v, list) and v else v) for k, v in vals.items() if k in cat}
        return data

    def printer_id(printer: str) -> str:
        if not printer.isalnum() or len(printer) > 20:
            raise HTTPException(400, "Unknown printer")
        return printer

    @router.get("/process-presets")
    def list_presets():
        return {"presets": presets.list() if presets else []}

    @router.put("/process-presets")
    def save_preset(body: ProcessPresetBody):
        if not presets:
            raise HTTPException(503, "Presets aren't set up")
        return presets.save(body.name, printer_id(body.printer), body.process, body.machine, body.overrides)

    @router.delete("/process-presets")
    def delete_preset(name: str):
        if not presets:
            raise HTTPException(503, "Presets aren't set up")
        presets.delete(name)
        return {"ok": True}

    @router.get("/jobs/{job_id}")
    def job(job_id: str):
        if not job_id.isalnum():
            raise HTTPException(404, "No such job")
        data = client.call("GET", f"/jobs/{job_id}")
        # New files are in the library now; scan once so the page shows them
        # as soon as it reloads.
        if data.get("state") == "done":
            with lock:
                first = job_id not in rescanned
                rescanned.add(job_id)
            if first:
                rescan()
        return data

    return router


def client_from_env() -> SlicerClient:
    return SlicerClient(os.environ.get("PP_SLICER_URL", ""))
