"""HTTP routes for printers. Mounted under /api/printers by app.main."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from . import IDLE_STATES, PrinterError, PrinterHub

log = logging.getLogger("printpantry.printers")


class ColorRequest(BaseModel):
    color: str | None = None  # "#RRGGBB", or null to go back to the printer's colour


class CalibrationRequest(BaseModel):
    confirmed: bool = False  # the user ticked the "bed is clear" style check
    params: dict = {}


class PrintRequest(BaseModel):
    file_id: str
    bed_clear: bool = False
    accept_warnings: bool = False
    # file filament (0-based) -> toolhead (0-based); missing = Print Pantry's default pick
    tool_map: dict[int, int] | None = None


def _material(value: str) -> str:
    return "".join(ch for ch in (value or "").upper() if ch.isalnum())


def same_material(a: str, b: str) -> bool:
    """PLA matches PLA, PLA+ and "PLA SnapSpeed"; PLA doesn't match PETG."""
    x, y = _material(a), _material(b)
    if not x or not y:
        return True  # nothing to compare
    return x.startswith(y) or y.startswith(x)


def default_tool_map(file_fils, heads: dict) -> dict[int, int]:
    """Pick a toolhead for each of the file's filaments. Its own slot when that
    holds the right material; otherwise a loaded head with that material that no
    other filament of this file is using (so colours of a multi-colour print
    stay apart); otherwise its own slot, and the check warns."""
    def fits(fl, h):
        return h is not None and h.loaded is not False and bool(h.type) and same_material(fl.type, h.type)

    out: dict[int, int] = {}
    for fl in file_fils:
        if fits(fl, heads.get(fl.slot - 1)):
            out[fl.slot - 1] = fl.slot - 1
    taken = set(out.values()) | {fl.slot - 1 for fl in file_fils}
    for fl in file_fils:
        i = fl.slot - 1
        if i in out:
            continue
        spare = next((h.index for h in sorted(heads.values(), key=lambda h: h.index)
                      if h.index not in taken and fits(fl, h)), None)
        out[i] = spare if spare is not None else i
        if spare is not None:
            taken.add(spare)
    return out


def preflight(printer, f, filaments=None, tool_map: dict[int, int] | None = None) -> dict:
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
    elif printer.calibration_running:
        blockers.append(f"{name} is running {printer.calibrating['name']}")
    if f is None:
        pass
    elif f.kind not in printer.profile.file_types:
        blockers.append(f"{f.name} isn't a file {name} can print")
    elif f.printer and f.printer != printer.profile.id:
        blockers.append(f"{f.name} was sliced for a different printer")
    elif not f.printer:
        warnings.append(f"Couldn't tell which printer {f.name} was sliced for")

    if f is None:
        file_fils = filaments or []
    else:
        plates = f.info.plates if f.info else []
        file_fils = plates[0].filaments if plates else []
    heads = {t.index: t for t in st.toolheads}
    # Library files get a suggested toolhead per filament; calibration prints
    # (f is None) are sliced for one specific toolhead and stay on it.
    chosen = default_tool_map(file_fils, heads) if f is not None else {fl.slot - 1: fl.slot - 1 for fl in file_fils}
    if tool_map:
        chosen.update({int(k): int(v) for k, v in tool_map.items() if int(k) in chosen})
    for fl in file_fils:
        file_idx = fl.slot - 1
        idx = chosen.get(file_idx, file_idx)
        t = heads.get(idx)
        row = {
            "index": idx,
            "file_index": file_idx,
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
    return {"ok": not blockers, "blockers": blockers, "warnings": warnings, "tools": tools, "tool_map": chosen}


def make_router(hub: PrinterHub, resolve) -> APIRouter:
    """``resolve(file_id)`` returns (LibFile, Path) or raises 404."""
    router = APIRouter(prefix="/api/printers")
    starting = hub.starting

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
    def check(printer_id: str, file_id: str, map: str = ""):
        """``map`` is optional "0:1,1:2" (file filament:toolhead, 0-based)."""
        p = get(printer_id)
        f, _ = resolve(file_id)
        if p.configured:
            p.refresh()
        try:
            tool_map = {int(a): int(b) for a, b in (pair.split(":") for pair in map.split(",") if pair)}
        except ValueError as exc:
            raise HTTPException(400, "map must look like 0:1,1:2") from exc
        return {**preflight(p, f, tool_map=tool_map), "printer": p.to_json(), "file": f.to_json()}

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
            pre = preflight(p, f, tool_map=req.tool_map)
            if pre["blockers"]:
                raise HTTPException(409, "; ".join(pre["blockers"]))
            if pre["warnings"] and not req.accept_warnings:
                raise HTTPException(409, "Confirm the toolhead warnings first: " + "; ".join(pre["warnings"]))
            log.info("starting %s on %s", f.relpath, printer_id)
            try:
                p.start_print(path, f.name, tool_map=pre["tool_map"])
            except PrinterError as exc:
                raise HTTPException(502, str(exc)) from exc
            return {"ok": True, "printer": p.to_json()}
        finally:
            lock.release()

    @router.put("/{printer_id}/toolheads/{index}/color")
    def set_color(printer_id: str, index: int, req: ColorRequest):
        p = get(printer_id)
        try:
            p.set_color(index, req.color)
        except PrinterError as exc:
            raise HTTPException(400, str(exc)) from exc
        return p.to_json()

    @router.post("/{printer_id}/calibrations/{cid}")
    def calibrate(printer_id: str, cid: str, req: CalibrationRequest):
        p = get(printer_id)
        if not p.configured:
            raise HTTPException(409, p.setup_hint or f"{p.profile.name} isn't set up")
        if not req.confirmed:
            raise HTTPException(400, "Confirm the printer is ready first")
        lock = starting[printer_id]
        if not lock.acquire(blocking=False):
            raise HTTPException(409, "A print is being sent to this printer")
        try:
            p.start_calibration(cid, req.params)
        except PrinterError as exc:
            raise HTTPException(409, str(exc)) from exc
        finally:
            lock.release()
        return p.to_json()

    @router.post("/{printer_id}/{action}")
    def action(printer_id: str, action: str):
        p = get(printer_id)
        if action not in ("pause", "resume", "cancel", "light_on", "light_off", "estop"):
            raise HTTPException(404, "Unknown action")
        try:
            p.action(action)
        except PrinterError as exc:
            raise HTTPException(502, str(exc)) from exc
        return p.to_json()

    return router
