"""HTTP routes for printers. Mounted under /api/printers by app.main."""

from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from . import IDLE_STATES, PrinterError, PrinterHub

log = logging.getLogger("printpantry.printers")


class PrintRequest(BaseModel):
    file_id: str
    bed_clear: bool = False
    accept_warnings: bool = False


def _material(value: str) -> str:
    return "".join(ch for ch in (value or "").upper() if ch.isalnum())


def same_material(a: str, b: str) -> bool:
    """PLA matches PLA, PLA+ and "PLA SnapSpeed"; PLA doesn't match PETG."""
    x, y = _material(a), _material(b)
    if not x or not y:
        return True  # nothing to compare
    return x.startswith(y) or y.startswith(x)


def preflight(printer, f) -> dict:
    """Everything that should be true before a print starts. Blockers stop it,
    warnings need a tick from the user."""
    blockers: list[str] = []
    warnings: list[str] = []
    tools: list[dict] = []
    st = printer.status
    name = printer.profile.name
    if not printer.configured:
        blockers.append(printer.setup_hint or f"{name} isn't set up")
    elif not printer.can_print:
        blockers.append(f"Starting prints on {name} isn't supported yet")
    elif not st.online:
        blockers.append(f"{name} is offline" + (f": {st.message}" if st.message else ""))
    elif st.state not in IDLE_STATES:
        blockers.append(f"{name} is {st.state}" + (f" ({st.message})" if st.message else ""))
    if f.kind not in printer.profile.file_types:
        blockers.append(f"{f.name} isn't a file {name} can print")
    elif f.printer and f.printer != printer.profile.id:
        blockers.append(f"{f.name} was sliced for a different printer")
    elif not f.printer:
        warnings.append(f"Couldn't tell which printer {f.name} was sliced for")

    plates = f.info.plates if f.info else []
    file_fils = plates[0].filaments if plates else []
    heads = {t.index: t for t in st.toolheads}
    for fl in file_fils:
        idx = fl.slot - 1
        t = heads.get(idx)
        row = {
            "index": idx,
            "file": {"type": fl.type, "color": fl.color, "grams": fl.grams},
            "loaded": None,
            "ok": True,
        }
        if st.online and printer.configured:
            if t is None:
                if heads:
                    blockers.append(f"The file uses T{idx + 1}, which this printer doesn't have")
                    row["ok"] = False
            else:
                row["loaded"] = {"type": t.type, "color": t.color, "vendor": t.vendor, "loaded": t.loaded}
                if t.loaded is False:
                    warnings.append(f"T{idx + 1} looks empty, but the file needs {fl.type or 'filament'} there")
                    row["ok"] = False
                elif not same_material(fl.type, t.type):
                    warnings.append(f"T{idx + 1} has {t.type}, but the file was sliced for {fl.type}")
                    row["ok"] = False
        tools.append(row)
    return {"ok": not blockers, "blockers": blockers, "warnings": warnings, "tools": tools}


def make_router(hub: PrinterHub, resolve) -> APIRouter:
    """``resolve(file_id)`` returns (LibFile, Path) or raises 404."""
    router = APIRouter(prefix="/api/printers")
    starting = {pid: threading.Lock() for pid in hub.printers}

    def get(printer_id: str):
        p = hub.get(printer_id)
        if not p:
            raise HTTPException(404, "No such printer")
        return p

    @router.get("")
    def list_printers():
        return {"printers": [p.to_json() for p in hub.printers.values()]}

    @router.get("/{printer_id}")
    def one(printer_id: str):
        return get(printer_id).to_json()

    @router.get("/{printer_id}/snapshot")
    def snapshot(printer_id: str):
        p = get(printer_id)
        if not (p.can_camera and p.configured):
            raise HTTPException(404, "No camera for this printer")
        try:
            data, taken = p.snapshot()
        except PrinterError as exc:
            raise HTTPException(503, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(503, f"Camera unavailable: {exc}") from exc
        headers = {"Cache-Control": "no-store"}
        if taken:
            headers["X-Frame-Time"] = f"{taken:.0f}"
        return Response(data, media_type="image/jpeg", headers=headers)

    @router.get("/{printer_id}/check")
    def check(printer_id: str, file_id: str):
        p = get(printer_id)
        f, _ = resolve(file_id)
        if p.configured:
            p.refresh()
        return {**preflight(p, f), "printer": p.to_json(), "file": f.to_json()}

    @router.post("/{printer_id}/print")
    def start_print(printer_id: str, req: PrintRequest):
        p = get(printer_id)
        f, path = resolve(req.file_id)
        if not req.bed_clear:
            raise HTTPException(400, "Confirm the bed is clear first")
        lock = starting[printer_id]
        if not lock.acquire(blocking=False):
            raise HTTPException(409, "A print is already being sent to this printer")
        try:
            if p.configured:
                p.refresh()
            pre = preflight(p, f)
            if pre["blockers"]:
                raise HTTPException(409, "; ".join(pre["blockers"]))
            if pre["warnings"] and not req.accept_warnings:
                raise HTTPException(409, "Confirm the toolhead warnings first: " + "; ".join(pre["warnings"]))
            log.info("starting %s on %s", f.relpath, printer_id)
            try:
                p.start_print(path, f.name)
            except PrinterError as exc:
                raise HTTPException(502, str(exc)) from exc
            return {"ok": True, "printer": p.to_json()}
        finally:
            lock.release()

    @router.post("/{printer_id}/{action}")
    def action(printer_id: str, action: str):
        p = get(printer_id)
        if action not in ("pause", "resume", "cancel", "light_on", "light_off"):
            raise HTTPException(404, "Unknown action")
        try:
            p.action(action)
        except PrinterError as exc:
            raise HTTPException(502, str(exc)) from exc
        return p.to_json()

    return router
