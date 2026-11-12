#!/usr/bin/env python3
"""Create a small fake library for trying Print Pantry without real files.

    python scripts/make_demo_library.py ./demo-library

The files mimic what Bambu Studio / OrcaSlicer / Snapmaker Orca write closely
enough for the scanner: binary STLs, project 3MFs, sliced P1S .gcode.3mf files
with several plates and thumbnails, and U1 .gcode files with a thumbnail block.
"""

from __future__ import annotations

import base64
import json
import math
import struct
import sys
import zipfile
import zlib
from pathlib import Path

# ------------------------------------------------------------------ meshes


def _box(cx, cy, cz, sx, sy, sz):
    x0, x1 = cx - sx / 2, cx + sx / 2
    y0, y1 = cy - sy / 2, cy + sy / 2
    z0, z1 = cz, cz + sz
    v = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
         (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    quads = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    tris = []
    for a, b, c, d in quads:
        tris += [(v[a], v[b], v[c]), (v[a], v[c], v[d])]
    return tris


def _cylinder(cx, cy, z0, r, h, n=48):
    tris = []
    for i in range(n):
        a0, a1 = 2 * math.pi * i / n, 2 * math.pi * (i + 1) / n
        p0 = (cx + r * math.cos(a0), cy + r * math.sin(a0))
        p1 = (cx + r * math.cos(a1), cy + r * math.sin(a1))
        b0, b1 = (*p0, z0), (*p1, z0)
        t0, t1 = (*p0, z0 + h), (*p1, z0 + h)
        tris += [(b0, b1, t1), (b0, t1, t0)]
        tris += [((cx, cy, z0), b1, b0), ((cx, cy, z0 + h), t0, t1)]
    return tris


def _torus(R, r, n=48, m=20, z=0.0):
    def pt(u, v):
        return ((R + r * math.cos(v)) * math.cos(u), (R + r * math.cos(v)) * math.sin(u), z + r + r * math.sin(v))
    tris = []
    for i in range(n):
        for j in range(m):
            u0, u1 = 2 * math.pi * i / n, 2 * math.pi * (i + 1) / n
            v0, v1 = 2 * math.pi * j / m, 2 * math.pi * (j + 1) / m
            a, b, c, d = pt(u0, v0), pt(u1, v0), pt(u1, v1), pt(u0, v1)
            tris += [(a, b, c), (a, c, d)]
    return tris


def write_stl(path: Path, tris) -> None:
    def normal(t):
        (ax, ay, az), (bx, by, bz), (cx, cy, cz) = t
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        ln = math.sqrt(nx * nx + ny * ny + nz * nz) or 1
        return nx / ln, ny / ln, nz / ln
    out = bytearray(b"Print Pantry demo".ljust(80, b" "))
    out += struct.pack("<I", len(tris))
    for t in tris:
        out += struct.pack("<3f", *normal(t))
        for p in t:
            out += struct.pack("<3f", *p)
        out += b"\0\0"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))


SHAPES = {
    "wrench": lambda: _box(0, 0, 0, 120, 18, 6) + _cylinder(-60, 0, 0, 16, 6) + _cylinder(60, 0, 0, 13, 6),
    "bin": lambda: _box(0, 0, 0, 84, 42, 3) + _box(0, -19.5, 3, 84, 3, 40) + _box(0, 19.5, 3, 84, 3, 40)
    + _box(-40.5, 0, 3, 3, 42, 40) + _box(40.5, 0, 3, 3, 42, 40),
    "ring": lambda: _torus(30, 6),
    "pot": lambda: _cylinder(0, 0, 0, 40, 70) + _cylinder(0, 0, 70, 44, 6),
    "dice": lambda: _box(0, 0, 0, 20, 20, 20),
    "hook": lambda: _box(0, 0, 0, 30, 10, 60) + _box(0, 12, 0, 30, 24, 8) + _box(0, 22, 0, 30, 6, 25),
    "cube": lambda: _box(0, 0, 0, 20, 20, 20),
}

# ------------------------------------------------------------------ images


def png(width: int, height: int, rgb: tuple[int, int, int], shape: str = "disc") -> bytes:
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            dx, dy = (x - width / 2) / (width / 2), (y - height / 2) / (height / 2)
            inside = dx * dx + dy * dy < 0.45 if shape == "disc" else abs(dx) < 0.6 and abs(dy) < 0.35
            if inside:
                shade = 0.75 + 0.25 * (1 - dy)
                row += bytes(min(255, int(c * shade)) for c in rgb)
            else:
                row += bytes((236, 236, 236))
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


def _hex(rgb):
    return "#%02X%02X%02X" % rgb


# ------------------------------------------------------------------ 3MF / gcode

MODEL_XML = """<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
 <metadata name="Application">BambuStudio-02.03.00.70</metadata>
 <resources><object id="1" type="model"><mesh><vertices>
  <vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/><vertex x="0" y="10" z="0"/><vertex x="0" y="0" z="10"/>
 </vertices><triangles>
  <triangle v1="0" v2="2" v3="1"/><triangle v1="0" v2="1" v3="3"/><triangle v1="1" v2="2" v3="3"/><triangle v1="0" v2="3" v3="2"/>
 </triangles></mesh></object></resources>
 <build><item objectid="1"/></build>
</model>
"""


def _model_settings(objects: list[str]) -> str:
    objs = "".join(
        f'<object id="{i + 1}"><metadata key="name" value="{o}"/>'
        f'<part id="1" subtype="normal_part"><metadata key="source_file" value="C:\\Users\\me\\Downloads\\{o}"/></part></object>'
        for i, o in enumerate(objects)
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><config>{objs}</config>'


def write_project_3mf(path: Path, objects: list[str], rgb, plates: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("3D/3dmodel.model", MODEL_XML)
        zf.writestr("Metadata/model_settings.config", _model_settings(objects))
        zf.writestr("Metadata/project_settings.config", json.dumps({"printer_model": "Bambu Lab P1S"}))
        for i in range(1, plates + 1):
            zf.writestr(f"Metadata/plate_{i}.png", png(128, 128, rgb))


def write_sliced_3mf(path: Path, objects: list[str], plates: list[dict], model="Bambu Lab P1S", model_id="C12") -> None:
    """plates: [{"seconds": int, "filaments": [(type, rgb, grams)], "objects": [...]}]"""
    path.parent.mkdir(parents=True, exist_ok=True)
    slice_plates = []
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("3D/3dmodel.model", MODEL_XML)
        zf.writestr("Metadata/model_settings.config", _model_settings(objects))
        zf.writestr(
            "Metadata/project_settings.config",
            json.dumps({"printer_model": model, "printer_settings_id": f"{model} 0.4 nozzle"}),
        )
        for i, p in enumerate(plates, start=1):
            rgb = p["filaments"][0][1]
            zf.writestr(f"Metadata/plate_{i}.gcode", f"; HEADER_BLOCK_START\n; plate {i}\nG28\n")
            zf.writestr(f"Metadata/plate_{i}.gcode.md5", "0" * 32)
            zf.writestr(f"Metadata/plate_{i}.png", png(160, 160, rgb))
            zf.writestr(f"Metadata/plate_{i}_small.png", png(32, 32, rgb))
            fil = "".join(
                f'<filament id="{n + 1}" tray_info_idx="GFA00" type="{t}" color="{_hex(c)}FF" used_m="{g / 3:.2f}" used_g="{g:.2f}"/>'
                for n, (t, c, g) in enumerate(p["filaments"])
            )
            objs = "".join(
                f'<object identify_id="{100 + k}" name="{o}" skipped="false"/>' for k, o in enumerate(p.get("objects", objects))
            )
            weight = sum(g for _, _, g in p["filaments"])
            slice_plates.append(
                f'<plate><metadata key="index" value="{i}"/><metadata key="printer_model_id" value="{model_id}"/>'
                f'<metadata key="nozzle_diameters" value="0.4"/><metadata key="prediction" value="{p["seconds"]}"/>'
                f'<metadata key="weight" value="{weight:.2f}"/>{objs}{fil}</plate>'
            )
        zf.writestr(
            "Metadata/slice_info.config",
            '<?xml version="1.0" encoding="UTF-8"?><config><header>'
            '<header_item key="X-BBL-Client-Type" value="slicer"/>'
            '<header_item key="X-BBL-Client-Version" value="02.03.00.70"/></header>'
            + "".join(slice_plates) + "</config>",
        )


def write_u1_gcode(path: Path, objects: list[str], seconds: int, filaments: list[tuple[str, tuple, float]]) -> None:
    rgb = filaments[0][1]
    thumb = base64.b64encode(png(96, 96, rgb, "rect")).decode()
    lines = [f"; {thumb[i:i + 78]}" for i in range(0, len(thumb), 78)]
    h, m = divmod(seconds // 60, 60)
    types = ";".join([t for t, _, _ in filaments] + ["PLA"] * (4 - len(filaments)))
    colors = ";".join([_hex(c) for _, c, _ in filaments] + ["#FFFFFF"] * (4 - len(filaments)))
    grams = ", ".join([f"{g:.2f}" for _, _, g in filaments] + ["0.00"] * (4 - len(filaments)))
    mm = ", ".join([f"{g * 330:.2f}" for _, _, g in filaments] + ["0.00"] * (4 - len(filaments)))
    body = "\n".join(
        [
            "; generated by Snapmaker Orca 2.1.0 on 2026-11-10 at 18:22:01",
            ";",
            "; THUMBNAIL_BLOCK_START",
            f"; thumbnail begin 96x96 {len(thumb)}",
            *lines,
            "; thumbnail end",
            "; THUMBNAIL_BLOCK_END",
            "",
            f"; model printing time: {h}h {m}m 0s; total estimated time: {h}h {m + 3}m 0s",
            *[f"EXCLUDE_OBJECT_DEFINE NAME={o.replace(' ', '_')}" for o in objects],
            "G28",
            "G1 X10 Y10 Z0.2 F3000",
            "",
            f"; filament used [mm] = {mm}",
            f"; filament used [g] = {grams}",
            f"; total filament used [g] = {sum(g for _, _, g in filaments):.2f}",
            f"; estimated printing time (normal mode) = {h}h {m}m 0s",
            "; CONFIG_BLOCK_START",
            f"; filament_colour = {colors}",
            f"; filament_type = {types}",
            '; printer_model = Snapmaker U1',
            '; printer_settings_id = Snapmaker U1 (0.4 nozzle)',
            "; CONFIG_BLOCK_END",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


# ------------------------------------------------------------------ demo set

RED, GREEN, BLUE, ORANGE, WHITE, BLACK = (220, 60, 50), (60, 170, 80), (50, 110, 210), (240, 140, 30), (240, 240, 240), (40, 40, 40)


def make_demo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    tools = root / "Tools"
    write_stl(tools / "Wrench Holder" / "wrench_holder.stl", SHAPES["wrench"]())
    write_project_3mf(tools / "Wrench Holder" / "wrench_holder.3mf", ["wrench_holder.stl"], ORANGE)
    write_sliced_3mf(
        tools / "Wrench Holder" / "wrench_holder_P1S_PLA.gcode.3mf",
        ["wrench_holder.stl"],
        [
            {"seconds": 5400, "filaments": [("PLA", ORANGE, 42.5)]},
            {"seconds": 2700, "filaments": [("PLA", BLACK, 18.2), ("PLA", WHITE, 2.1)]},
        ],
    )
    write_u1_gcode(tools / "Wrench Holder" / "wrench_holder_U1.gcode", ["wrench_holder.stl"], 6100, [("PLA", ORANGE, 41.0)])
    (tools / "Wrench Holder" / "images").mkdir(exist_ok=True)

    write_stl(tools / "Wall Hook" / "files" / "hook.stl", SHAPES["hook"]())
    write_sliced_3mf(tools / "Wall Hook" / "hook_x4.gcode.3mf", ["hook.stl"], [{"seconds": 3900, "filaments": [("PETG", BLUE, 30.0)]}])

    org = root / "Organizers"
    write_stl(org / "Gridfinity Bin 2x1" / "bin_2x1.stl", SHAPES["bin"]())
    write_stl(org / "Gridfinity Bin 2x1" / "bin_2x1_lid.stl", SHAPES["dice"]())
    write_u1_gcode(
        org / "Gridfinity Bin 2x1" / "bin_2x1_U1_4color.gcode",
        ["bin_2x1.stl"],
        8000,
        [("PLA", RED, 20.0), ("PLA", GREEN, 18.0), ("PLA", BLUE, 15.5), ("PLA", WHITE, 3.0)],
    )

    garden = root / "Garden"
    write_stl(garden / "planter.stl", SHAPES["pot"]())
    write_sliced_3mf(garden / "planter_P1S.gcode.3mf", ["planter.stl"], [{"seconds": 14400, "filaments": [("PETG", GREEN, 120.0)]}])
    write_u1_gcode(garden / "planter_U1.gcode", ["planter.stl"], 15000, [("PETG", GREEN, 118.0)])
    write_stl(garden / "plant_label.stl", SHAPES["cube"]())

    toys = root / "Toys & Games"
    write_stl(toys / "Fidget Ring" / "ring.stl", SHAPES["ring"]())
    write_project_3mf(toys / "Fidget Ring" / "ring.3mf", ["ring.stl"], RED)
    write_stl(toys / "Dice Tower" / "tower.stl", SHAPES["dice"]())

    (root / "Printer Upgrades").mkdir(exist_ok=True)
    write_stl(root / "Printer Upgrades" / "Calibration Cube" / "xyz_cube.stl", SHAPES["cube"]())
    write_sliced_3mf(
        root / "Printer Upgrades" / "Calibration Cube" / "xyz_cube.gcode.3mf",
        ["xyz_cube.stl"],
        [{"seconds": 900, "filaments": [("PLA", WHITE, 6.0)]}],
    )
    write_stl(root / "loose_part.stl", SHAPES["cube"]())
    return root


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "demo-library")
    make_demo(target)
    print(f"Demo library written to {target.resolve()}")
