"""Remembered temperatures per printer, filament preset and plate.

The Slice dialog's "Remember" tick saves nozzle / first layer nozzle / bed /
first layer bed for the filament preset and plate in use; next time they fill
in on their own and win over the preset's values. The Settings page lists
them all to edit or delete.

temps.json: {printer: {filament preset: {plate: {field: °C, "saved": time}}}}
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

# field, lowest, highest (same ranges as the slicer's clean_temps)
FIELDS = {"nozzle": (150, 350), "nozzle_first": (150, 350), "bed": (0, 130), "bed_first": (0, 130)}
MAX_ENTRIES = 500


class TempBody(BaseModel):
    printer: str
    filament: str
    plate: str
    nozzle: float | None = None
    nozzle_first: float | None = None
    bed: float | None = None
    bed_first: float | None = None


def _name(value: str, what: str) -> str:
    value = (value or "").strip()
    if not value or len(value) > 200 or any(ord(c) < 32 for c in value):
        raise HTTPException(400, f"Bad {what}")
    return value


def _printer(value: str) -> str:
    if not value.isalnum() or len(value) > 20:
        raise HTTPException(400, "Unknown printer")
    return value


def clean(values: dict) -> dict:
    """The temperature fields that are set, as whole °C; 400 on a value out of range."""
    out = {}
    for key, (lo, hi) in FIELDS.items():
        v = values.get(key)
        if v is None or v == "":
            continue
        try:
            n = float(v)
        except (TypeError, ValueError):
            raise HTTPException(400, f"{key} must be a number") from None
        if not lo <= n <= hi:
            raise HTTPException(400, f"{key.replace('_', ' ')} must be {lo} to {hi} °C")
        out[key] = int(round(n))
    return out


class TempStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()

    def read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self.path)

    def list(self, printer: str = "") -> list[dict]:
        out = []
        for p, fils in self.read().items():
            if printer and p != printer:
                continue
            for fil, plates in (fils or {}).items():
                for plate, vals in (plates or {}).items():
                    out.append({"printer": p, "filament": fil, "plate": plate,
                                **{k: vals[k] for k in FIELDS if k in vals}, "saved": vals.get("saved", 0)})
        out.sort(key=lambda r: (r["printer"], r["filament"].lower(), r["plate"]))
        return out

    def save(self, printer: str, filament: str, plate: str, values: dict) -> dict:
        printer, filament, plate = _printer(printer), _name(filament, "filament"), _name(plate, "plate")
        vals = clean(values)
        if not vals:
            raise HTTPException(400, "Give at least one temperature")
        with self.lock:
            data = self.read()
            if sum(len(p) for f in data.values() for p in f.values()) >= MAX_ENTRIES and \
                    plate not in data.get(printer, {}).get(filament, {}):
                raise HTTPException(400, f"At most {MAX_ENTRIES} saved temperatures")
            data.setdefault(printer, {}).setdefault(filament, {})[plate] = {**vals, "saved": time.time()}
            self._write(data)
        return {"printer": printer, "filament": filament, "plate": plate, **vals}

    def delete(self, printer: str, filament: str, plate: str) -> None:
        with self.lock:
            data = self.read()
            fils = data.get(printer, {})
            if fils.get(filament, {}).pop(plate, None) is None:
                raise HTTPException(404, "Nothing saved for that filament and plate")
            if not fils[filament]:
                del fils[filament]
            if not fils:
                data.pop(printer, None)
            self._write(data)


def make_router(store: TempStore, client=None) -> APIRouter:
    """``client``: the SlicerClient, for the filament presets and plates the Settings page offers."""
    router = APIRouter(prefix="/api/slicer")

    @router.get("/temps")
    def list_temps(printer: str = ""):
        return {"temps": store.list(_printer(printer) if printer else "")}

    @router.put("/temps")
    def save_temp(body: TempBody):
        return store.save(body.printer, body.filament, body.plate, body.model_dump())

    @router.delete("/temps")
    def delete_temp(printer: str, filament: str, plate: str):
        store.delete(printer, filament, plate)
        return {"ok": True}

    @router.get("/filaments")
    def filaments(printer: str):
        """Every filament preset for a printer (any of its machine presets) with its own
        temperatures, and the plates, for the Settings page."""
        if client is None or not client.enabled:
            raise HTTPException(503, "The slicer isn't set up")
        o = client.call("GET", "/options", params={"printer": _printer(printer), "path": ""})
        seen: dict[str, dict] = {}
        for fils in (o.get("filaments") or {}).values():
            for f in fils:
                seen.setdefault(f["name"], {k: f.get(k) for k in ("name", "type", "user", "nozzle", "nozzle_first",
                                                                     "beds", "beds_first")})
        return {"printer": o.get("printer", {}), "bed_types": o.get("bed_types", []),
                "filaments": sorted(seen.values(), key=lambda f: (not f.get("user"), f["name"].lower()))}

    return router
