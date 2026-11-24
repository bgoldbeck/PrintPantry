"""Copies spread evenly over the whole bed.

OrcaSlicer's --arrange packs objects tightly in the middle of the plate. For
copies we place them ourselves instead: rows of pieces with the spare room
shared out evenly between them (and the bed edges), so they cover the plate.
With "print by object" the gap between pieces is the toolhead's clearance
radius, so each object can be printed whole without the head hitting the
others.

The slice dialog has the same algorithm in JavaScript (spreadCopies in
app/static/slicing.js) for its preview and its "Max" button: keep them in step.
"""
from __future__ import annotations

import math

GAP = 8.0  # mm between pieces printed layer by layer
EDGE = 3.0  # mm kept clear at the bed edges
MAX_COPIES = 200


def _rows_greedy(items: list[dict], width: float, gap: float) -> list[list[dict]]:
    rows, row, x = [], [], 0.0
    for it in items:
        if row and x + gap + it["w"] > width:
            rows.append(row)
            row, x = [], 0.0
        x = x + (gap if row else 0) + it["w"]
        row.append(it)
    if row:
        rows.append(row)
    return rows


def _rows_even(items: list[dict], n_rows: int) -> list[list[dict]]:
    n = len(items)
    rows, start = [], 0
    for r in range(n_rows):
        k = n // n_rows + (1 if r < n % n_rows else 0)
        rows.append(items[start:start + k])
        start += k
    return [r for r in rows if r]


def _fits(rows: list[list[dict]], W: float, D: float, gap: float, edge: float) -> bool:
    for row in rows:
        if sum(i["w"] for i in row) + gap * (len(row) - 1) + 2 * edge > W + 1e-6:
            return False
    depth = sum(max(i["d"] for i in row) for row in rows)
    return depth + gap * (len(rows) - 1) + 2 * edge <= D + 1e-6


def _score(rows: list[list[dict]], W: float, D: float) -> float:
    """The smallest even gap: bigger = more spread out."""
    h = min((W - sum(i["w"] for i in row)) / (len(row) + 1) for row in rows)
    v = (D - sum(max(i["d"] for i in row) for row in rows)) / (len(rows) + 1)
    return min(h, v)


def _spaced(sizes: list[float], total: float, gap: float, edge: float) -> list[float]:
    """Centres of ``sizes`` laid along ``total`` with the free room shared out:
    the same gap at the edges and between pieces when that leaves at least
    ``gap`` between them, else ``gap`` between and the rest at the edges."""
    k = len(sizes)
    free = total - sum(sizes)
    even = free / (k + 1)
    end = even if k == 1 else max(edge, min(even, (free - (k - 1) * gap) / 2))
    between = (free - 2 * end) / (k - 1) if k > 1 else 0
    out, pos = [], end
    for s in sizes:
        out.append(pos + s / 2)
        pos += s + between
    return out


def spread(rects: list[dict], copies: int, bed: tuple[float, float],
           gap: float = GAP, edge: float = EDGE) -> list[dict] | None:
    """``copies`` sets of footprints ``rects`` ({g, w, d} mm) spread over a
    ``bed`` (width, depth mm); None if they don't fit. Returns {c, g, x, y,
    w, d} per piece: centres in mm from the middle of the bed (y = back)."""
    W, D = float(bed[0]), float(bed[1])
    items = [{"c": c, "g": r["g"], "w": float(r["w"]), "d": float(r["d"])}
             for c in range(max(1, int(copies))) for r in rects]
    if not items:
        return None
    items.sort(key=lambda i: (-i["d"], -i["w"], i["c"]))
    best, best_score = None, -math.inf
    for rows in [_rows_greedy(items, W - 2 * edge, gap)] + [_rows_even(items, n) for n in range(1, len(items) + 1)]:
        if not _fits(rows, W, D, gap, edge):
            continue
        s = _score(rows, W, D)
        if s > best_score + 1e-9 or (abs(s - best_score) <= 1e-9 and len(rows) < len(best)):
            best, best_score = rows, s
    if best is None:
        return None
    depths = [max(i["d"] for i in row) for row in best]
    ys = _spaced(depths, D, gap, edge)
    out = []
    for row, yc in zip(best, ys):
        for it, xc in zip(row, _spaced([i["w"] for i in row], W, gap, edge)):
            out.append({**it, "x": xc - W / 2, "y": D / 2 - yc})
    return out


def max_copies(rects: list[dict], bed: tuple[float, float], gap: float = GAP, edge: float = EDGE) -> int:
    if not rects or spread(rects, 1, bed, gap, edge) is None:
        return 1
    n = 1
    while n < MAX_COPIES and spread(rects, n + 1, bed, gap, edge) is not None:
        n += 1
    return n


def bed_of(machine: dict) -> tuple[float, float, float, float]:
    """(x0, y0, x1, y1) of the machine preset's printable area."""
    pts = []
    for p in machine.get("printable_area") or []:
        try:
            x, y = str(p).lower().split("x")
            pts.append((float(x), float(y)))
        except ValueError:
            continue
    if not pts:
        return (0.0, 0.0, 256.0, 256.0)
    return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))


def _num(v, default: float) -> float:
    if isinstance(v, list):
        v = v[0] if v else None
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def brim(process: dict) -> float:
    """mm a brim adds on each side of an object's footprint (0 without one)."""
    kind = process.get("brim_type")
    kind = (kind[0] if isinstance(kind, list) and kind else kind) or "auto_brim"
    if kind in ("no_brim", "inner_only", "brim_ears"):
        return 0.0
    return max(0.0, _num(process.get("brim_width"), 0.0)) + max(0.0, _num(process.get("brim_object_gap"), 0.0))


def clearance(machine: dict, process: dict | None = None) -> dict:
    """Toolhead clearance for printing by object (OrcaSlicer's settings):
    radius around each object, and the height the gantry (rod) and lid allow."""
    def get(key, default):
        for src in (process or {}, machine):
            if key in src:
                return _num(src[key], default)
        return default
    return {"radius": get("extruder_clearance_radius", 45.0),
            "rod": get("extruder_clearance_height_to_rod", 40.0),
            "lid": get("extruder_clearance_height_to_lid", 120.0)}
