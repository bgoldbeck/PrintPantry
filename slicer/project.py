"""Look inside a project 3MF and prepare a copy for slicing.

Pure Python (zipfile + json), unit-tested without the slicer.
"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

SETTINGS = "Metadata/project_settings.config"
MODEL_SETTINGS = "Metadata/model_settings.config"


@dataclass
class ProjectInfo:
    kind: str  # "3mf" or "stl"
    printer_model: str = ""
    printer_preset: str = ""
    process_preset: str = ""
    bed_type: str = ""
    filaments: list[dict] = field(default_factory=list)  # {type, colour, preset}
    plates: int = 1
    sliced: bool = False

    def to_json(self) -> dict:
        return {
            "kind": self.kind,
            "printer_model": self.printer_model,
            "printer_preset": self.printer_preset,
            "process_preset": self.process_preset,
            "bed_type": self.bed_type,
            "filaments": self.filaments,
            "plates": self.plates,
            "sliced": self.sliced,
        }


def _first(value) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


def _list(value) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if value in (None, ""):
        return []
    return [v for v in re.split(r"[;,]", str(value))]


def inspect(path: Path) -> ProjectInfo:
    if path.suffix.lower() == ".stl":
        return ProjectInfo(kind="stl")
    info = ProjectInfo(kind="3mf")
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        info.sliced = any(re.fullmatch(r"Metadata/plate_\d+\.gcode", n) for n in names)
        settings: dict = {}
        if SETTINGS in names:
            try:
                settings = json.loads(zf.read(SETTINGS))
            except ValueError:
                settings = {}
        info.printer_model = _first(settings.get("printer_model"))
        info.printer_preset = _first(settings.get("printer_settings_id"))
        info.process_preset = _first(settings.get("print_settings_id"))
        info.bed_type = _first(settings.get("curr_bed_type"))
        colours = _list(settings.get("filament_colour"))
        types = _list(settings.get("filament_type"))
        presets = _list(settings.get("filament_settings_id"))
        # The slicer sizes everything from filament_colour, so that is the count.
        count = len(colours) or len(presets) or len(types)
        for i in range(count):
            info.filaments.append({
                "type": types[i] if i < len(types) else "",
                "colour": _norm_colour(colours[i]) if i < len(colours) else "",
                "preset": presets[i] if i < len(presets) else "",
            })
        if MODEL_SETTINGS in names:
            try:
                root = ET.fromstring(zf.read(MODEL_SETTINGS))
                info.plates = max(1, len(root.findall("plate")))
            except ET.ParseError:
                pass
    return info


def _norm_colour(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"#[0-9a-fA-F]{8}", value):
        value = value[:7]
    return value.upper()


# Per-nozzle settings that OrcaSlicer indexes by extruder number. When a
# multi-toolhead printer (the U1 has 4) has only one value, the command line
# reads past the end of the list and crashes now and then, so pad them.
PER_EXTRUDER_DEFAULTS = {"extruder_type": "Direct Drive", "nozzle_volume_type": "Standard"}


def pad_per_extruder(config: dict) -> bool:
    """Make per-extruder lists as long as nozzle_diameter. Returns True if changed."""
    nozzles = config.get("nozzle_diameter")
    count = len(nozzles) if isinstance(nozzles, list) else 0
    changed = False
    for key, default in PER_EXTRUDER_DEFAULTS.items():
        value = config.get(key)
        if not isinstance(value, list) or not value:
            value = [default]
        if count and len(value) < count:
            config[key] = value + [value[0]] * (count - len(value))
            changed = True
    return changed


def copy_for_slicing(src: Path, dst: Path) -> None:
    """Copy a 3MF, padding its per-extruder settings (see pad_per_extruder)."""
    with zipfile.ZipFile(src) as zin:
        try:
            settings = json.loads(zin.read(SETTINGS)) if SETTINGS in zin.namelist() else None
        except ValueError:
            settings = None
        if isinstance(settings, dict) and pad_per_extruder(settings):
            with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    data = zin.read(item)
                    if item.filename == SETTINGS:
                        data = json.dumps(settings, indent=4).encode()
                    zout.writestr(item, data)
            return
    dst.write_bytes(src.read_bytes())
