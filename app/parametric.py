"""Parametric models: OpenSCAD files and FreeCAD documents with a parameter sheet.

The app reads the parameters itself (no OpenSCAD or FreeCAD needed for that):

* OpenSCAD: Customizer comments, the same ones MakerWorld and Printables use.
      /* [Size] */                 a group (tab)
      // Width of the box          description (the comment line above)
      width = 40; // [10:1:200]    slider: min:step:max (or min:max)
      style = "round"; // [round, square]        drop-down
      size = 2; // [1:Small, 2:Medium, 3:Large]  drop-down with labels
      hollow = true;               check box
      /* [Hidden] */               nothing after this is shown
  Only plain values (numbers, strings, true/false, number lists) before the
  first module or function count, as in OpenSCAD's own Customizer.

* FreeCAD: every spreadsheet cell with an alias and a plain value (not a
  formula) is a parameter. The cell to its left is its label and the cell to
  its right its description, if they hold text. Parts to export are the
  visible solids that no other visible solid is built from.

Rendering happens in the slicer container (slicer/render.py), one job at a
time together with slicing. Saved parameter sets live next to the source:
OpenSCAD's own <name>.json format for .scad files (so the desktop Customizer
sees them too), and the same format in <name>.params.json for FreeCAD files.
Mounted under /api/parametric by app.main.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

log = logging.getLogger("printpantry.parametric")

KINDS = ("scad", "fcstd")
FONT_EXTS = (".ttf", ".otf", ".ttc")
MAX_SOURCE = 2 * 1024 * 1024  # .scad files bigger than this are not parsed


# ---------------------------------------------------------------- OpenSCAD

_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_ASSIGN = re.compile(r"^\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(.+?)\s*;\s*(?://\s*(.*))?$")
_GROUP = re.compile(r"^\s*/\*\s*\[\s*(.+?)\s*\]\s*\*/\s*$")
_DEF = re.compile(r"^\s*(module|function)\b")


def _strip_code(line: str, in_block: bool) -> tuple[str, bool, int]:
    """Brace depth change of one line, skipping strings and comments."""
    depth = 0
    i = 0
    in_str = False
    while i < len(line):
        c = line[i]
        if in_block:
            if line.startswith("*/", i):
                in_block = False
                i += 2
                continue
        elif in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
        elif line.startswith("//", i):
            break
        elif line.startswith("/*", i):
            in_block = True
            i += 2
            continue
        elif c == '"':
            in_str = True
        elif c in "{[(":
            depth += 1
        elif c in "}])":
            depth -= 1
        i += 1
    return line, in_block, depth


def _literal(text: str):
    """A plain OpenSCAD value, or raise ValueError for expressions."""
    t = text.strip()
    if re.fullmatch(_NUM, t):
        return float(t) if re.search(r"[.eE]", t) else int(t)
    if t in ("true", "false"):
        return t == "true"
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        return json.loads(t)  # OpenSCAD string escapes are a subset of JSON's
    if t.startswith("[") and t.endswith("]"):
        inner = t[1:-1].strip()
        if not inner:
            raise ValueError("empty vector")
        items = [i.strip() for i in inner.split(",")]
        if all(re.fullmatch(_NUM, i) for i in items):
            return [float(i) if re.search(r"[.eE]", i) else int(i) for i in items]
    raise ValueError("not a plain value")


def _option_value(raw: str, numeric: bool):
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        raw = raw[1:-1]
    if numeric and re.fullmatch(_NUM, raw):
        return float(raw) if re.search(r"[.eE]", raw) else int(raw)
    return raw


def _hint(param: dict, hint: str) -> None:
    """Apply a Customizer comment like [0:5:100] or [a, b:Label]."""
    m = re.fullmatch(r"\[(.*)\]", hint.strip())
    if not m:
        return
    body = m.group(1).strip()
    value = param["value"]
    numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
    parts = [p.strip() for p in body.split(":")]
    if (numeric or isinstance(value, list)) and "," not in body and all(re.fullmatch(_NUM, p) for p in parts if p):
        nums = [float(p) for p in parts]
        if len(nums) == 1:
            param.update(min=0, max=nums[0])
        elif len(nums) == 2:
            param.update(min=nums[0], max=nums[1])
        elif len(nums) == 3:
            param.update(min=nums[0], step=nums[1], max=nums[2])
        if isinstance(value, (int, float)):
            param["type"] = "slider"
        return
    if isinstance(value, (bool, list)):
        return
    options = []
    for item in [i for i in re.split(r",(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", body) if i.strip()]:
        if ":" in item and not item.strip().startswith('"') or re.fullmatch(r'\s*"[^"]*"\s*:\s*.+', item):
            val, label = item.split(":", 1)
        else:
            val, label = item, item
        options.append({"value": _option_value(val, numeric), "label": _option_value(label, False)})
    if options:
        param["type"] = "choice"
        param["options"] = options


def parse_scad(text: str) -> dict:
    params: list[dict] = []
    group = "Parameters"
    hidden = False
    comment = ""
    depth = 0
    in_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if depth == 0 and not in_block:
            g = _GROUP.match(line)
            if g:
                name = g.group(1)
                hidden = name.lower() == "hidden"
                group = "Parameters" if name.lower() == "global" else name
                comment = ""
                continue
            if _DEF.match(line):
                break
            if stripped.startswith("//"):
                comment = stripped.lstrip("/").strip()
                continue
            a = _ASSIGN.match(line)
            if a and not hidden:
                name, raw, hint = a.group(1), a.group(2), a.group(3) or ""
                try:
                    value = _literal(raw)
                except ValueError:
                    value = None
                if value is not None:
                    kind = ("bool" if isinstance(value, bool) else "vector" if isinstance(value, list)
                            else "string" if isinstance(value, str) else "number")
                    p = {"name": name, "label": name.replace("_", " "), "description": comment,
                         "group": group, "type": kind, "value": value}
                    if hint:
                        _hint(p, hint)
                    if isinstance(value, str) and value.lower().endswith(FONT_EXTS):
                        p["type"] = "font"
                    params.append(p)
            if not stripped.startswith("//"):
                comment = ""
        _, in_block, d = _strip_code(line, in_block)
        depth = max(0, depth + d)
    return {"params": params, "parts": []}


# ---------------------------------------------------------------- FreeCAD

def _col_row(address: str) -> tuple[str, int]:
    m = re.fullmatch(r"([A-Z]+)(\d+)", address)
    return (m.group(1), int(m.group(2))) if m else ("", 0)


def _col_shift(col: str, delta: int) -> str:
    n = 0
    for ch in col:
        n = n * 26 + ord(ch) - 64
    n += delta
    if n < 1:
        return ""
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def _cell_text(content: str | None) -> str:
    if not content or content.startswith("="):
        return ""
    return content.removeprefix("'")


_UNIT_NUM = re.compile(rf"^\s*({_NUM})\s*([A-Za-z°/^0-9]*)\s*$")
_BOOL_HINT = re.compile(r"\b[01]\s*=|\b(on|off|yes|no|true|false|enable|disable)\b", re.IGNORECASE)


def _sheet_params(obj: ET.Element, sheet_label: str) -> list[dict]:
    cells: dict[str, ET.Element] = {}
    for c in obj.iter("Cell"):
        cells[c.get("address", "")] = c
    params = []
    for addr, c in cells.items():
        alias = c.get("alias")
        content = c.get("content")
        if not alias or content is None or content.startswith("="):
            continue  # formulas are computed, not parameters
        col, row = _col_row(addr)
        left = cells.get(f"{_col_shift(col, -1)}{row}")
        right = cells.get(f"{_col_shift(col, 1)}{row}")
        label = _cell_text(left.get("content")) if left is not None and not left.get("alias") else ""
        note = _cell_text(right.get("content")) if right is not None and not right.get("alias") else ""
        p = {"name": alias, "label": label or alias, "description": note, "group": sheet_label,
             "sheet": obj.get("name"), "cell": addr}
        if content.startswith("'"):
            value = content[1:]
            p.update(type="font" if value.lower().endswith(FONT_EXTS) else "string", value=value)
        else:
            m = _UNIT_NUM.match(content)
            if not m:
                p.update(type="string", value=content)
            else:
                num = m.group(1)
                value = float(num) if re.search(r"[.eE]", num) else int(num)
                p.update(type="number", value=value)
                if m.group(2):
                    p["unit"] = m.group(2)
                if value in (0, 1) and isinstance(value, int) and _BOOL_HINT.search(note):
                    p["type"] = "bool"
                    p["value"] = bool(value)
        p["order"] = (row, col)
        params.append(p)
    params.sort(key=lambda p: (p["order"][0], len(p["order"][1]), p["order"][1]))
    for p in params:
        p.pop("order")
    return params


def _links(obj: ET.Element) -> set[str]:
    out = set()
    for prop in obj.iter("Property"):
        if prop.get("type", "").startswith("App::PropertyExpression"):
            continue
        for el in prop.iter():
            if el.tag in ("Link", "LinkSub"):
                v = el.get("value") or el.get("obj")
                if v:
                    out.add(v)
    return out


SKIP_TYPES = ("App::", "Spreadsheet::", "Sketcher::", "Part::Part2DObject", "Measure::", "TechDraw::",
              "Spreadsheet", "Image::")


def parse_fcstd(path: Path) -> dict:
    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(zf.read("Document.xml"))
    types = {o.get("name"): o.get("type", "") for o in root.iter("Object") if o.get("type")}
    data = root.find("ObjectData")
    params: list[dict] = []
    visible: list[str] = []
    labels: dict[str, str] = {}
    links: dict[str, set[str]] = {}
    for obj in (data if data is not None else []):
        name = obj.get("name", "")
        props = {p.get("name"): p for p in obj.findall("Properties/Property")}
        lab = props.get("Label")
        labels[name] = lab.find("String").get("value", name) if lab is not None and lab.find("String") is not None else name
        links[name] = _links(obj)
        t = types.get(name, "")
        if t == "Spreadsheet::Sheet":
            params += _sheet_params(obj, labels[name])
            continue
        vis = props.get("Visibility")
        b = vis.find("Bool") if vis is not None else None
        if b is not None and b.get("value") == "true" and not t.startswith(SKIP_TYPES):
            visible.append(name)

    def reach(name: str, seen: set[str]) -> set[str]:
        for n in links.get(name, ()):
            if n not in seen:
                seen.add(n)
                reach(n, seen)
        return seen

    used: set[str] = set()
    for v in visible:
        used |= reach(v, set())
    parts = [{"name": v, "label": labels.get(v, v)} for v in visible if v not in used]
    # Several sheets: group by sheet label. Only one: a plain "Parameters" tab.
    if len({p["group"] for p in params}) == 1:
        for p in params:
            p["group"] = "Parameters"
    return {"params": params, "parts": parts}


# ---------------------------------------------------------------- presets

def presets_path(src: Path, kind: str) -> Path:
    if kind == "scad":
        return src.with_suffix(".json")
    return src.with_name(src.name + ".params.json")


def load_presets(src: Path, kind: str) -> dict[str, dict]:
    p = presets_path(src, kind)
    try:
        data = json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    sets = data.get("parameterSets") if isinstance(data, dict) else None
    return {str(k): v for k, v in sets.items() if isinstance(v, dict)} if isinstance(sets, dict) else {}


def _scad_str(value) -> str:
    """OpenSCAD's preset files keep every value as text."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ", ".join(_scad_str(v) for v in value) + "]"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def save_preset(src: Path, kind: str, name: str, values: dict | None) -> dict[str, dict]:
    """Add, replace or (values None) delete one parameter set."""
    p = presets_path(src, kind)
    try:
        data = json.loads(p.read_text("utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    sets = data.get("parameterSets") if isinstance(data.get("parameterSets"), dict) else {}
    if values is None:
        sets.pop(name, None)
    else:
        sets[name] = {k: _scad_str(v) for k, v in values.items()}
    data["parameterSets"] = sets
    data.setdefault("fileFormatVersion", "1")
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=4), "utf-8")
    tmp.replace(p)
    return sets


def coerce(params: list[dict], values: dict) -> dict:
    """Values from the form, checked against the parameters' types."""
    by_name = {p["name"]: p for p in params}
    out = {}
    for name, raw in (values or {}).items():
        p = by_name.get(name)
        if not p:
            continue
        t = p["type"]
        try:
            if t == "bool":
                v = raw if isinstance(raw, bool) else str(raw).strip().lower() in ("1", "true", "yes", "on")
            elif t in ("number", "slider") or (t == "choice" and isinstance(p["value"], (int, float))):
                v = float(raw)
                if v.is_integer() and isinstance(p["value"], int) and not (p.get("step") and float(p["step"]) % 1):
                    v = int(v)
            elif t == "vector":
                items = raw if isinstance(raw, list) else re.split(r"[,\s]+", str(raw).strip("[] "))
                v = [float(x) for x in items if str(x) != ""]
            else:
                v = str(raw)
                if len(v) > 500:
                    raise ValueError("too long")
        except (TypeError, ValueError):
            raise HTTPException(400, f'"{p["label"]}" has a bad value: {raw!r}') from None
        out[name] = v
    return out


def safe_variant(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(name or "")).strip(" .")
    return name[:60] or "custom"


# ---------------------------------------------------------------- routes

class RenderRequest(BaseModel):
    file_id: str
    values: dict = {}
    variant: str = ""
    parts: list[str] = []  # FreeCAD: parts saved as STLs
    combine: list[str] = []  # FreeCAD: parts put together in one multi-colour 3MF


class PresetRequest(BaseModel):
    name: str
    values: dict | None = None  # None deletes


def read_params(f, path: Path) -> dict:
    if f.kind == "scad":
        if path.stat().st_size > MAX_SOURCE:
            raise HTTPException(400, f"{f.name} is too big to read its parameters")
        return parse_scad(path.read_text("utf-8", errors="replace"))
    try:
        return parse_fcstd(path)
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise HTTPException(400, f"Can't read {f.name}: {exc}") from exc


def make_router(client, resolve, rescan, make_id) -> APIRouter:
    """``client``: SlicerClient for the slicer container (it also renders).
    ``resolve(file_id)`` -> (LibFile, Path) or 404; ``rescan()`` blocks."""
    router = APIRouter(prefix="/api/parametric")
    rescanned: set[str] = set()
    lock = threading.Lock()

    def source(file_id: str):
        f, path = resolve(file_id)
        if f.kind not in KINDS:
            raise HTTPException(400, f"{f.name} is not an OpenSCAD or FreeCAD file")
        return f, path

    @router.get("/{file_id}")
    def describe(file_id: str):
        f, path = source(file_id)
        info = read_params(f, path)
        tools: dict = {}
        if client.enabled:
            try:
                tools = client.call("GET", "/render/tools")
            except HTTPException as exc:
                tools = {"error": exc.detail}
        return {"file": {"id": f.id, "name": f.name, "kind": f.kind}, **info,
                "presets": load_presets(path, f.kind), "tools": tools}

    @router.put("/{file_id}/presets")
    def put_preset(file_id: str, req: PresetRequest):
        f, path = source(file_id)
        name = req.name.strip()[:60]
        if not name:
            raise HTTPException(400, "Give the parameter set a name")
        values = None
        if req.values is not None:
            values = coerce(read_params(f, path)["params"], req.values)
        try:
            return {"presets": save_preset(path, f.kind, name, values)}
        except OSError as exc:
            raise HTTPException(500, f"Couldn't save the parameter set: {exc}") from exc

    @router.post("/jobs")
    def start(req: RenderRequest):
        f, path = source(req.file_id)
        info = read_params(f, path)
        values = coerce(info["params"], req.values)
        body = {"path": f.relpath, "kind": f.kind, "variant": safe_variant(req.variant), "values": values}
        if f.kind == "fcstd":
            order = [p["name"] for p in info["parts"]]
            parts = [p for p in order if p in req.parts]
            combine = [p for p in order if p in req.combine]
            if len(combine) < 2:
                combine = []  # one part is just its STL
            if not parts and not combine:
                raise HTTPException(400, "Pick at least one part" if order
                                    else "This FreeCAD file has no visible solid to export")
            body["parts"] = parts
            body["combine"] = combine
            body["labels"] = {p["name"]: p["label"] for p in info["parts"]}
            body["cells"] = {p["name"]: [p["sheet"], p["cell"]] for p in info["params"] if p["name"] in values}
        job = client.call("POST", "/render", json=body)
        log.info("render job %s: %s (%s)", job.get("id"), f.relpath, body["variant"])
        return job

    @router.get("/jobs/{job_id}")
    def job(job_id: str):
        if not job_id.isalnum():
            raise HTTPException(404, "No such job")
        data = client.call("GET", f"/jobs/{job_id}")
        if data.get("state") == "done":
            with lock:
                first = job_id not in rescanned
                rescanned.add(job_id)
            if first:
                rescan()
            data["output_ids"] = [make_id(o) for o in data.get("outputs", [])]
        return data

    return router
