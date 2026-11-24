"""Copies spread over the bed (slicer/layout.py), and placed in the 3MF."""
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "slicer"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import layout as L  # noqa: E402
import objects as O  # noqa: E402
from test_objects import cube, write_stl  # noqa: E402

BED = (270, 270)


def boxes(spots):
    return [(p["x"] - p["w"] / 2, p["y"] - p["d"] / 2, p["x"] + p["w"] / 2, p["y"] + p["d"] / 2) for p in spots]


def min_gap(spots):
    """Smallest distance between two pieces' boxes (along the axis that separates them)."""
    bs, best = boxes(spots), float("inf")
    for i, a in enumerate(bs):
        for b in bs[i + 1:]:
            best = min(best, max(b[0] - a[2], a[0] - b[2], b[1] - a[3], a[1] - b[3]))
    return best


def test_two_copies_spread_over_the_bed():
    spots = L.spread([{"g": 0, "w": 40, "d": 20}], 2, BED)
    # Not bunched in the middle: the spare room is shared out evenly.
    assert min_gap(spots) > 70


def test_copies_stay_on_the_bed_and_apart():
    for n in (1, 3, 7, 20, 45):
        spots = L.spread([{"g": 0, "w": 40, "d": 20}], n, BED)
        assert len(spots) == n
        for x0, y0, x1, y1 in boxes(spots):
            assert -135 + 3 - 1e-6 <= x0 and x1 <= 135 - 3 + 1e-6 and -135 + 3 - 1e-6 <= y0 and y1 <= 135 - 3 + 1e-6
        if n > 1:
            assert min_gap(spots) >= L.GAP - 1e-6


def test_max_fills_the_bed_and_by_object_fits_fewer():
    rect = [{"g": 0, "w": 40, "d": 20}]
    most = L.max_copies(rect, BED)
    assert most >= 40 and L.spread(rect, most + 1, BED) is None
    by_obj = L.max_copies(rect, BED, gap=58)
    assert 1 < by_obj < most
    assert min_gap(L.spread(rect, by_obj, BED, gap=58)) >= 58 - 1e-6


def test_brim_and_clearance_from_presets():
    assert L.brim({"brim_type": "outer_only", "brim_width": "5", "brim_object_gap": "0.1"}) == 5.1
    assert L.brim({"brim_type": "no_brim", "brim_width": "5"}) == 0
    assert L.clearance({"extruder_clearance_radius": ["57"], "extruder_clearance_height_to_rod": "36"})["radius"] == 57
    assert L.bed_of({"printable_area": ["0x0", "270x0", "270x270", "0x270"]}) == (0, 0, 270, 270)


def test_plate_places_copies_when_given_a_bed(tmp_path):
    stl = write_stl(tmp_path / "s.stl", cube(s=20))
    out = tmp_path / "c.3mf"
    assert O.plate_3mf([stl], {}, out, copies=4, bed={"area": (0, 0, 270, 270), "gap": 8, "margin": 5})
    model = zipfile.ZipFile(out).read("3D/3dmodel.model").decode()
    spots = [tuple(map(float, m)) for m in re.findall(r'transform="1 0 0 0 1 0 0 0 1 ([\d.]+) ([\d.]+) 0"', model)]
    assert len(spots) == 4
    assert all(15 <= x <= 255 and 15 <= y <= 255 for x, y in spots)
    xs, ys = sorted({round(x) for x, _ in spots}), sorted({round(y) for _, y in spots})
    assert len(xs) == 2 and len(ys) == 2 and xs[1] - xs[0] > 80  # a 2 x 2 grid over the bed
    # Too many to fit: left to the slicer's arrange.
    assert not O.plate_3mf([stl], {}, out, copies=200, bed={"area": (0, 0, 270, 270), "gap": 58})
