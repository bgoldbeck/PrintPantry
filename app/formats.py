"""Readers for the file types Print Pantry understands.

* Sliced Bambu files (``*.gcode.3mf``): a zip with ``Metadata/plate_N.gcode`` per
  plate, ``Metadata/plate_N.png`` thumbnails, ``Metadata/slice_info.config``
  (time, weight, filaments per plate) and ``Metadata/project_settings.config``
  (printer model).
* Project 3MF (``*.3mf`` without gcode inside): the editable source. We only read
  its plate thumbnails and object names.
* Plain G-code (``*.gcode``), e.g. from Snapmaker Orca for the U1: we read the
  header comments, the thumbnail block and the config block at the end.

Everything here is pure and side-effect free so it can be unit-tested and cached.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

# Bambu printer_model_id codes seen in slice_info.config.
BAMBU_MODEL_IDS = {
    "C11": "Bambu Lab P1P",
    "C12": "Bambu Lab P1S",
    "C13": "Bambu Lab X1E",
    "BL-P001": "Bambu Lab X1 Carbon",
    "BL-P002": "Bambu Lab X1",
    "N1": "Bambu Lab A1 mini",
    "N2S": "Bambu Lab A1",
    "O1D": "Bambu Lab H2D",
}

GCODE_HEAD_BYTES = 2 * 1024 * 1024  # thumbnails live near the top
GCODE_TAIL_BYTES = 256 * 1024  # Orca writes its config block at the end


@dataclass
class Filament:
    slot: int
    type: str = ""
    color: str = ""
    grams: float | None = None
    meters: float | None = None


@dataclass
class Plate:
    index: int
    seconds: int | None = None
    grams: float | None = None
    filaments: list[Filament] = field(default_factory=list)
    objects: list[str] = field(default_factory=list)
    has_thumbnail: bool = False


@dataclass
class FileInfo:
    kind: str  # "stl" | "project3mf" | "sliced3mf" | "gcode" | "image" | "other"
    printer_model: str = ""  # e.g. "Bambu Lab P1S", "Snapmaker U1"
    slicer: str = ""
    plates: list[Plate] = field(default_factory=list)
    objects: list[str] = field(default_factory=list)  # object / source names inside
    error: str = ""

    def to_json(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_json(data: dict) -> "FileInfo":
        plates = []
        for p in data.get("plates", []):
            fils = [Filament(**f) for f in p.get("filaments", [])]
            plates.append(Plate(**{**p, "filaments": fils}))
        return FileInfo(**{**data, "plates": plates})


MODEL_EXTS = {".stl"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def classify_name(name: str) -> str:
    """Cheap classification by file name only."""
    low = name.lower()
    if low.endswith(".gcode.3mf"):
        return "sliced3mf"
    if low.endswith(".3mf"):
        return "3mf"  # project or sliced; needs a look inside
    if low.endswith((".gcode", ".bgcode")):
        return "gcode"
    suffix = Path(low).suffix
    if suffix in MODEL_EXTS:
        return "stl"
    if suffix in IMAGE_EXTS:
        return "image"
    return "other"


# ---------------------------------------------------------------- 3MF


def _num(value: str | None, cast=float):
    if value is None or value == "":
        return None
    try:
        return cast(float(value)) if cast is int else cast(value)
    except (TypeError, ValueError):
        return None


def read_3mf(path: Path) -> FileInfo:
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            gcode_plates = sorted(
                int(m.group(1))
                for n in names
                if (m := re.fullmatch(r"Metadata/plate_(\d+)\.gcode", n))
            )
            thumb_plates = {
                int(m.group(1))
                for n in names
                if (m := re.fullmatch(r"Metadata/plate_(\d+)\.png", n))
            }
            info = FileInfo(kind="sliced3mf" if gcode_plates else "project3mf")

            settings = {}
            if "Metadata/project_settings.config" in names:
                try:
                    settings = json.loads(zf.read("Metadata/project_settings.config"))
                except ValueError:
                    settings = {}
            info.printer_model = str(settings.get("printer_model") or "")

            slice_plates: dict[int, Plate] = {}
            if "Metadata/slice_info.config" in names:
                slice_plates, model_id, slicer = _parse_slice_info(
                    zf.read("Metadata/slice_info.config")
                )
                if not info.printer_model and model_id:
                    info.printer_model = BAMBU_MODEL_IDS.get(model_id, model_id)
                info.slicer = slicer

            app_name = _3mf_application(zf, names)
            if app_name:
                info.slicer = app_name.replace("-", " ", 1)

            if info.kind == "sliced3mf":
                for idx in gcode_plates:
                    plate = slice_plates.get(idx, Plate(index=idx))
                    plate.has_thumbnail = idx in thumb_plates
                    info.plates.append(plate)
            else:
                for idx in sorted(thumb_plates):
                    info.plates.append(Plate(index=idx, has_thumbnail=True))

            objects: list[str] = []
            for p in info.plates:
                objects.extend(p.objects)
            if "Metadata/model_settings.config" in names:
                objects.extend(_model_settings_names(zf.read("Metadata/model_settings.config")))
            info.objects = list(dict.fromkeys(o for o in objects if o))
            return info
    except (zipfile.BadZipFile, OSError, KeyError) as exc:
        return FileInfo(kind="other", error=f"unreadable 3MF: {exc}")


def _parse_slice_info(raw: bytes) -> tuple[dict[int, Plate], str, str]:
    plates: dict[int, Plate] = {}
    model_id = ""
    slicer = ""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return plates, model_id, slicer
    header = root.find("header")
    if header is not None:
        for item in header.findall("header_item"):
            if item.get("key") == "X-BBL-Client-Version":
                slicer = f"Bambu Studio {item.get('value', '')}".strip()
    for pel in root.findall("plate"):
        meta = {m.get("key"): m.get("value") for m in pel.findall("metadata")}
        idx = _num(meta.get("index"), int)
        if idx is None:
            continue
        model_id = model_id or (meta.get("printer_model_id") or "")
        plate = Plate(
            index=idx,
            seconds=_num(meta.get("prediction"), int),
            grams=_num(meta.get("weight")),
        )
        for obj in pel.findall("object"):
            if obj.get("skipped") == "true":
                continue
            if obj.get("name"):
                plate.objects.append(obj.get("name"))
        for fel in pel.findall("filament"):
            plate.filaments.append(
                Filament(
                    slot=_num(fel.get("id"), int) or 0,
                    type=fel.get("type", ""),
                    color=_norm_color(fel.get("color", "")),
                    grams=_num(fel.get("used_g")),
                    meters=_num(fel.get("used_m")),
                )
            )
        plates[idx] = plate
    return plates, model_id, slicer


def _model_settings_names(raw: bytes) -> list[str]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    names = []
    for obj in root.findall("object"):
        for meta in obj.findall("metadata"):
            if meta.get("key") == "name" and meta.get("value"):
                names.append(meta.get("value"))
        for part in obj.findall("part"):
            for meta in part.findall("metadata"):
                if meta.get("key") == "source_file" and meta.get("value"):
                    names.append(Path(meta.get("value").replace("\\", "/")).name)
    return names


def _3mf_application(zf: zipfile.ZipFile, names: set[str]) -> str:
    if "3D/3dmodel.model" not in names:
        return ""
    with zf.open("3D/3dmodel.model") as fh:
        head = fh.read(4096).decode("utf-8", "replace")
    m = re.search(r'<metadata name="Application">([^<]*)</metadata>', head)
    return m.group(1).strip() if m else ""


def read_3mf_thumbnail(path: Path, plate: int) -> bytes | None:
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            for cand in (
                f"Metadata/plate_{plate}.png",
                f"Metadata/top_{plate}.png",
                "Metadata/thumbnail.png",
                "Auxiliaries/.thumbnails/thumbnail_middle.png",
                "Auxiliaries/.thumbnails/thumbnail_3mf.png",
            ):
                if cand in names:
                    return zf.read(cand)
    except (zipfile.BadZipFile, OSError):
        return None
    return None


# ---------------------------------------------------------------- G-code

_THUMB_BEGIN = re.compile(
    r"^;\s*(thumbnail(?:_PNG|_JPG|_QOI)?) begin (\d+)x(\d+) (\d+)\s*$", re.M
)


def _read_head_tail(path: Path) -> tuple[str, str]:
    size = path.stat().st_size
    with path.open("rb") as fh:
        head = fh.read(GCODE_HEAD_BYTES)
        tail = b""
        if size > GCODE_HEAD_BYTES:
            fh.seek(max(GCODE_HEAD_BYTES, size - GCODE_TAIL_BYTES))
            tail = fh.read()
    return head.decode("utf-8", "replace"), tail.decode("utf-8", "replace")


def _gcode_settings(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in re.finditer(r"^;\s*([A-Za-z0-9_ \[\]()]+?)\s*[=:]\s*(.*?)\s*$", text, re.M):
        key = m.group(1).strip()
        if key not in out:
            out[key] = m.group(2)
    return out


def _parse_duration(text: str) -> int | None:
    text = text.strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return int(float(text))
    total = 0
    found = False
    for value, unit in re.findall(r"(\d+)\s*([dhms])", text):
        found = True
        total += int(value) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
    return total if found else None


def _split_list(value: str) -> list[str]:
    return [v.strip().strip('"') for v in re.split(r"[;,]", value) if v.strip()]


def _floats(value: str) -> list[float | None]:
    return [_num(v) for v in _split_list(value)]


def _norm_color(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"#[0-9a-fA-F]{8}", value):
        value = value[:7]  # drop alpha (Bambu writes #RRGGBBAA)
    return value.upper() if re.fullmatch(r"#[0-9a-fA-F]{6}", value) else value


def read_gcode(path: Path) -> FileInfo:
    try:
        head, tail = _read_head_tail(path)
    except OSError as exc:
        return FileInfo(kind="other", error=f"unreadable G-code: {exc}")
    info = FileInfo(kind="gcode")
    settings = _gcode_settings(tail)
    settings.update({k: v for k, v in _gcode_settings(head).items() if k not in settings})

    m = re.search(r"^;\s*generated by (.+?)(?: on .*)?$", head, re.M | re.I)
    if m:
        info.slicer = m.group(1).strip()
    info.printer_model = (
        settings.get("printer_model")
        or settings.get("printer_settings_id")
        or settings.get("machine_name")
        or ""
    ).strip('"')

    seconds = None
    for key in (
        "estimated printing time (normal mode)",
        "estimated printing time",
        "total estimated time",
        "TIME",
    ):
        if key in settings:
            seconds = _parse_duration(settings[key])
            if seconds is not None:
                break
    if seconds is None:
        m = re.search(r";\s*model printing time:\s*([^;]+?);", head)
        if m:
            seconds = _parse_duration(m.group(1))

    grams = _floats(settings.get("filament used [g]", "") or settings.get("total filament weight [g]", ""))
    meters_mm = _floats(settings.get("filament used [mm]", ""))
    types = _split_list(settings.get("filament_type", ""))
    colors = _split_list(settings.get("filament_colour", "") or settings.get("filament_color", ""))
    count = max(len(grams), len(types), len(colors), len(meters_mm))
    filaments = []
    for i in range(count):
        g = grams[i] if i < len(grams) else None
        mm = meters_mm[i] if i < len(meters_mm) else None
        if count > 1 and not g and not mm:
            continue  # unused extruder slot
        filaments.append(
            Filament(
                slot=i + 1,
                type=types[i] if i < len(types) else "",
                color=_norm_color(colors[i]) if i < len(colors) else "",
                grams=g,
                meters=(mm / 1000) if mm else None,
            )
        )
    total_g = settings.get("total filament used [g]") or settings.get("total filament weight [g]")
    plate = Plate(
        index=1,
        seconds=seconds,
        grams=_num(total_g) if total_g else (sum(f.grams or 0 for f in filaments) or None),
        filaments=filaments,
        has_thumbnail=bool(_THUMB_BEGIN.search(head)),
    )
    # "; printing object Pot Ladle Clip.stl id:0 copy 0" keeps spaces, so prefer
    # it; Klipper's "EXCLUDE_OBJECT_DEFINE NAME=Pot_Ladle_Clip.stl_id_0_copy_0" doesn't.
    objs = [re.sub(r"\s+id:\d+\s+copy\s+\d+$", "", o.strip())
            for o in re.findall(r"^; printing object (.+?)\s*$", head, re.M)]
    if not objs:
        objs = re.findall(r"^EXCLUDE_OBJECT_DEFINE NAME=(\S+)", head, re.M)
    objs = [re.sub(r"_id_\d+_copy_\d+$", "", o) for o in objs]
    plate.objects = list(dict.fromkeys(objs))
    info.objects = plate.objects
    info.plates = [plate]
    return info


def read_gcode_thumbnail(path: Path) -> tuple[bytes, str] | None:
    """Return the largest embedded thumbnail as (bytes, mime)."""
    try:
        head, _ = _read_head_tail(path)
    except OSError:
        return None
    best = None
    for m in _THUMB_BEGIN.finditer(head):
        kind, w, h = m.group(1), int(m.group(2)), int(m.group(3))
        end = head.find(f"; {kind} end", m.end())
        if end < 0:
            continue
        body = "".join(
            line.lstrip(";").strip() for line in head[m.end():end].splitlines()
        )
        if best is None or w * h > best[0]:
            best = (w * h, kind, body)
    if not best:
        return None
    _, kind, body = best
    if kind == "thumbnail_QOI":
        return None  # browsers can't show QOI
    try:
        data = base64.b64decode(body)
    except (binascii.Error, ValueError):
        return None
    return data, "image/jpeg" if kind == "thumbnail_JPG" else "image/png"


# ---------------------------------------------------------------- dispatch


def read_info(path: Path) -> FileInfo:
    kind = classify_name(path.name)
    if kind in ("sliced3mf", "3mf"):
        return read_3mf(path)
    if kind == "gcode":
        if path.name.lower().endswith(".bgcode"):
            return FileInfo(kind="gcode", error="binary G-code is not parsed")
        return read_gcode(path)
    return FileInfo(kind=kind)
