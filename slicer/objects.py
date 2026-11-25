"""The separate objects in what is being sliced, so each can get its own filament.

Pure Python (standard library only), unit-tested without the slicer.

An "item" is one thing the slice dialog lists with a toolhead picker:

* a project 3MF: one item per object, or one per part when an object is made
  of several parts (a multi-colour model). Keys "o<object id>" and
  "o<object id>:<part id>", matching Metadata/model_settings.config.
* STLs: one item per STL, or one per separate body (connected piece of mesh)
  when an STL holds several. Keys "f<file>" and "f<file>b<body>".

``mesh_blob`` gives the browser each item's triangles for the preview, and
``assign_3mf`` / ``stls_to_3mf`` write the chosen filament per item into the
3MF the slicer reads (``<metadata key="extruder" value="N"/>``, 1-based).
"""

from __future__ import annotations

import re
import struct
import threading
import zipfile
from array import array
from dataclasses import dataclass, field
from itertools import chain
from pathlib import Path
from xml.etree import ElementTree as ET

import layout as L

MODEL = "3D/3dmodel.model"
MODEL_SETTINGS = "Metadata/model_settings.config"
NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
P_PATH = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}path"

MAX_SPLIT_TRIANGLES = 600_000  # bigger STLs aren't split into bodies (too slow in pure Python)
MAX_BODIES = 200  # an STL with more pieces than this is treated as one (e.g. a texture)
PREVIEW_TRIANGLES = 250_000  # bigger models are simplified for the preview (see simplify)


class ObjectsError(Exception):
    pass


@dataclass
class Item:
    key: str
    name: str
    extruder: int = 1
    triangles: int = 0
    size: list[float] = field(default_factory=list)  # x, y, z in mm
    plate: int = 0
    painted: bool = False  # has painted (per-triangle) colours; the picker only sets the base
    tris: array = field(default_factory=lambda: array("f"), repr=False)  # 9 floats per triangle, world mm

    def to_json(self) -> dict:
        # Pieces of one group print as one object (stay together on the plate).
        group = self.key.split(":")[0] if self.key.startswith("o") else re.match(r"f\d+", self.key).group(0)
        lo, hi = bbox(self.tris)
        return {"key": self.key, "name": self.name, "extruder": self.extruder, "group": group,
                "triangles": self.triangles, "size": [round(s, 1) for s in self.size],
                # footprint on the plate (x0, y0, x1, y1 mm), to work out how many copies fit
                "box": [round(lo[0], 1), round(lo[1], 1), round(hi[0], 1), round(hi[1], 1)],
                "plate": self.plate, "painted": self.painted}


# ---------------------------------------------------------------- geometry helpers

IDENTITY = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)


def parse_transform(value: str | None) -> tuple:
    """3MF transform: 12 numbers, p' = p * M (rows m00 m01 m02 / ... / tx ty tz)."""
    if not value:
        return IDENTITY
    try:
        nums = tuple(float(v) for v in value.split())
    except ValueError:
        return IDENTITY
    return nums if len(nums) == 12 else IDENTITY


def compose(inner: tuple, outer: tuple) -> tuple:
    """Transform that applies ``inner`` first, then ``outer``."""
    a, b = inner, outer
    out = []
    for r in range(4):
        row = a[r * 3:r * 3 + 3]
        for c in range(3):
            v = row[0] * b[c] + row[1] * b[3 + c] + row[2] * b[6 + c]
            if r == 3:
                v += b[9 + c]
            out.append(v)
    return tuple(out)


def transformed(tris: array, m: tuple) -> array:
    if m == IDENTITY:
        return tris
    out = array("f", bytes(len(tris) * 4))
    m00, m01, m02, m10, m11, m12, m20, m21, m22, tx, ty, tz = m
    for i in range(0, len(tris), 3):
        x, y, z = tris[i], tris[i + 1], tris[i + 2]
        out[i] = x * m00 + y * m10 + z * m20 + tx
        out[i + 1] = x * m01 + y * m11 + z * m21 + ty
        out[i + 2] = x * m02 + y * m12 + z * m22 + tz
    return out


# ---------------------------------------------------------------- scale

MIN_SCALE, MAX_SCALE = 0.01, 10.0  # 1% to 1000%


class ScaleError(ValueError):
    pass


def clean_scale(value) -> tuple | None:
    """A scale from a request: 3 factors (x, y, z; 1 = as is), or None for
    "not scaled". Raises ScaleError if it isn't one."""
    if value is None or value == []:
        return None
    try:
        s = tuple(float(v) for v in value)
    except (TypeError, ValueError):
        raise ScaleError("A scale is 3 numbers (x, y, z)") from None
    if len(s) != 3 or not all(MIN_SCALE - 1e-9 <= v <= MAX_SCALE + 1e-9 for v in s):
        raise ScaleError(f"Scale must be {MIN_SCALE * 100:g}% to {MAX_SCALE * 100:g}%")
    s = tuple(round(v, 6) for v in s)
    return None if is_unscaled(s) else s


def is_unscaled(s) -> bool:
    return not s or all(abs(v - 1.0) < 1e-9 for v in s)


def scaled(tris: array, s: tuple) -> array:
    """Triangles scaled about the origin by (sx, sy, sz)."""
    if is_unscaled(s):
        return tris
    sx, sy, sz = s
    out = array("f", tris)
    for i in range(0, len(out), 3):
        out[i] *= sx
        out[i + 1] *= sy
        out[i + 2] *= sz
    return out


_BUILD_RE = re.compile(rb"<((?:[\w.-]+:)?build)\b[^>]*>(.*?)</\1\s*>", re.S)
_ITEM_RE = re.compile(rb"<(?:[\w.-]+:)?item\b[^>]*?/?>", re.S)
_ATTR_RE = r'(\s%s\s*=\s*")([^"]*)(")'


def _attr(tag: bytes, name: str) -> str | None:
    m = re.search((_ATTR_RE % re.escape(name)).encode(), tag)
    return m.group(2).decode() if m else None


def scale_3mf(path: Path, s: tuple) -> int:
    """Scale each object of a 3MF in place (build items' transforms), each
    about the middle of its footprint, its bottom staying on the bed, so the
    objects stay where the project put them. Keeps the project's settings,
    modifiers and painted colours. Returns how many objects were scaled."""
    if is_unscaled(s):
        return 0
    sx, sy, sz = s
    with zipfile.ZipFile(path) as zin:
        infos = zin.infolist()
        data = {i.filename: zin.read(i) for i in infos}
        if MODEL not in data:
            raise ObjectsError(f"{path.name} has no 3D model")
        model = _Model(zin, True)
        raw = data[MODEL]
        b = _BUILD_RE.search(raw)
        if not b:
            return 0
        done = 0

        def one(m: re.Match) -> bytes:
            nonlocal done
            tag = m.group(0)
            oid = _attr(tag, "objectid")
            file = _attr(tag, "p:path") or MODEL
            t = parse_transform(_attr(tag, "transform"))
            tris = array("f")
            for pid, f, pm in model.parts(file, oid, IDENTITY):
                tris.extend(model.mesh(f, pid, compose(pm, t)))
            if not tris:
                return tag
            lo, hi = bbox(tris)
            cx, cy, cz = (lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2]
            about = (sx, 0.0, 0.0, 0.0, sy, 0.0, 0.0, 0.0, sz, cx * (1 - sx), cy * (1 - sy), cz * (1 - sz))
            value = " ".join("%.9g" % v for v in compose(t, about)).encode()
            done += 1
            pat = (_ATTR_RE % "transform").encode()
            if re.search(pat, tag):
                return re.sub(pat, lambda a: a.group(1) + value + a.group(3), tag, count=1)
            return re.sub(rb"\s*(/?>)$", lambda e: b' transform="' + value + b'"' + e.group(1), tag, count=1)

        inner = _ITEM_RE.sub(one, b.group(2))
        data[MODEL] = raw[:b.start(2)] + inner + raw[b.end(2):]
    tmp = path.with_name(path.name + ".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for i in infos:
            zout.writestr(i, data[i.filename])
    tmp.replace(path)
    return done


def bbox(tris: array) -> tuple[list[float], list[float]]:
    if not tris:
        return [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]
    xs, ys, zs = tris[0::3], tris[1::3], tris[2::3]
    return [min(xs), min(ys), min(zs)], [max(xs), max(ys), max(zs)]


def _size(tris: array) -> list[float]:
    lo, hi = bbox(tris)
    return [hi[i] - lo[i] for i in range(3)]


# ---------------------------------------------------------------- STL

def read_stl(path: Path) -> array:
    """Triangles of a binary or ASCII STL as 9 floats each."""
    data = path.read_bytes()
    if len(data) >= 84:
        n = struct.unpack_from("<I", data, 80)[0]
        if 84 + n * 50 == len(data):
            out = array("f")
            mv = memoryview(data)
            for i in range(n):
                off = 84 + i * 50 + 12
                out.frombytes(mv[off:off + 36])
            if out.itemsize == 4 and struct.pack("=f", 1.0) != struct.pack("<f", 1.0):
                out.byteswap()
            return out
    text = data.decode("latin-1")
    if "facet" not in text[:4096] and "vertex" not in text:
        raise ObjectsError(f"{path.name} isn't an STL file")
    nums = re.findall(r"vertex\s+(\S+)\s+(\S+)\s+(\S+)", text)
    out = array("f", (float(v) for xyz in nums for v in xyz))
    return out[: len(out) - len(out) % 9]


def split_bodies(tris: array) -> list[array]:
    """Connected pieces of a triangle soup (triangles sharing a vertex join)."""
    count = len(tris) // 9
    if count == 0 or count > MAX_SPLIT_TRIANGLES:
        return [tris]
    raw = tris.tobytes()
    index: dict[bytes, int] = {}
    corners = []
    for t in range(count * 3):
        key = raw[t * 12:t * 12 + 12]
        v = index.get(key)
        if v is None:
            v = index[key] = len(index)
        corners.append(v)
    parent = list(range(len(index)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for t in range(count):
        a, b, c = find(corners[t * 3]), find(corners[t * 3 + 1]), find(corners[t * 3 + 2])
        if b != a:
            parent[b] = a
        if c != a and find(c) != a:
            parent[find(c)] = a
    groups: dict[int, list[int]] = {}
    for t in range(count):
        groups.setdefault(find(corners[t * 3]), []).append(t)
    if len(groups) == 1 or len(groups) > MAX_BODIES:
        return [tris]
    bodies = []
    for tri_ids in groups.values():
        body = array("f")
        for t in tri_ids:
            body.extend(tris[t * 9:t * 9 + 9])
        bodies.append(body)
    bodies.sort(key=len, reverse=True)
    return bodies


_STL_CACHE: dict[tuple, list[array]] = {}
_STL_LOCK = threading.Lock()


def stl_bodies(path: Path) -> list[array]:
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    with _STL_LOCK:
        hit = _STL_CACHE.get(key)
    if hit is not None:
        return hit
    bodies = split_bodies(read_stl(path))
    with _STL_LOCK:
        if len(_STL_CACHE) > 8:
            _STL_CACHE.clear()
        _STL_CACHE[key] = bodies
    return bodies


DETAIL_SIZE = 0.25  # a body smaller than this share of the biggest body's size is a detail
DETAIL_VOLUME = 0.02  # ... and its box holds less than this share of the biggest box


def group_details(bodies: list[array]) -> list[tuple[str | None, array]]:
    """Bodies with the small loose bits (embossed letters, tick marks, a logo)
    gathered into one "Details" piece per height band, so text exported with
    its model shows as one piece rather than one per letter.

    A body is a detail when it is much smaller than the biggest body. Details
    with the same bottom and top heights (one pad / extrusion) go together.
    Returns (name or None, triangles); details come last."""
    if len(bodies) < 3:
        return [(None, b) for b in bodies]
    boxes = [bbox(b) for b in bodies]
    sizes = [[hi[k] - lo[k] for k in range(3)] for lo, hi in boxes]
    vols = [max(s[0], 1e-6) * max(s[1], 1e-6) * max(s[2], 1e-6) for s in sizes]
    big = max(range(len(bodies)), key=lambda i: vols[i])
    reach = max(sizes[big])
    small = [i for i in range(len(bodies)) if i != big
             and max(sizes[i]) < DETAIL_SIZE * reach and vols[i] < DETAIL_VOLUME * vols[big]]
    if len(small) < 2:
        return [(None, b) for b in bodies]
    bands: dict[tuple, list[int]] = {}
    for i in small:
        lo, hi = boxes[i]
        bands.setdefault((round(lo[2], 1), round(hi[2], 1)), []).append(i)
    # A band of one lone bit stays its own body.
    grouped = {i for members in bands.values() if len(members) > 1 for i in members}
    if not grouped:
        return [(None, b) for b in bodies]
    out: list[tuple[str | None, array]] = [(None, b) for i, b in enumerate(bodies) if i not in grouped]
    multi = [(band, m) for band, m in bands.items() if len(m) > 1]
    for band, members in sorted(multi, key=lambda bm: (-len(bm[1]), bm[0])):
        tris = array("f")
        for i in members:
            tris.extend(bodies[i])
        name = "Details" if len(multi) == 1 else f"Details {band[0]:g}–{band[1]:g} mm"
        out.append((f"{name} ({len(members)} bits)", tris))
    return out


def stl_items(paths: list[Path], start: int = 0) -> list[Item]:
    """Items of STLs; keys count files from ``start`` (after a 3MF it's 1)."""
    items = []
    for i, path in enumerate(paths, start=start):
        pieces = group_details(stl_bodies(path))
        stem = path.stem
        if len(pieces) == 1:
            tris = pieces[0][1]
            items.append(Item(f"f{i}", stem, triangles=len(tris) // 9, size=_size(tris), tris=tris))
            continue
        n = 0
        for j, (label, body) in enumerate(pieces):
            if label is None:
                n += 1
                label = f"Body {n}"
            name = f"{stem} · {label[0].lower() + label[1:]}" if len(paths) + start > 1 else label
            items.append(Item(f"f{i}b{j}", name, triangles=len(body) // 9, size=_size(body), tris=body))
    # Separate pieces start on separate toolheads: one per STL when there are
    # several (bodies of one STL share it), else one per body. More pieces
    # than toolheads all start on T1 (cycling colours over them looks random).
    single = len(paths) + start == 1
    if single and len(items) > 4:
        for item in items:
            item.extruder = 1
        return items
    for item in items:
        m = re.fullmatch(r"f(\d+)(?:b(\d+))?", item.key)
        item.extruder = int(m.group(1)) + 1 if not single else int(m.group(2) or 0) + 1
    return items


def overlapping(paths: list[Path]) -> bool:
    """True when the STLs' boxes overlap, i.e. they look like the colours of
    one model that fit together, rather than separate pieces."""
    boxes = []
    for p in paths:
        body = array("f")
        for b in stl_bodies(p):
            body.extend(b)
        boxes.append(bbox(body))
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            (alo, ahi), (blo, bhi) = boxes[i], boxes[j]
            if all(alo[k] <= bhi[k] + 0.01 and blo[k] <= ahi[k] + 0.01 for k in range(3)):  # touching counts
                return True
    return False


def _mesh_xml(obj_id: int, name: str, tris: array, shift=(0.0, 0.0, 0.0)) -> str:
    index: dict[tuple, int] = {}
    verts: list[str] = []
    faces: list[str] = []
    dx, dy, dz = shift
    for t in range(len(tris) // 9):
        ids = []
        for c in range(3):
            o = t * 9 + c * 3
            key = (tris[o], tris[o + 1], tris[o + 2])
            v = index.get(key)
            if v is None:
                v = index[key] = len(verts)
                verts.append('    <vertex x="%.5g" y="%.5g" z="%.5g"/>\n' % (key[0] + dx, key[1] + dy, key[2] + dz))
            ids.append(v)
        if ids[0] != ids[1] and ids[1] != ids[2] and ids[0] != ids[2]:
            faces.append('    <triangle v1="%d" v2="%d" v3="%d"/>\n' % tuple(ids))
    return (' <object id="%d" name="%s" type="model">\n  <mesh>\n   <vertices>\n%s   </vertices>\n'
            '   <triangles>\n%s   </triangles>\n  </mesh>\n </object>\n'
            % (obj_id, esc(name), "".join(verts), "".join(faces)))


def esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def plate_3mf(paths: list[Path], assign: dict, dst: Path, keep_positions: bool = True, plate: int = 0,
              copies: int = 1, bed: dict | None = None, scale: tuple | None = None) -> bool:
    """A plain 3MF of what goes on the plate: a project 3MF's printed meshes
    and/or STLs (split into bodies), a filament per piece from ``assign``
    (0 = leave it off the plate).

    Project objects stay objects (their parts stay parts). STLs: with
    ``keep_positions`` they become parts of one object, so pieces made to fit
    together (colours of one model) stay where they are; otherwise each STL
    is its own object. The slicer lays the objects out (--arrange).

    Used for STLs, and for a project when objects move to more toolheads than
    it has filaments (OrcaSlicer can't grow a project's filament settings:
    "invalid size found"), pieces are left off, STLs are added, unused
    filaments are dropped, or ``copies`` > 1 (each object written that many
    times). The project's per-object settings and modifiers are left behind.

    With ``bed`` ({"area": (x0, y0, x1, y1), "gap": mm}) the objects are
    spread over the bed here (see layout.spread) and True is returned: slice
    without --arrange to keep that. False: they didn't fit that way, let the
    slicer arrange them.

    ``scale`` (x, y, z factors on the bed's axes) resizes every piece; pieces
    keep their places relative to each other (scaled about one origin)."""
    items = items_for(paths)
    if not is_unscaled(scale):
        for it in items:
            it.tris = scaled(it.tris, scale)
    keep = [it for it in items if str(assign.get(it.key, "")) != "0"]
    if plate:
        keep = [it for it in keep if not it.key.startswith("o") or not it.plate or it.plate == plate]
    if not keep:
        raise ObjectsError("Nothing left on the plate: every piece is set to don't print")
    if any(it.painted for it in keep):
        raise ObjectsError("This project has painted colours, so it can't be re-arranged or put on new toolheads here")
    groups: dict[str, list[Item]] = {}
    for it in keep:
        if it.key.startswith("o"):
            g = it.key.split(":")[0]
        else:
            g = "stl" if keep_positions else re.match(r"f\d+", it.key).group(0)
        groups.setdefault(g, []).append(it)
    stems = {f"f{i}": p.stem for i, p in enumerate(paths)}
    named = []
    for g, members in groups.items():
        if g.startswith("o"):
            name = members[0].name.split(" · ")[0]
        elif len(members) == 1:
            name = members[0].name
        else:
            name = stems.get(g) or next((p.stem for p in paths if p.suffix.lower() == ".stl"), "STLs")
        named.append((name, members))
    return write_3mf(named, assign, dst, paths[0].stem, max(1, int(copies or 1)), bed)


def stls_to_3mf(paths: list[Path], assign: dict, dst: Path, keep_positions: bool) -> None:
    plate_3mf(paths, assign, dst, keep_positions)


def rebuild_3mf(src: Path, assign: dict, dst: Path, plate: int = 0) -> None:
    plate_3mf([src], assign, dst, True, plate)


def write_3mf(groups: list[tuple[str, list[Item]]], assign: dict, dst: Path, title: str, copies: int = 1,
              bed: dict | None = None) -> bool:
    """One object per group, one part per item, filament per part from
    ``assign`` (else the item's own). Objects are centred on their origin;
    slice with --arrange so the slicer places them. ``copies`` > 1 writes
    each object that many times, each copy a full object with the same
    filaments (not instances: OrcaSlicer drops the settings of extra
    instances in a 3MF it didn't make).
    ``bed``: place them spread over the bed instead (see plate_3mf)."""
    resources, build, config = [], [], []
    next_id = 1
    boxes = []
    for name, group in groups:
        both = array("f")
        for it in group:
            both.extend(it.tris)
        boxes.append(bbox(both))
    places = None
    if bed:
        x0, y0, x1, y1 = bed["area"]
        m = 2 * bed.get("margin", 0)  # a brim widens each footprint
        rects = [{"g": g, "w": hi[0] - lo[0] + m, "d": hi[1] - lo[1] + m} for g, (lo, hi) in enumerate(boxes)]
        spots = L.spread(rects, copies, (x1 - x0, y1 - y0), bed.get("gap", L.GAP))
        if spots:
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            places = {}
            for p in spots:
                places.setdefault(p["g"], []).append((cx + p["x"], cy + p["y"]))
    for g, (name, group) in enumerate(groups):
        lo, hi = boxes[g]
        # Each object centred on its own origin, lowest point on the plate.
        shift = (-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2])
        first = _slot(assign.get(group[0].key), group[0].extruder)
        # Every copy is its own object with its own parts and settings.
        # OrcaSlicer splits extra instances of an object in a 3MF it didn't
        # write into new objects without the per-object/part settings
        # (bbs_3mf.cpp "split as instance"), so those copies fell back to
        # filament 1 while the first copy kept the picked one.
        meshes = [_mesh_xml(0, it.name, it.tris, shift) for it in group]  # id swapped per copy
        for c in range(copies):
            part_ids, parts_cfg = [], []
            for it, mesh in zip(group, meshes):
                pid = next_id
                next_id += 1
                resources.append(mesh.replace(' <object id="0"', ' <object id="%d"' % pid, 1))
                part_ids.append(pid)
                ext = _slot(assign.get(it.key), it.extruder)
                parts_cfg.append('  <part id="%d" subtype="normal_part">\n   <metadata key="name" value="%s"/>\n'
                                 '   <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>\n'
                                 '   <metadata key="extruder" value="%d"/>\n  </part>\n' % (pid, esc(it.name), ext))
            oid = next_id
            next_id += 1
            comps = "".join('   <component objectid="%d"/>\n' % p for p in part_ids)
            resources.append(' <object id="%d" name="%s" type="model">\n  <components>\n%s  </components>\n </object>\n'
                             % (oid, esc(name), comps))
            x, y = places[g][c] if places else (128 + g * 10, 128 + c * 5)
            build.append('  <item objectid="%d" transform="1 0 0 0 1 0 0 0 1 %.3f %.3f 0" printable="1"/>\n'
                         % (oid, x, y))
            config.append(' <object id="%d">\n  <metadata key="name" value="%s"/>\n  <metadata key="extruder" value="%d"/>\n%s </object>\n'
                          % (oid, esc(name), first, "".join(parts_cfg)))
    model = ('<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" '
             'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n'
             ' <metadata name="Title">%s</metadata>\n<resources>\n%s</resources>\n<build>\n%s</build>\n</model>\n'
             % (esc(title), "".join(resources), "".join(build)))
    cfg = '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n%s</config>\n' % "".join(config)
    types = ('<?xml version="1.0" encoding="UTF-8"?>\n'
             '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
             ' <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
             ' <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
             ' <Default Extension="config" ContentType="text/xml"/>\n</Types>\n')
    rels = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
            ' <Relationship Target="/3D/3dmodel.model" Id="rel-1" '
            'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n</Relationships>\n')
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", types)
        z.writestr("_rels/.rels", rels)
        z.writestr(MODEL, model)
        z.writestr(MODEL_SETTINGS, cfg)
    return places is not None


def _slot(value, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n >= 1 else default


# ---------------------------------------------------------------- 3MF

_MESH_OPEN_RE = re.compile(rb"<((?:[\w.-]+:)?mesh)(?=[\s/>])[^>]*>")
_VERTEX_RE = re.compile(rb'<(?:[\w.-]+:)?vertex\s+x="([^"]*)"\s+y="([^"]*)"\s+z="([^"]*)"')
_TRIANGLE_RE = re.compile(rb'<(?:[\w.-]+:)?triangle\s+v1="(\d+)"\s+v2="(\d+)"\s+v3="(\d+)"')


def _mesh_tris(raw: bytes, m: tuple = IDENTITY) -> array:
    """Triangles (9 floats each, transformed by ``m``) of the inside of a 3MF <mesh>.

    Regular expressions over the bytes: an ElementTree of a million-triangle
    mesh (Bambu Studio writes STEP imports that fine) takes many seconds and
    gigabytes. Attributes in another order fall back to ElementTree."""
    nv, nt = raw.count(b"<vertex"), raw.count(b"<triangle ")
    vs, ve = raw.find(b"<vertices"), raw.find(b"</vertices")
    ts, te = raw.find(b"<triangles"), raw.find(b"</triangles")
    # Fast path: plain x/y/z and v1/v2/v3 attributes, so every quoted value is a number we want.
    vals = raw[raw.find(b">", vs) + 1:ve].split(b'"')[1::2] if 0 <= vs < ve else []
    tvals = raw[raw.find(b">", ts) + 1:te].split(b'"')[1::2] if 0 <= ts < te else []
    if len(vals) == 3 * nv and len(tvals) == 3 * nt:
        verts = list(zip(*[iter(vals)] * 3))
        faces = tvals
    else:
        verts = _VERTEX_RE.findall(raw)
        faces = list(chain.from_iterable(_TRIANGLE_RE.findall(raw)))
    if len(verts) != nv or len(faces) != 3 * nt:
        mesh = ET.fromstring(b'<mesh xmlns="' + NS[1:-1].encode() + b'">' + raw + b"</mesh>")
        return transformed(_read_mesh_el(mesh), m)
    pts = [(float(x), float(y), float(z)) for x, y, z in verts]
    if m != IDENTITY:
        m00, m01, m02, m10, m11, m12, m20, m21, m22, tx, ty, tz = m
        pts = [(x * m00 + y * m10 + z * m20 + tx, x * m01 + y * m11 + z * m21 + ty, x * m02 + y * m12 + z * m22 + tz)
               for x, y, z in pts]
    idx = list(map(int, faces))
    if idx and max(idx) >= len(pts):  # broken file: skip triangles that point nowhere
        n = len(pts)
        idx = [i for k in range(0, len(idx) - 2, 3) if max(idx[k:k + 3]) < n for i in idx[k:k + 3]]
    return array("f", chain.from_iterable(map(pts.__getitem__, idx)))


def _read_mesh_el(mesh: ET.Element) -> array:
    out = array("f")
    verts = [(float(v.get("x", 0)), float(v.get("y", 0)), float(v.get("z", 0)))
             for v in mesh.iter(NS + "vertex")]
    for t in mesh.iter(NS + "triangle"):
        try:
            a, b, c = verts[int(t.get("v1"))], verts[int(t.get("v2"))], verts[int(t.get("v3"))]
        except (IndexError, TypeError, ValueError):
            continue
        out.extend(a)
        out.extend(b)
        out.extend(c)
    return out


class _Model:
    """The objects of a 3MF, following components into other model files."""

    def __init__(self, zf: zipfile.ZipFile, with_mesh: bool):
        self.zf = zf
        self.with_mesh = with_mesh
        self.files: dict[str, dict[str, ET.Element]] = {}
        self.painted_files: set[str] = set()
        self.raw_meshes: list[bytes] = []  # <mesh> insides, cut out before parsing the XML

    def objects(self, name: str) -> dict[str, ET.Element]:
        name = name.lstrip("/")
        if name not in self.files:
            try:
                data = self.zf.read(name)
            except KeyError:
                self.files[name] = {}
                return self.files[name]
            if b"paint_color" in data or b"slic3rpe:mmu_segmentation" in data:
                self.painted_files.add(name)
            root = ET.fromstring(self._cut_meshes(data))
            del data
            res = root.find(NS + "resources")
            self.files[name] = {o.get("id"): o for o in (res.iter(NS + "object") if res is not None else [])}
        return self.files[name]

    def _cut_meshes(self, data: bytes) -> bytes:
        """The XML with each <mesh>'s inside moved to raw_meshes (parsed by _mesh_tris)."""
        out, pos = [], 0
        while (m := _MESH_OPEN_RE.search(data, pos)) is not None:
            tag = m.group(1)
            if m.group(0).endswith(b"/>"):
                out.append(data[pos:m.end()])
                pos = m.end()
                continue
            end = data.find(b"</" + tag, m.end())
            close = data.find(b">", end) if end >= 0 else -1
            if close < 0:
                break  # let the XML parser complain
            self.raw_meshes.append(data[m.end():end])
            out.append(data[pos:m.start()] + b'<%s ppmesh="%d"/>' % (tag, len(self.raw_meshes) - 1))
            pos = close + 1
        out.append(data[pos:])
        return b"".join(out)

    def parts(self, file: str, oid: str, m: tuple = IDENTITY, depth: int = 0):
        """(part id, file, transform) for each mesh under an object."""
        obj = self.objects(file).get(oid)
        if obj is None or depth > 4:
            return []
        comps = obj.find(NS + "components")
        if comps is None:
            return [(oid, file, m)]
        out = []
        for c in comps.iter(NS + "component"):
            sub = c.get(P_PATH) or file
            cm = compose(parse_transform(c.get("transform")), m)
            inner = self.parts(sub, c.get("objectid"), cm, depth + 1)
            # The part id is the component's object id (as in model_settings.config).
            out += [(c.get("objectid") if depth == 0 else pid, f, t) for pid, f, t in inner]
        return out

    def mesh(self, file: str, oid: str, m: tuple) -> array:
        obj = self.objects(file).get(oid)
        mesh = obj.find(NS + "mesh") if obj is not None else None
        if mesh is None or not mesh.get("ppmesh", "").isdigit():
            return array("f")
        return _mesh_tris(self.raw_meshes[int(mesh.get("ppmesh"))], m)


def _settings(zf: zipfile.ZipFile) -> ET.Element | None:
    if MODEL_SETTINGS not in zf.namelist():
        return None
    try:
        return ET.fromstring(zf.read(MODEL_SETTINGS))
    except ET.ParseError:
        return None


def _meta(el: ET.Element, key: str) -> str:
    for m in el.findall("metadata"):
        if m.get("key") == key:
            return m.get("value", "")
    return ""


def threemf_items(path: Path, with_mesh: bool = False) -> list[Item]:
    with zipfile.ZipFile(path) as zf:
        if MODEL not in zf.namelist():
            raise ObjectsError(f"{path.name} has no 3D model")
        model = _Model(zf, with_mesh)
        root = ET.fromstring(zf.read(MODEL))
        settings = _settings(zf)
        cfg_objects = {o.get("id"): o for o in settings.findall("object")} if settings is not None else {}
        plates: dict[str, int] = {}
        if settings is not None:
            for n, plate in enumerate(settings.findall("plate"), start=1):
                pid = int(_meta(plate, "plater_id") or n) if (_meta(plate, "plater_id") or "").isdigit() else n
                for inst in plate.findall("model_instance"):
                    plates.setdefault(_meta(inst, "object_id"), pid)
        build = root.find(NS + "build")
        instances: dict[str, list[tuple]] = {}
        for it in (build.iter(NS + "item") if build is not None else []):
            if it.get("printable", "1") == "0":
                continue
            instances.setdefault(it.get("objectid"), []).append(parse_transform(it.get("transform")))
        items: list[Item] = []
        for oid, transforms in instances.items():
            cfg = cfg_objects.get(oid)
            obj_el = model.objects(MODEL).get(oid)
            oname = (_meta(cfg, "name") if cfg is not None else "") or (obj_el.get("name") if obj_el is not None else "") or f"Object {oid}"
            o_ext = _slot(_meta(cfg, "extruder") if cfg is not None else None, 1)
            cfg_parts = {p.get("id"): p for p in cfg.findall("part")} if cfg is not None else {}
            normal = {pid for pid, p in cfg_parts.items() if p.get("subtype", "normal_part") == "normal_part"}
            parts = model.parts(MODEL, oid)
            # Modifiers, negative parts and supports blockers aren't printed on their own.
            printed = [p for p in parts if p[0] in normal] or parts
            split = len(printed) > 1
            groups = [[p] for p in printed] if split else [printed]
            for group in groups:
                if not group:
                    continue
                pid = group[0][0]
                part_cfg = cfg_parts.get(pid)
                if split:
                    key = f"o{oid}:{pid}"
                    name = f"{oname} · {(_meta(part_cfg, 'name') if part_cfg is not None else '') or 'part ' + str(pid)}"
                    ext = _slot(_meta(part_cfg, "extruder") if part_cfg is not None else None, o_ext)
                else:
                    key, name, ext = f"o{oid}", oname, o_ext
                tris = array("f")
                painted = False
                for _pid, file, m in group:
                    for t in transforms:
                        tris.extend(model.mesh(file, _pid, compose(m, t)))
                    painted = painted or file.lstrip("/") in model.painted_files
                count = len(tris) // 9 // len(transforms)
                item = Item(key, name, extruder=ext, triangles=count, plate=plates.get(oid, 0), painted=painted)
                item.size = _size(tris)
                if with_mesh:
                    item.tris = tris
                items.append(item)
        return items


def assign_3mf(src: Path, dst: Path, assign: dict) -> None:
    """Copy a 3MF, setting the filament (extruder) of the items in ``assign``.

    Writes Metadata/model_settings.config, creating it for a plain 3MF."""
    if not assign:
        dst.write_bytes(src.read_bytes())
        return
    with zipfile.ZipFile(src) as zin:
        settings = _settings(zin)
        if settings is None:
            settings = ET.Element("config")
        objs = {o.get("id"): o for o in settings.findall("object")}

        def set_meta(el: ET.Element, value: int) -> None:
            for m in el.findall("metadata"):
                if m.get("key") == "extruder":
                    m.set("value", str(value))
                    return
            m = ET.Element("metadata", {"key": "extruder", "value": str(value)})
            # Keep the name first, as the slicers write it.
            el.insert(1 if el.find("metadata") is not None else 0, m)

        for key, value in assign.items():
            slot = _slot(value, 0)
            mm = re.fullmatch(r"o(\w+?)(?::(\w+))?", str(key))
            if not slot or not mm:
                continue
            oid, pid = mm.group(1), mm.group(2)
            obj = objs.get(oid)
            if obj is None:
                obj = ET.SubElement(settings, "object", {"id": oid})
                # A new object entry must come before plates/assemble blocks.
                settings.remove(obj)
                first_other = next((i for i, el in enumerate(settings) if el.tag != "object"), len(settings))
                settings.insert(first_other, obj)
                objs[oid] = obj
            if pid is None:
                set_meta(obj, slot)
                # A part with its own filament would win over the object's.
                for part in obj.findall("part"):
                    if part.get("subtype", "normal_part") == "normal_part":
                        set_meta(part, slot)
            else:
                part = next((p for p in obj.findall("part") if p.get("id") == pid), None)
                if part is None:
                    part = ET.SubElement(obj, "part", {"id": pid, "subtype": "normal_part"})
                set_meta(part, slot)
        data = ET.tostring(settings, encoding="utf-8", xml_declaration=True)
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if item.filename == MODEL_SETTINGS:
                    continue
                zout.writestr(item, zin.read(item))
            zout.writestr(MODEL_SETTINGS, data)


# ---------------------------------------------------------------- what the dialog asks for

_ITEMS_CACHE: dict[tuple, list[Item]] = {}


def items_for(paths: list[Path]) -> list[Item]:
    """Items (with their triangles) of one project 3MF or of STLs; cached by file stamp."""
    key = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in paths)
    with _STL_LOCK:
        hit = _ITEMS_CACHE.get(key)
    if hit is not None:
        return hit
    if any(p.suffix.lower() != ".stl" for p in paths[1:]):
        raise ObjectsError("Only STLs can be added to the plate")
    if paths[0].suffix.lower() == ".3mf":
        items = threemf_items(paths[0], with_mesh=True) + stl_items(paths[1:], start=1)
    else:
        items = stl_items(paths)
    with _STL_LOCK:
        if len(_ITEMS_CACHE) > 6:
            _ITEMS_CACHE.clear()
        _ITEMS_CACHE[key] = items
    return items


def simplify(tris: array, budget: int) -> array:
    """At most about ``budget`` triangles of the same shape (vertex clustering).

    Vertices are snapped to a grid and merged per cell (to their average), and
    triangles that collapse are dropped, so the surface stays closed and solid.
    (Keeping every n-th triangle instead leaves a see-through mess of slivers.)
    The grid gets coarser until the result fits."""
    n = len(tris) // 9
    if n <= budget or n == 0:
        return tris
    it = iter(tris)
    ids: dict = {}
    idx = [ids.setdefault(p, len(ids)) for p in zip(it, it, it)]
    uniq = list(ids)
    del ids
    xs = [p[0] for p in uniq]
    ys = [p[1] for p in uniq]
    zs = [p[2] for p in uniq]
    lo = (min(xs), min(ys), min(zs))
    span = max(max(xs) - lo[0], max(ys) - lo[1], max(zs) - lo[2], 1e-6)
    del xs, ys, zs
    # A surface over a grid of res cells a side gives about 3-6 res² triangles.
    res = max(8, int((budget / 3) ** 0.5))
    x0, y0, z0 = lo
    for _ in range(8):
        inv = res / span
        r1 = res + 2
        cell: dict = {}
        cmap = [cell.setdefault((int((x - x0) * inv) * r1 + int((y - y0) * inv)) * r1 + int((z - z0) * inv), len(cell))
                for x, y, z in uniq]
        faces = {}
        ii = iter(idx)
        for a, b, c in zip(ii, ii, ii):
            a, b, c = cmap[a], cmap[b], cmap[c]
            if a != b and b != c and a != c:
                faces[(a, b, c) if a < b and a < c else (b, c, a) if b < c else (c, a, b)] = None
        if len(faces) <= budget or res <= 8:
            break
        res = max(8, int(res * min(0.9, (budget / len(faces)) ** 0.5 * 0.97)))
    m = len(cell)
    cnt = [0] * m
    sx = [0.0] * m
    sy = [0.0] * m
    sz = [0.0] * m
    for c, (x, y, z) in zip(cmap, uniq):
        cnt[c] += 1
        sx[c] += x
        sy[c] += y
        sz[c] += z
    rep = [(sx[c] / cnt[c], sy[c] / cnt[c], sz[c] / cnt[c]) for c in range(m)]
    return array("f", [v for f in faces for k in f for v in rep[k]])


_BLOB_CACHE: dict[int, tuple] = {}
_BLOB_LOCK = threading.Lock()  # one at a time: the dialog's request waits for the warm-up


def mesh_blob(items: list[Item]) -> bytes:
    """Little-endian: uint32 item count, then per item uint32 triangle count and
    that many triangles of 9 float32 (mm, Z up). Models over PREVIEW_TRIANGLES
    are simplified, each piece by its share. Cached with the items."""
    with _BLOB_LOCK:
        hit = _BLOB_CACHE.get(id(items))
        if hit is not None and hit[0] is items:
            return hit[1]
        total = sum(len(i.tris) // 9 for i in items)
        out = bytearray(struct.pack("<I", len(items)))
        for it in items:
            tris = it.tris
            if total > PREVIEW_TRIANGLES:
                tris = simplify(tris, max(2000, PREVIEW_TRIANGLES * (len(tris) // 9) // total))
            if struct.pack("=f", 1.0) != struct.pack("<f", 1.0):
                tris = array("f", tris)
                tris.byteswap()
            out += struct.pack("<I", len(tris) // 9)
            out += tris.tobytes()
        blob = bytes(out)
        if len(_BLOB_CACHE) > 6:
            _BLOB_CACHE.clear()
        _BLOB_CACHE[id(items)] = (items, blob)
        return blob
