"""Process settings for the Slice dialog: OrcaSlicer's process tab as
overrides on top of the picked process preset, and named sets of them saved
per printer ("my presets").

The settings shown (tabs, groups, labels, choices) come from
static/process_catalog.json, generated from OrcaSlicer's source by
tools/gen_process_catalog.py. Only settings in it are passed to the slicer.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from fastapi import HTTPException

CATALOG_PATH = Path(__file__).parent / "static" / "process_catalog.json"
MAX_PRESETS = 100
NAME = re.compile(r"^[^\x00-\x1f/\\]{1,60}$")


def load_catalog(path: Path = CATALOG_PATH) -> dict[str, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {o["key"]: o for t in data["tabs"] for g in t["groups"] for o in g["options"]}


_CATALOG: dict[str, dict] | None = None


def catalog() -> dict[str, dict]:
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = load_catalog()
    return _CATALOG


def _number(raw, label: str, opt: dict, integer: bool = False) -> float:
    try:
        v = float(str(raw).strip())
    except ValueError:
        raise HTTPException(400, f"{label}: {raw!r} isn't a number") from None
    if v != v or v in (float("inf"), float("-inf")):
        raise HTTPException(400, f"{label}: {raw!r} isn't a number")
    if integer and not v.is_integer():
        raise HTTPException(400, f"{label} must be a whole number")
    lo, hi = opt.get("min"), opt.get("max")
    if lo is not None and v < lo:
        raise HTTPException(400, f"{label} must be at least {lo:g}")
    if hi is not None and v > hi:
        raise HTTPException(400, f"{label} must be at most {hi:g}")
    return v


def _fmt(v: float) -> str:
    return f"{v:.6f}".rstrip("0").rstrip(".") if not float(v).is_integer() else str(int(v))


def normalise_value(key: str, raw) -> str | list[str]:
    opt = catalog().get(key)
    if not opt:
        raise HTTPException(400, f"{key!r} isn't a process setting that can be changed here")
    label, kind = opt["label"], opt["type"]
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    if kind == "bool":
        if isinstance(raw, bool):
            value = "1" if raw else "0"
        elif str(raw).strip().lower() in ("1", "true", "yes", "on"):
            value = "1"
        elif str(raw).strip().lower() in ("0", "false", "no", "off", ""):
            value = "0"
        else:
            raise HTTPException(400, f"{label}: expected on or off")
    elif kind == "int":
        value = _fmt(_number(raw, label, opt, integer=True))
    elif kind == "float":
        value = _fmt(_number(raw, label, opt))
    elif kind == "percent":
        value = _fmt(_number(str(raw).strip().rstrip("%"), label, opt)) + "%"
    elif kind == "floatpct":
        s = str(raw).strip()
        if s.endswith("%"):
            value = _fmt(_number(s[:-1], label, {})) + "%"
        else:
            value = _fmt(_number(s, label, opt))
    elif kind == "enum":
        value = str(raw)
        if value not in [c[0] for c in opt.get("choices", [])]:
            raise HTTPException(400, f"{label}: {value!r} isn't one of the choices")
    else:  # string
        value = str(raw)
        if len(value) > 200 or "\n" in value or "\r" in value:
            raise HTTPException(400, f"{label}: too long")
    return [value] if opt.get("list") else value


def normalise(overrides: dict | None) -> dict:
    if not overrides:
        return {}
    if not isinstance(overrides, dict):
        raise HTTPException(400, "overrides must map settings to values")
    return {str(k): normalise_value(str(k), v) for k, v in overrides.items()}


def carry_over(values: dict) -> dict:
    """The settings of another profile (a project's own) that can be passed
    on: those in the catalog with values the slicer accepts."""
    out = {}
    for key, value in values.items():
        if key in catalog():
            try:
                out[key] = normalise_value(key, value)
            except HTTPException:
                pass  # e.g. -1 for "auto" in a Bambu Studio project
    return out


class ProcessPresets:
    """process-presets.json: {name: {printer, process, machine, overrides, saved}}.

    Shared by all printers: a preset holds only the settings it changes, and
    they go on top of whichever printer's process preset (or project) is
    picked. ``printer`` / ``process`` say where it was made."""

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

    def list(self) -> list[dict]:
        return [{"name": n, **v} for n, v in sorted(self.read().items(), key=lambda kv: kv[0].lower())]

    def save(self, name: str, printer: str, process: str, machine: str, overrides: dict) -> dict:
        name = name.strip()
        if not NAME.match(name):
            raise HTTPException(400, "Give the preset a name (up to 60 characters)")
        overrides = normalise(overrides)
        if not overrides:
            raise HTTPException(400, "Change at least one setting before saving")
        with self.lock:
            data = self.read()
            if name not in data and len(data) >= MAX_PRESETS:
                raise HTTPException(400, f"At most {MAX_PRESETS} presets")
            data[name] = {"printer": printer, "process": process, "machine": machine,
                          "overrides": overrides, "saved": time.time()}
            self._write(data)
        return {"name": name, **data[name]}

    def delete(self, name: str) -> None:
        with self.lock:
            data = self.read()
            if data.pop(name, None) is None:
                raise HTTPException(404, "No such preset")
            self._write(data)
