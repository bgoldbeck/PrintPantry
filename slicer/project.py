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
        bed_key = BED_TEMP_KEYS.get(info.bed_type, "")
        temp_lists = {"nozzle": _list(settings.get("nozzle_temperature")),
                      "nozzle_first": _list(settings.get("nozzle_temperature_initial_layer")),
                      "bed": _list(settings.get(bed_key)) if bed_key else [],
                      "bed_first": _list(settings.get(bed_key + "_initial_layer")) if bed_key else []}
        # The slicer sizes everything from filament_colour, so that is the count.
        count = len(colours) or len(presets) or len(types)
        for i in range(count):
            info.filaments.append({
                "type": types[i] if i < len(types) else "",
                "colour": _norm_colour(colours[i]) if i < len(colours) else "",
                "preset": presets[i] if i < len(presets) else "",
                "temps": _temps_at(temp_lists, i),
            })
        if MODEL_SETTINGS in names:
            try:
                root = ET.fromstring(zf.read(MODEL_SETTINGS))
                info.plates = max(1, len(root.findall("plate")))
                # A plain 3MF (no project settings, e.g. a multi-colour FreeCAD
                # render) still says which filament each part uses.
                if not info.filaments:
                    used = [int(m.get("value")) for m in root.iter("metadata")
                            if m.get("key") == "extruder" and str(m.get("value", "")).isdigit()]
                    for _ in range(max(used, default=0)):
                        info.filaments.append({"type": "", "colour": "", "preset": ""})
            except ET.ParseError:
                pass
    return info


# Plate type -> the filament setting with its bed temperature (as slicer/service.py BED_TYPES).
BED_TEMP_KEYS = {"Textured PEI Plate": "textured_plate_temp", "High Temp Plate": "hot_plate_temp",
                 "Cool Plate": "cool_plate_temp", "Engineering Plate": "eng_plate_temp",
                 "Textured Cool Plate": "textured_cool_plate_temp", "Supertack Plate": "supertack_plate_temp"}


def _int(value) -> int | None:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _temps_at(lists: dict, i: int) -> dict:
    """Filament i's temperatures; a missing first layer one is the same as the rest."""
    out = {k: _int(v[i]) for k, v in lists.items() if i < len(v) and _int(v[i]) is not None}
    for first, main in (("nozzle_first", "nozzle"), ("bed_first", "bed")):
        if first not in out and main in out:
            out[first] = out[main]
    return out


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


# Preset bookkeeping keys that must not replace the project's own.
META_KEYS = {"name", "type", "from", "inherits", "instantiation", "setting_id", "version",
             "compatible_printers", "compatible_printers_condition", "print_settings_id",
             "nozzle_diameter", "curr_bed_type"}


def apply_process(config: dict, process: dict) -> None:
    """Put a process preset's values into project settings.

    Also clears the project's list of process keys changed from the system
    preset, so the CLI does not keep the project's old values for them.
    """
    for key, value in process.items():
        if key not in META_KEYS:
            config[key] = value
    diff = config.get("different_settings_to_system")
    if isinstance(diff, list) and diff:
        diff[0] = ""


def apply_overrides(config: dict, overrides: dict) -> None:
    """Put single settings (the Slice dialog's process settings) into a
    process preset or project settings, keeping each value's shape: a list
    setting stays a list of the same length."""
    for key, value in overrides.items():
        one = value[0] if isinstance(value, list) and value else value
        old = config.get(key)
        if isinstance(old, list):
            config[key] = [one] * max(1, len(old))
        else:
            config[key] = value
    diff = config.get("different_settings_to_system")
    if overrides and isinstance(diff, list) and diff:
        diff[0] = ""


def set_filament_values(config: dict, values: list[dict]) -> bool:
    """Set per-filament settings (e.g. nozzle_temperature) in project
    settings: ``values[i]`` holds filament i+1's. Lists keep their length
    (the filament count); a missing one is made from the filament count."""
    count = len(_list(config.get("filament_colour"))) or len(_list(config.get("filament_settings_id"))) or 1
    changed = False
    for i, row in enumerate(values):
        for key, v in (row or {}).items():
            old = config.get(key)
            items = list(old) if isinstance(old, list) else ([str(old)] if old not in (None, "") else [])
            if not items:
                items = [str(v)] * count
            while len(items) <= i:
                items.append(items[-1])
            items[i] = str(v)
            config[key] = items
            changed = True
    if changed:
        # Filaments whose values were set are no longer "as the system preset".
        diff = config.get("different_settings_to_system")
        if isinstance(diff, list):
            for i, row in enumerate(values):
                if row and i + 1 < len(diff):
                    have = [k for k in str(diff[i + 1]).split(";") if k]
                    diff[i + 1] = ";".join(have + [k for k in row if k not in have])
    return changed


def read_settings(path: Path) -> dict:
    """A project 3MF's own settings ({} when it has none)."""
    try:
        with zipfile.ZipFile(path) as zf:
            data = json.loads(zf.read(SETTINGS)) if SETTINGS in zf.namelist() else {}
    except (OSError, ValueError, zipfile.BadZipFile, KeyError):
        return {}
    return data if isinstance(data, dict) else {}


def pad_filaments(config: dict, count: int, filament_keys, nozzles: int = 1) -> bool:
    """Grow the project's per-filament lists to ``count`` filaments.

    When objects are moved to a toolhead the project never used, more
    filaments are loaded than the project has; OrcaSlicer then aborts with
    "set_with_restore_2(): invalid size found" unless every per-filament list
    (and the flush matrix) has an entry for each. Returns True if changed."""
    have = len(_list(config.get("filament_colour"))) or len(_list(config.get("filament_settings_id")))
    if not have or count <= have:
        return False
    keys = set(filament_keys) | {k for k in config if k.startswith("filament_")}
    keys -= {"compatible_prints", "compatible_printers", "filament_map"}
    for key in keys:
        value = config.get(key)
        if isinstance(value, list) and len(value) == have and value:
            config[key] = value + [value[-1]] * (count - have)
    # Which nozzle each filament prints with: on a toolchanger (U1: 4) filament
    # N goes to toolhead N, on a single nozzle all go to 1.
    fmap = config.get("filament_map")
    if isinstance(fmap, list) and len(fmap) == have:
        config["filament_map"] = fmap + [str(min(i + 1, max(1, nozzles))) for i in range(have, count)]
    matrix = config.get("flush_volumes_matrix")
    if isinstance(matrix, list):
        old = [float(v) if str(v).replace(".", "", 1).isdigit() else 0.0 for v in matrix]
        nozzles = max(1, len(old) // (have * have)) if have and len(old) % (have * have) == 0 else 1
        default = max(old) if old and max(old) > 0 else 280.0
        out = []
        for n in range(nozzles):
            block = old[n * have * have:(n + 1) * have * have]
            for i in range(count):
                for j in range(count):
                    if i == j:
                        out.append("0")
                    elif i < have and j < have and block:
                        out.append(f"{block[i * have + j]:g}")
                    else:
                        out.append(f"{default:g}")
        config["flush_volumes_matrix"] = out
    return True


SPARE_COLOURS = ["#FFFFFF", "#F5D000", "#D02020", "#2060D0", "#20A040", "#202020", "#E07020", "#9040C0"]


def distinct_hex(colours: list[str]) -> list[str]:
    """Every filament its own colour, kept as close to the chosen one as can be.

    OrcaSlicer treats filaments of the same colour and type as one (objects
    put on the second print with the first). A repeat is nudged by a step or
    two in each channel, so it still looks like the toolhead's spool in every
    preview and picture; a missing colour gets a spare one."""
    seen: set[str] = set()
    out = []
    for c in colours:
        c = str(c or "").upper()[:7]
        if not re.fullmatch(r"#[0-9A-F]{6}", c):
            c = next((x for x in SPARE_COLOURS if x not in seen), "#808080")
        if c in seen:
            n = int(c[1:], 16)
            rgb = [(n >> 16) & 255, (n >> 8) & 255, n & 255]
            for step in range(1, 64):
                d = -step if sum(rgb) > 382 else step
                cand = "#" + "".join(f"{min(255, max(0, v + d)):02X}" for v in rgb)
                if cand not in seen:
                    c = cand
                    break
        seen.add(c)
        out.append(c)
    return out


def copy_for_slicing(src: Path, dst: Path, process: dict | None = None,
                     filaments: int = 0, filament_keys=(), nozzles: int = 1,
                     colours: list[str] | None = None, overrides: dict | None = None,
                     temps: list[dict] | None = None) -> None:
    """Copy a 3MF, padding its per-extruder settings (see pad_per_extruder)
    and per-filament lists (see pad_filaments), and, when given, writing a
    process preset, single setting overrides and per-filament temperatures
    ({setting: °C} per filament, see set_filament_values) into its settings."""
    with zipfile.ZipFile(src) as zin:
        try:
            settings = json.loads(zin.read(SETTINGS)) if SETTINGS in zin.namelist() else None
        except ValueError:
            settings = None
        if isinstance(settings, dict):
            changed = pad_per_extruder(settings)
            if filaments and pad_filaments(settings, filaments, filament_keys, nozzles):
                changed = True
            # Colours of the loaded spools (per filament; "" keeps the
            # project's), so the sliced file shows what will really print.
            have = settings.get("filament_colour")
            if colours and isinstance(have, list) and any(colours):
                cols = [str(c) for c in have]
                for i, c in enumerate(colours[:len(cols)]):
                    if c:
                        cols[i] = c
                cols = distinct_hex(cols) if len(cols) > 1 else cols
                if cols != have:
                    settings["filament_colour"] = cols
                    changed = True
            if process:
                apply_process(settings, process)
                changed = True
            if overrides:
                apply_overrides(settings, overrides)
                changed = True
            if temps and set_filament_values(settings, temps):
                changed = True
            if changed:
                with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
                    for item in zin.infolist():
                        data = zin.read(item)
                        if item.filename == SETTINGS:
                            data = json.dumps(settings, indent=4).encode()
                        zout.writestr(item, data)
                return
    dst.write_bytes(src.read_bytes())


def drop_keys(path: Path, keys: list[str]) -> list[str]:
    """Remove settings from a 3MF's project settings, in place. Returns those removed."""
    with zipfile.ZipFile(path) as zin:
        items = [(i, zin.read(i)) for i in zin.infolist()]
    removed: list[str] = []
    out = []
    for item, data in items:
        if item.filename == SETTINGS:
            try:
                settings = json.loads(data)
            except ValueError:
                return []
            removed = [k for k in keys if k in settings]
            for k in removed:
                del settings[k]
            data = json.dumps(settings, indent=4).encode()
        out.append((item, data))
    if removed:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zout:
            for item, data in out:
                zout.writestr(item, data)
    return removed
