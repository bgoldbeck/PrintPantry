"""Print orientation: saved rotations per STL and auto-orient (slicer/orient.py)."""

import math
import sys
from array import array
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "slicer"))

import objects as O  # noqa: E402
import orient as OR  # noqa: E402
import service  # noqa: E402
from test_objects import write_stl  # noqa: E402


def box(x0, y0, z0, x1, y1, z1):
    v = [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
    faces = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
             (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]
    return [[v[a], v[b], v[c]] for a, b, c in faces]


def ring_on_side():
    """A 40 x 30 rectangular ring 3 mm thick, standing on its long edge (ring
    plane upright), like a spacer exported the wrong way up."""
    flat = box(0, 0, 0, 40, 5, 3) + box(0, 25, 0, 40, 30, 3) + box(0, 5, 0, 5, 25, 3) + box(35, 5, 0, 40, 25, 3)
    return [[(x, -z, y) for x, y, z in t] for t in flat]  # a quarter turn about X: stands up


def flat(tris):
    return array("f", (c for t in tris for p in t for c in p))


def test_auto_lays_ring_flat():
    m = OR.auto_matrix(flat(ring_on_side()))
    out = OR.rotate(flat(ring_on_side()), m)
    lo, hi = O.bbox(out)
    assert lo[2] == pytest.approx(0, abs=1e-4)
    assert hi[2] - lo[2] == pytest.approx(3, abs=1e-3)  # lies flat: 3 mm tall


def test_auto_keeps_a_cube_as_it_is():
    assert OR.auto_matrix(flat(box(0, 0, 0, 10, 10, 10))) == OR.IDENTITY3


def test_down_to_bed_turns_vector_down():
    for d in [(1, 0, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1), (0.6, 0, 0.8)]:
        m = OR.down_to_bed(d)
        v = [sum(m[r * 3 + k] * d[k] for k in range(3)) for r in range(3)]
        assert v == pytest.approx([0, 0, -1], abs=1e-9)
        assert OR.clean_matrix(m) is None or len(OR.clean_matrix(m)) == 9


def test_clean_matrix():
    assert OR.clean_matrix(None) is None
    assert OR.clean_matrix(list(OR.IDENTITY3)) is None
    rx = [1, 0, 0, 0, 0, -1, 0, 1, 0]
    assert OR.clean_matrix(rx) == tuple(float(v) for v in rx)
    with pytest.raises(OR.OrientError):
        OR.clean_matrix([1, 2, 3])
    with pytest.raises(OR.OrientError):
        OR.clean_matrix([2, 0, 0, 0, 1, 0, 0, 0, 1])
    with pytest.raises(OR.OrientError):
        OR.clean_matrix([-1, 0, 0, 0, 1, 0, 0, 0, 1])  # mirror
    with pytest.raises(OR.OrientError):
        OR.clean_matrix([math.nan] * 9)


def test_saved_wins_over_auto_and_copies_are_cached(tmp_path):
    a = write_stl(tmp_path / "lib" / "a.stl", ring_on_side())
    cache = tmp_path / "cache"
    turn = (0.0, 0.0, 1.0, 0.0, 1.0, 0.0, -1.0, 0.0, 0.0)  # 90 degrees about Y: on its short end
    [p] = OR.oriented_paths([a], {a: turn}, True, cache)
    assert p.name == "a.stl" and p != a
    lo, hi = O.bbox(O.read_stl(p))
    assert hi[2] - lo[2] == pytest.approx(40, abs=1e-3)  # the saved turn, not auto's flat 3 mm
    assert OR.oriented_paths([a], {a: turn}, True, cache) == [p]
    [q] = OR.oriented_paths([a], {}, True, cache)
    lo, hi = O.bbox(O.read_stl(q))
    assert hi[2] - lo[2] == pytest.approx(3, abs=1e-3)
    assert OR.oriented_paths([a], {}, False, cache) == [a]


def test_parts_of_one_model_turn_together(tmp_path):
    base = ring_on_side()
    a = write_stl(tmp_path / "a.stl", base)
    b = write_stl(tmp_path / "b.stl", box(10, -3, 10, 20, 0, 20))  # sits on the ring's face
    plan = OR.plan([a, b], {}, True)
    assert plan[0] == plan[1] and plan[0] is not None


def test_slice_dialog_objects_use_orientation(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    write_stl(lib / "Spacer" / "spacer.stl", ring_on_side())
    monkeypatch.setattr(service, "LIBRARY", lib)
    monkeypatch.setattr(service, "ORIENTED", tmp_path / "oriented")
    plain = service.list_objects({"paths": ["Spacer/spacer.stl"]})
    auto = service.list_objects({"paths": ["Spacer/spacer.stl"], "auto_orient": True})
    assert plain["items"][0]["size"][2] == pytest.approx(30, abs=0.1)
    assert auto["items"][0]["size"][2] == pytest.approx(3, abs=0.1)
    with pytest.raises(service.BadRequest):
        service.clean_orient({"Spacer/spacer.stl": [1, 2]})
    assert service.clean_orient({"Spacer/spacer.stl": list(OR.IDENTITY3)}) == {}
