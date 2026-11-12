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

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

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


class SlicerClient:
    def __init__(self, base_url: str, timeout: float = 15.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.base)

    def call(self, method: str, path: str, **kw) -> dict:
        if not self.enabled:
            raise HTTPException(503, "Slicing isn't set up (PP_SLICER_URL is empty)")
        try:
            res = httpx.request(method, self.base + path, timeout=self.timeout, **kw)
        except httpx.HTTPError as exc:
            raise HTTPException(503, f"The slicer isn't reachable: {exc}") from exc
        try:
            data = res.json()
        except ValueError:
            data = {"detail": res.text[:300]}
        if res.status_code >= 400:
            raise HTTPException(res.status_code, data.get("detail") or "Slicer error")
        return data


def make_router(client: SlicerClient, resolve, rescan) -> APIRouter:
    """``resolve(file_id)`` -> (LibFile, Path) or 404; ``rescan()`` scans the library (blocking)."""
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
        return client.call("GET", "/options", params={"printer": printer, "path": f.relpath})

    @router.post("/jobs")
    def start(req: SliceRequest):
        f = sliceable(req.file_id)
        body = req.model_dump(exclude={"file_id"})
        body["path"] = f.relpath
        job = client.call("POST", "/jobs", json=body)
        log.info("slice job %s: %s for %s", job.get("id"), f.relpath, req.printer)
        return job

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
