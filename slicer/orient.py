"""Print orientation for STLs: a saved rotation per STL, or auto-orient.

Pure Python (standard library only), unit-tested without the slicer.

A rotation is 9 numbers, a 3x3 matrix row by row, applied as p' = R p (column
vectors), in the STL's own Z-up millimetres. The app saves one per STL (the
"lay face flat" / rotate buttons on the model page); ``auto_matrix`` picks one
by looking for the face to put on the bed that leaves the least overhang.

``oriented_paths`` turns library STLs into rotated copies (cached by file
stamp and rotation, so the slice dialog's piece list and the slice itself read
the very same meshes, and the same body keys).
"""

from __future__ import annotations

import hashlib
import math
import os
import struct
import threading
from array import array
from pathlib import Path

import objects as O

IDENTITY3 = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
OVERHANG_COS = math.cos(math.radians(45))  # faces leaning further than 45 degrees from upright need support
SCORE_TRIANGLES = 60_000  # bigger meshes are simplified before scoring
CACHE_KEEP = 40  # rotated STL copies kept

_AUTO: dict = {}
_LOCK = threading.Lock()


class OrientError(Exception):
    pass


def clean_matrix(value) -> tuple | None:
    """A rotation from a request: 9 finite numbers making an orthonormal
    matrix, or None for "not rotated". Raises OrientError if it isn't one."""
    if value is None or value == []:
        return None
    try:
        m = tuple(float(v) for v in value)
    except (TypeError, ValueError):
        raise OrientError("A rotation is 9 numbers") from None
    if len(m) != 9 or not all(math.isfinite(v) for v in m):
        raise OrientError("A rotation is 9 numbers")
    rows = (m[0:3], m[3:6], m[6:9])
    for i in range(3):
        for j in range(3):
            dot = sum(rows[i][k] * rows[j][k] for k in range(3))
            if abs(dot - (1.0 if i == j else 0.0)) > 1e-3:
                raise OrientError("That isn't a rotation")
    det = (m[0] * (m[4] * m[8] - m[5] * m[7]) - m[1] * (m[3] * m[8] - m[5] * m[6])
           + m[2] * (m[3] * m[7] - m[4] * m[6]))
    if det < 0:
        raise OrientError("That rotation mirrors the model")
    m = tuple(round(v, 9) + 0.0 for v in m)
    return None if is_identity(m) else m


def is_identity(m) -> bool:
    return m is None or all(abs(a - b) < 1e-9 for a, b in zip(m, IDENTITY3))


def multiply(a: tuple, b: tuple) -> tuple:
    """a @ b (apply b first, then a)."""
    return tuple(sum(a[r * 3 + k] * b[k * 3 + c] for k in range(3)) for r in range(3) for c in range(3))


def down_to_bed(d: tuple) -> tuple:
    """The smallest rotation turning unit vector ``d`` to point straight down (0, 0, -1)."""
    x, y, z = d
    # Rodrigues: rotate d onto t = (0, 0, -1); axis = d x t, cos = d . t
    c = -z
    if c > 1 - 1e-12:
        return IDENTITY3
    if c < -1 + 1e-12:  # pointing straight up: half a turn about X
        return (1.0, 0.0, 0.0, 0.0, -1.0, 0.0, 0.0, 0.0, -1.0)
    ax, ay, az = -y, x, 0.0  # d x (0, 0, -1)
    s = math.sqrt(ax * ax + ay * ay)
    ax, ay = ax / s, ay / s
    k = 1 - c
    sn = s  # |d x t| = sin(angle)
    return (
        c + ax * ax * k, ax * ay * k - az * sn, ax * az * k + ay * sn,
        ay * ax * k + az * sn, c + ay * ay * k, ay * az * k - ax * sn,
        az * ax * k - ay * sn, az * ay * k + ax * sn, c + az * az * k,
    )


def rotate(tris: array, m: tuple) -> array:
    """Triangles rotated by ``m`` and set down on the bed (lowest point at z = 0)."""
    a, b, c, d, e, f, g, h, i = m
    out = array("f", bytes(len(tris) * 4))
    lowest = math.inf
    for j in range(0, len(tris), 3):
        x, y, z = tris[j], tris[j + 1], tris[j + 2]
        out[j] = a * x + b * y + c * z
        out[j + 1] = d * x + e * y + f * z
        nz = g * x + h * y + i * z
        out[j + 2] = nz
        if nz < lowest:
            lowest = nz
    if lowest != math.inf and lowest != 0:
        for j in range(2, len(out), 3):
            out[j] -= lowest
    return out


# ---------------------------------------------------------------- auto-orient

def _facets(tris: array):
    """(unit normal, area, 3 vertices) per non-degenerate triangle."""
    out = []
    for j in range(0, len(tris) - 8, 9):
        ax, ay, az, bx, by, bz, cx, cy, cz = tris[j:j + 9]
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        ln = math.sqrt(nx * nx + ny * ny + nz * nz)
        if ln < 1e-12:
            continue
        out.append((nx / ln, ny / ln, nz / ln, ln / 2, ax, ay, az, bx, by, bz, cx, cy, cz))
    return out


def candidates(facets, limit: int = 14) -> list[tuple]:
    """Directions worth putting down on the bed: the six axes and the
    normals of the biggest flat areas (the faces a model could stand on)."""
    buckets: dict[tuple, list] = {}
    for f in facets:
        k = (round(f[0], 2), round(f[1], 2), round(f[2], 2))
        b = buckets.setdefault(k, [0.0, 0.0, 0.0, 0.0])
        b[0] += f[0] * f[3]
        b[1] += f[1] * f[3]
        b[2] += f[2] * f[3]
        b[3] += f[3]
    total = sum(b[3] for b in buckets.values()) or 1.0
    big = sorted(buckets.values(), key=lambda b: -b[3])[:limit]
    dirs = [(0.0, 0.0, -1.0), (0.0, 0.0, 1.0), (1.0, 0.0, 0.0), (-1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, -1.0, 0.0)]
    for b in big:
        if b[3] < total * 0.01:
            break
        ln = math.sqrt(b[0] ** 2 + b[1] ** 2 + b[2] ** 2)
        if ln < 1e-9:
            continue
        d = (b[0] / ln, b[1] / ln, b[2] / ln)
        if all(d[0] * e[0] + d[1] * e[1] + d[2] * e[2] < 0.999 for e in dirs):
            dirs.append(d)
    return dirs


def score(facets, d: tuple, total: float) -> tuple[float, float, float, float]:
    """(cost, overhang area, bed contact area, height) with ``d`` pointing at the bed."""
    dx, dy, dz = d
    lowest = math.inf
    highest = -math.inf
    for f in facets:
        for k in (4, 7, 10):
            h = -(f[k] * dx + f[k + 1] * dy + f[k + 2] * dz)
            if h < lowest:
                lowest = h
            if h > highest:
                highest = h
    overhang = contact = 0.0
    on_bed = lowest + 0.15
    for f in facets:
        down = f[0] * dx + f[1] * dy + f[2] * dz  # 1 = facing the bed
        if down <= OVERHANG_COS:
            continue
        top = max(-(f[4] * dx + f[5] * dy + f[6] * dz), -(f[7] * dx + f[8] * dy + f[9] * dz),
                  -(f[10] * dx + f[11] * dy + f[12] * dz))
        if top <= on_bed:
            if down > 0.99:
                contact += f[3]
        else:
            overhang += f[3] * down
    height = highest - lowest
    # Least overhang first; then a bigger footprint (sticks better); then lower.
    cost = overhang - 0.25 * contact + 0.002 * height * math.sqrt(total)
    return cost, overhang, contact, height


def auto_matrix(tris: array) -> tuple:
    """The rotation that puts the model on the bed with the least overhang.
    The way it already lies wins ties, so a well-placed STL isn't flipped."""
    tris = O.simplify(tris, SCORE_TRIANGLES)
    facets = _facets(tris)
    if not facets:
        return IDENTITY3
    total = sum(f[3] for f in facets)
    dirs = candidates(facets)
    best_d, best = dirs[0], score(facets, dirs[0], total)[0]
    for d in dirs[1:]:
        c = score(facets, d, total)[0]
        if c < best - total * 0.01:
            best_d, best = d, c
    return tuple(round(v, 9) + 0.0 for v in down_to_bed(best_d))


def _stamp(path: Path) -> tuple:
    st = path.stat()
    return (str(path), st.st_mtime_ns, st.st_size)


def auto_for(paths: list[Path]) -> tuple:
    """Auto-orient for one STL, or for several that fit together (one model's
    colours) as a whole. Cached by file stamp."""
    key = tuple(_stamp(p) for p in paths)
    with _LOCK:
        hit = _AUTO.get(key)
    if hit is not None:
        return hit
    tris = array("f")
    for p in paths:
        tris.extend(O.read_stl(p))
    m = auto_matrix(tris)
    with _LOCK:
        if len(_AUTO) > 64:
            _AUTO.clear()
        _AUTO[key] = m
    return m


# ---------------------------------------------------------------- rotated copies

def write_stl(tris: array, dst: Path) -> None:
    n = len(tris) // 9
    tmp = dst.with_name(dst.name + ".part")
    with open(tmp, "wb") as fh:
        fh.write(b"Print Pantry oriented".ljust(80, b" "))
        fh.write(struct.pack("<I", n))
        zero = struct.pack("<3f", 0.0, 0.0, 0.0)
        pack = struct.Struct("<9fH").pack
        for j in range(0, n * 9, 9):
            fh.write(zero)
            fh.write(pack(*tris[j:j + 9], 0))
    os.replace(tmp, dst)


def rotated_copy(src: Path, m: tuple, cache: Path) -> Path:
    """``src`` rotated by ``m``, as a file named like it (piece names come from it)."""
    stamp = _stamp(src)
    h = hashlib.sha1(repr((stamp, m)).encode()).hexdigest()[:16]
    dst = cache / h / src.name
    if dst.is_file():
        os.utime(dst.parent)
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    write_stl(rotate(O.read_stl(src), m), dst)
    _prune(cache)
    return dst


def _prune(cache: Path) -> None:
    try:
        dirs = sorted((d for d in cache.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime)
    except OSError:
        return
    for d in dirs[:-CACHE_KEEP]:
        for f in d.iterdir():
            try:
                f.unlink()
            except OSError:
                pass
        try:
            d.rmdir()
        except OSError:
            pass


def plan(paths: list[Path], saved: dict, auto: bool) -> list[tuple | None]:
    """The rotation for each path (None = as it is). Only STLs are turned
    (a project 3MF keeps its own placement). A saved rotation wins over
    auto-orient. STLs that fit together (the colours of one model) turn as
    one, by the first one's saved rotation or auto-orient of them all."""
    stls = [i for i, p in enumerate(paths) if p.suffix.lower() == ".stl"]
    out: list[tuple | None] = [None] * len(paths)
    if not stls:
        return out
    unique = list(dict.fromkeys(paths[i] for i in stls))
    if len(unique) > 1 and O.overlapping(unique):
        m = next((saved[p] for p in unique if saved.get(p)), None)
        if m is None and auto:
            m = auto_for(unique)
        for i in stls:
            out[i] = None if is_identity(m) else m
        return out
    for i in stls:
        m = saved.get(paths[i])
        if m is None and auto:
            m = auto_for([paths[i]])
        out[i] = None if is_identity(m) else m
    return out


def oriented_paths(paths: list[Path], saved: dict, auto: bool, cache: Path) -> list[Path]:
    """``paths`` with each STL swapped for its rotated copy where it turns."""
    if not saved and not auto:
        return list(paths)
    return [p if m is None else rotated_copy(p, m, cache) for p, m in zip(paths, plan(paths, saved, auto))]
