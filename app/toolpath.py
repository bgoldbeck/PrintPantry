"""Extrusion toolpaths of a sliced file, for the build plate preview in the print dialog.

Reads plain G-code or the plate G-code inside a sliced .gcode.3mf and returns a
compact binary the browser draws as coloured lines:

    uint32 magic "PPTP", float32 bed_x0, bed_y0, bed_x1, bed_y1, uint32 tools
    per tool: uint32 filament index, uint32 segments, int16[segments * 6]
              (x0, y0, z0, x1, y1, z1 in 1/20 mm)

Tool numbers are the file's filament numbers (the T commands), so the page can
colour each with the toolhead it is mapped to.
"""
from __future__ import annotations

import io
import math
import re
import struct
import zipfile
from pathlib import Path

VERSION = 2  # bump when parsing changes, so cached toolpaths are made again
SCALE = 20  # 1/20 mm per int16 step: +-1638 mm range
MAX_SEGMENTS = 400_000
MIN_LEN = 0.6  # mm; shorter continuous pieces are merged into one line

_AREA = re.compile(r"^;\s*(?:printable_area|bed_shape)\s*=\s*(.+)$", re.M)


def _open_text(path: Path, plate: int = 1) -> io.TextIOBase:
    if path.name.lower().endswith(".3mf"):
        z = zipfile.ZipFile(path)
        names = [n for n in z.namelist() if re.fullmatch(r"Metadata/plate_\d+\.gcode", n)]
        if not names:
            raise ValueError("no G-code in this 3MF")
        name = f"Metadata/plate_{plate}.gcode"
        if name not in names:
            name = sorted(names)[0]
        return io.TextIOWrapper(z.open(name), encoding="utf-8", errors="replace")
    return open(path, encoding="utf-8", errors="replace")


def _bed(text: str) -> tuple[float, float, float, float] | None:
    m = _AREA.search(text)
    if not m:
        return None
    try:
        pts = [tuple(float(v) for v in p.split("x")) for p in m.group(1).split(",")]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return min(xs), min(ys), max(xs), max(ys)
    except (ValueError, IndexError):
        return None


def _words(line: str) -> dict[str, float]:
    out = {}
    for w in line.split()[1:]:
        if len(w) > 1 and w[0] in "XYZEIJR":
            try:
                out[w[0]] = float(w[1:])
            except ValueError:
                pass
    return out


def parse(path: Path, plate: int = 1) -> tuple[tuple[float, float, float, float] | None, dict[int, list]]:
    """Return (bed rectangle or None, {tool: [x0, y0, z0, x1, y1, z1, ...]})."""
    segs: dict[int, list] = {}
    tool = 0
    x = y = z = e = 0.0
    abs_xyz, abs_e = True, False
    header: list[str] = []
    custom = False
    # current open polyline (merged short pieces): start point and tool
    sx = sy = sz = None

    def flush_to(nx, ny):
        nonlocal sx, sy, sz
        if sx is not None and (sx != nx or sy != ny):
            segs.setdefault(tool, []).extend((sx, sy, sz, nx, ny, z))
        sx = sy = sz = None

    with _open_text(path, plate) as fh:
        for raw in fh:
            if raw[0] == ";":
                if len(header) < 4000:
                    header.append(raw)
                if raw.startswith(";TYPE:"):
                    custom = raw[6:].strip() == "Custom"  # start/end G-code purge lines
                continue
            line = raw.split(";", 1)[0].strip()
            if not line:
                continue
            cmd = line.split(None, 1)[0].upper()
            if cmd in ("G0", "G1", "G2", "G3"):
                w = _words(line)
                nx = w.get("X", None if abs_xyz else 0.0)
                ny = w.get("Y", None if abs_xyz else 0.0)
                nx = (nx if abs_xyz else x + nx) if nx is not None else x
                ny = (ny if abs_xyz else y + ny) if ny is not None else y
                nz = w.get("Z")
                if nz is not None:
                    nz = nz if abs_xyz else z + nz
                de = 0.0
                if "E" in w:
                    de = w["E"] if not abs_e else w["E"] - e
                    e = w["E"] if abs_e else e + w["E"]
                if nz is not None and nz != z:
                    if sx is not None:
                        flush_to(x, y)
                    z = nz
                if de > 0 and not custom and (nx != x or ny != y):
                    pts = [(nx, ny)]
                    if cmd in ("G2", "G3") and ("I" in w or "J" in w):
                        pts = _arc(x, y, nx, ny, w.get("I", 0.0), w.get("J", 0.0), cmd == "G2")
                    for px, py in pts:
                        if sx is None:
                            sx, sy, sz = x, y, z
                        # keep merging while the piece is short and roughly straight
                        if math.hypot(px - sx, py - sy) >= MIN_LEN:
                            segs.setdefault(tool, []).extend((sx, sy, z, px, py, z))
                            sx, sy, sz = px, py, z
                        x, y = px, py
                else:
                    if sx is not None:
                        flush_to(x, y)
                x, y = nx, ny
            elif cmd == "G90":
                abs_xyz = True
            elif cmd == "G91":
                abs_xyz = False
            elif cmd == "M82":
                abs_e = True
            elif cmd == "M83":
                abs_e = False
            elif cmd == "G92":
                w = _words(line)
                if "E" in w:
                    e = w["E"]
            elif cmd == "M620" and (m := re.match(r"\s*M620\s+S(\d+)A", line)) and int(m.group(1)) < 255:
                # Bambu: "M620 S<n>A" loads filament n from the AMS; the T
                # that follows can be the T1000 macro instead of T<n>.
                if sx is not None:
                    flush_to(x, y)
                tool = int(m.group(1))
            elif cmd[0] == "T" and cmd[1:].isdigit() and int(cmd[1:]) < 255:  # T255 unload, T1000 macro
                if sx is not None:
                    flush_to(x, y)
                tool = int(cmd[1:])
        if sx is not None:
            flush_to(x, y)
    bed = _bed("".join(header))  # Orca/Bambu: may instead be in the settings block at the end
    return bed, segs


def _arc(x0, y0, x1, y1, i, j, cw):
    cx, cy = x0 + i, y0 + j
    r = math.hypot(i, j)
    a0 = math.atan2(y0 - cy, x0 - cx)
    a1 = math.atan2(y1 - cy, x1 - cx)
    if cw:
        if a1 >= a0:
            a1 -= 2 * math.pi
    elif a1 <= a0:
        a1 += 2 * math.pi
    n = max(1, min(32, int(abs(a1 - a0) * r / 1.0)))
    return [(cx + r * math.cos(a0 + (a1 - a0) * k / n), cy + r * math.sin(a0 + (a1 - a0) * k / n)) for k in range(1, n)] + [(x1, y1)]


def _tail_bed(path: Path) -> tuple[float, float, float, float] | None:
    try:
        if path.name.lower().endswith(".3mf"):
            return None
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 400_000))
            return _bed(fh.read().decode("utf-8", "replace"))
    except OSError:
        return None


def encode(path: Path, plate: int = 1) -> bytes:
    bed, segs = parse(path, plate)
    bed = bed or _tail_bed(path)
    total = sum(len(v) // 6 for v in segs.values())
    step = max(1, math.ceil(total / MAX_SEGMENTS))
    if bed is None:
        xs = [v for s in segs.values() for v in (s[0::3])]
        ys = [v for s in segs.values() for v in (s[1::3])]
        bed = (min(xs, default=0), min(ys, default=0), max(xs, default=200), max(ys, default=200))
    out = io.BytesIO()
    out.write(b"PPTP")
    out.write(struct.pack("<4fI", *bed, len(segs)))
    for t in sorted(segs):
        vals = segs[t]
        if step > 1:  # too much to draw: keep every n-th line
            vals = [v for k in range(0, len(vals), 6 * step) for v in vals[k:k + 6]]
        n = len(vals) // 6
        out.write(struct.pack("<II", t, n))
        clamp = lambda v: max(-32768, min(32767, round(v * SCALE)))  # noqa: E731
        out.write(struct.pack(f"<{n * 6}h", *(clamp(v) for v in vals)))
    return out.getvalue()
