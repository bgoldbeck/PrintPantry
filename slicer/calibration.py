"""Calibration prints built from OrcaSlicer's own test models.

Flow ratio: OrcaSlicer ships the test plates it uses for its "Flow rate"
calibration (resources/calib/filament_flow). Each block is named after its
offset (``flowrate_m5`` = -5 %, ``flowrate_0.01`` = +0.01) and has that offset
embossed on top. We copy the plate into a project 3MF and give every block its
own ``print_flow_ratio``, plus the per-object settings OrcaSlicer's
calibration uses (monotonic top surface, 5 top layers, ...), the same way the
desktop slicer's Calibration menu does. The slicer multiplies the filament's
``filament_flow_ratio`` by an object's ``print_flow_ratio``, so the block that
prints best gives the new filament flow ratio directly:

    new filament_flow_ratio = old filament_flow_ratio * block ratio

Pure Python (zipfile), unit-tested without the slicer.
"""

from __future__ import annotations

import math
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

MODEL = "3D/3dmodel.model"
MODEL_SETTINGS = "Metadata/model_settings.config"
NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"


@dataclass(frozen=True)
class FlowTest:
    id: str
    label: str
    file: str  # in <profiles>/../calib/filament_flow
    linear: bool  # block names are ratio offsets (0.01) rather than percent (5)
    hint: str


FLOW_TESTS = {
    "yolo": FlowTest("yolo", "Standard (±5 %, 1 % steps)", "Orca-LinearFlow.3mf", True,
                     "11 blocks from 0.95 to 1.05 times the current flow. Start here."),
    "yolo_fine": FlowTest("yolo_fine", "Fine (-4 % to +3.5 %, 0.5 % steps)", "Orca-LinearFlow_fine.3mf", True,
                          "16 blocks in half-percent steps, to fine-tune after the standard test."),
    "coarse": FlowTest("coarse", "Coarse (±20 %, 5 % steps)", "flowrate-test-pass1.3mf", False,
                       "9 blocks from 0.80 to 1.20, for a filament that is far off."),
}

# Per-object settings OrcaSlicer's flow calibration sets (Plater::calib_flowrate).
OBJECT_SETTINGS = {
    "wall_loops": "3",
    "only_one_wall_top": "1",
    "thick_internal_bridges": "0",
    "sparse_infill_density": "35%",
    "min_width_top_surface": "100%",
    "bottom_shell_layers": "1",
    "top_shell_layers": "5",
    "top_shell_thickness": "0",
    "bottom_shell_thickness": "0",
    "detect_thin_wall": "1",
    "filter_out_gap_fill": "0",
    "sparse_infill_pattern": "zig-zag",
    "top_surface_pattern": "monotonic",
    "top_solid_infill_flow_ratio": "1",
    "infill_direction": "45",
    "solid_infill_direction": "135",
    "ironing_type": "no ironing",
    "seam_slope_type": "none",
    "gap_fill_target": "nowhere",
}


class CalibrationError(Exception):
    pass


def block_ratio(name: str, linear: bool) -> float | None:
    """``flowrate_m5`` -> 0.95 (percent), ``flowrate_m0.01`` -> 0.99 (linear)."""
    m = re.fullmatch(r"flowrate_(m?)([\d.]+)", name or "")
    if not m:
        return None
    value = float(m.group(2)) * (-1 if m.group(1) else 1)
    return round(1 + value if linear else (100 + value) / 100, 4)


def mm3_per_mm(width: float, height: float) -> float:
    """Cross-section of an extrusion (OrcaSlicer's rounded rectangle)."""
    return height * (width - height * (1 - math.pi / 4))


def speeds(nozzle: float, layer: float, max_volumetric: float, process: dict, coarse: bool) -> dict:
    """Keep solid infill and the top surface within the filament's volumetric limit."""
    flow = mm3_per_mm(nozzle * 1.2, layer) * (1.2 if coarse else 1)
    out = {}
    for key in ("internal_solid_infill_speed", "top_surface_speed"):
        try:
            current = float(_first(process.get(key)) or 0)
        except (TypeError, ValueError):
            current = 0
        limit = max_volumetric / flow if max_volumetric > 0 else 0
        values = [v for v in (current, limit) if v > 0]
        if values:
            out[key] = str(math.floor(min(values)))
    return out


def _first(v):
    return v[0] if isinstance(v, list) and v else v


def process_overrides(process: dict, nozzle: float) -> dict:
    """Process changes for the whole plate (like OrcaSlicer's calibration)."""
    layer = round(nozzle / 2, 3)
    try:
        first = float(_first(process.get("initial_layer_print_height")) or layer)
    except (TypeError, ValueError):
        first = layer
    return {
        "layer_height": str(layer),
        "initial_layer_print_height": str(max(first, layer)),
        "alternate_extra_wall": "0",
        "reduce_crossing_wall": "1",
        "print_flow_ratio": "1",
        "enable_prime_tower": "0",
    }


def bed_center(machine: dict) -> tuple[float, float]:
    """Middle of the machine's printable_area ("0x0,270x0,270x270,0x270")."""
    pts = []
    for p in machine.get("printable_area") or []:
        try:
            x, y = (float(v) for v in str(p).split("x"))
            pts.append((x, y))
        except ValueError:
            continue
    if not pts:
        return (128.0, 128.0)
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)


def build_flow_project(src: Path, dst: Path, test: FlowTest, extruder: int, extra: dict | None = None,
                       center: tuple[float, float] | None = None) -> list[dict]:
    """Copy a test plate into ``dst`` with per-block flow ratios. Returns the blocks.

    With ``center``, the plate keeps OrcaSlicer's layout and is moved so its
    middle sits there (the slicer's own arrange packs the blocks too tightly
    for its collision check).
    """
    try:
        zin = zipfile.ZipFile(src)
    except (OSError, zipfile.BadZipFile) as exc:
        raise CalibrationError(f"Couldn't open the test model {src.name}: {exc}") from exc
    with zin:
        root = ET.fromstring(zin.read(MODEL))
        blocks = []
        for obj in root.iter(f"{NS}object"):
            ratio = block_ratio(obj.get("name", ""), test.linear)
            if ratio is None:
                continue
            blocks.append({"id": obj.get("id"), "name": obj.get("name"), "ratio": ratio})
        if not blocks:
            raise CalibrationError(f"{src.name} has no flow test blocks")
        model = None
        if center:
            xs, ys = [], []
            for v in root.iter(f"{NS}vertex"):
                xs.append(float(v.get("x")))
                ys.append(float(v.get("y")))
            dx = center[0] - (min(xs) + max(xs)) / 2
            dy = center[1] - (min(ys) + max(ys)) / 2
            for item in root.iter(f"{NS}item"):
                item.set("transform", f"1 0 0 0 1 0 0 0 1 {dx:.4f} {dy:.4f} 0")
            ET.register_namespace("", NS[1:-1])
            for prefix, uri in (("m", "http://schemas.microsoft.com/3dmanufacturing/material/2015/02"),
                                ("p", "http://schemas.microsoft.com/3dmanufacturing/production/2015/06")):
                ET.register_namespace(prefix, uri)
            model = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")
        settings = ET.Element("config")
        for b in blocks:
            obj = ET.SubElement(settings, "object", id=str(b["id"]))
            values = {"name": b["name"], "extruder": str(extruder), **OBJECT_SETTINGS, **(extra or {}),
                      "print_flow_ratio": f"{b['ratio']:g}"}
            for k, v in values.items():
                ET.SubElement(obj, "metadata", key=k, value=v)
        data = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(settings, encoding="utf-8")
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if item.filename == MODEL and model is not None:
                    zout.writestr(item, model)
                elif item.filename != MODEL_SETTINGS:
                    zout.writestr(item, zin.read(item))
            zout.writestr(MODEL_SETTINGS, data)
    blocks.sort(key=lambda b: b["ratio"])
    return blocks


def retarget(src: Path, dst: Path, extruder: int, filament_keys) -> None:
    """Move every object of a project (exported by the slicer) onto ``extruder``.

    The slicer's export only keeps one value for most per-filament settings when
    a single filament is used, and the command line crashes (or refuses) when an
    object then uses filament 2+. So every one-value per-filament list is
    repeated to the number of filaments the project lists.
    """
    import json

    with zipfile.ZipFile(src) as zin:
        items = [(i, zin.read(i)) for i in zin.infolist()]
    keys = set(filament_keys)
    out = []
    for item, data in items:
        if item.filename == MODEL_SETTINGS:
            data = re.sub(rb'(<metadata key="extruder" value=")\d+(")', rb"\g<1>%d\g<2>" % extruder, data)
        elif item.filename == "Metadata/project_settings.config":
            cfg = json.loads(data)
            count = len(cfg.get("filament_settings_id") or [])
            if count < extruder:
                raise CalibrationError(f"The slicer kept {count} filaments; T{extruder} needs {extruder}")
            for k, v in cfg.items():
                if isinstance(v, list) and len(v) == 1 and (k in keys or k.startswith("filament_")):
                    cfg[k] = v * count
            data = json.dumps(cfg, indent=1).encode()
        out.append((item, data))
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item, data in out:
            zout.writestr(item, data)
