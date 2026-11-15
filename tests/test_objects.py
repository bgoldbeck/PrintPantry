"""Per-object filaments: listing a 3MF's objects / an STL's bodies, writing the
chosen toolheads into the 3MF the slicer reads, and the routes around it."""

import struct
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "slicer"))

import objects as O  # noqa: E402
import service  # noqa: E402


def cube(x=0.0, y=0.0, z=0.0, s=10.0):
    """12 triangles of a cube at (x, y, z)."""
    v = [(x + dx * s, y + dy * s, z + dz * s) for dx in (0, 1) for dy in (0, 1) for dz in (0, 1)]
    faces = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
             (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]
    return [[v[a], v[b], v[c]] for a, b, c in faces]


def write_stl(path: Path, tris) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = bytearray(80) + struct.pack("<I", len(tris))
    for t in tris:
        data += struct.pack("<3f", 0, 0, 0)
        for p in t:
            data += struct.pack("<3f", *p)
        data += b"\0\0"
    path.write_bytes(bytes(data))
    return path


def bambu_3mf(path: Path, parts_per_object=(1, 2), painted=False) -> Path:
    """Bambu-style project: objects in a sub-model file, config with parts and plates."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sub, top, cfg, items = [], [], [], []
    pid = 1
    oid = 100
    for o, nparts in enumerate(parts_per_object):
        comps, parts = [], []
        for k in range(nparts):
            tris = cube(x=o * 30 + k * 12)
            verts = [p for t in tris for p in t]
            paint = ' paint_color="4"' if painted and o == 0 else ""
            sub.append(f'<object id="{pid}" type="model"><mesh><vertices>'
                       + "".join(f'<vertex x="{a}" y="{b}" z="{c}"/>' for a, b, c in verts)
                       + "</vertices><triangles>"
                       + "".join(f'<triangle v1="{i*3}" v2="{i*3+1}" v3="{i*3+2}"{paint}/>' for i in range(len(tris)))
                       + "</triangles></mesh></object>")
            comps.append(f'<component p:path="/3D/Objects/object_1.model" objectid="{pid}" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>')
            parts.append(f'<part id="{pid}" subtype="normal_part"><metadata key="name" value="P{pid}"/>'
                         f'<metadata key="extruder" value="{k + 1}"/></part>')
            pid += 1
        top.append(f'<object id="{oid}" type="model"><components>{"".join(comps)}</components></object>')
        cfg.append(f'<object id="{oid}"><metadata key="name" value="Obj{o}"/><metadata key="extruder" value="1"/>{"".join(parts)}</object>')
        items.append(f'<item objectid="{oid}" transform="1 0 0 0 1 0 0 0 1 100 100 0" printable="1"/>')
        oid += 1
    ns = ('xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
          'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06"')
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("3D/3dmodel.model", f'<model {ns}><resources>{"".join(top)}</resources><build>{"".join(items)}</build></model>')
        zf.writestr("3D/Objects/object_1.model", f'<model {ns}><resources>{"".join(sub)}</resources></model>')
        zf.writestr("Metadata/model_settings.config",
                    f'<config>{"".join(cfg)}<plate><metadata key="plater_id" value="1"/>'
                    '<model_instance><metadata key="object_id" value="101"/></model_instance></plate></config>')
    return path


def test_3mf_objects_and_parts(tmp_path):
    items = O.threemf_items(bambu_3mf(tmp_path / "a.3mf"), with_mesh=True)
    assert [i.key for i in items] == ["o100", "o101:2", "o101:3"]
    assert [i.extruder for i in items] == [1, 1, 2]
    assert items[0].name == "Obj0" and items[1].name == "Obj1 · P2"
    assert items[0].triangles == 12 and items[0].size == pytest.approx([10, 10, 10])
    assert items[1].plate == 1
    lo, _ = O.bbox(items[0].tris)
    assert lo == pytest.approx([100, 100, 0])  # build item transform applied


def test_painted_objects_are_flagged(tmp_path):
    items = O.threemf_items(bambu_3mf(tmp_path / "a.3mf", painted=True))
    assert all(i.painted for i in items)  # same sub-model file


def config_of(path: Path) -> ET.Element:
    with zipfile.ZipFile(path) as zf:
        return ET.fromstring(zf.read(O.MODEL_SETTINGS))


def extruders(root):
    out = {}
    for o in root.findall("object"):
        out[o.get("id")] = O._meta(o, "extruder")
        for p in o.findall("part"):
            out[o.get("id") + ":" + p.get("id")] = O._meta(p, "extruder")
    return out


def test_assign_writes_object_and_part_extruders(tmp_path):
    src = bambu_3mf(tmp_path / "a.3mf")
    dst = tmp_path / "b.3mf"
    O.assign_3mf(src, dst, {"o100": 3, "o101:3": 4})
    ex = extruders(config_of(dst))
    assert ex["100"] == "3" and ex["100:1"] == "3"  # the part follows its object
    assert ex["101:2"] == "1" and ex["101:3"] == "4"
    assert [i.extruder for i in O.threemf_items(dst)] == [3, 1, 4]
    with zipfile.ZipFile(dst) as zf:  # everything else is kept
        assert "3D/Objects/object_1.model" in zf.namelist()


def test_assign_on_a_plain_3mf_creates_settings(tmp_path):
    src = tmp_path / "plain.3mf"
    tris = cube()
    with zipfile.ZipFile(src, "w") as zf:
        verts = [p for t in tris for p in t]
        zf.writestr("3D/3dmodel.model",
                    '<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><resources>'
                    '<object id="1" type="model"><mesh><vertices>'
                    + "".join(f'<vertex x="{a}" y="{b}" z="{c}"/>' for a, b, c in verts)
                    + "</vertices><triangles>"
                    + "".join(f'<triangle v1="{i*3}" v2="{i*3+1}" v3="{i*3+2}"/>' for i in range(12))
                    + '</triangles></mesh></object></resources><build><item objectid="1"/></build></model>')
    assert [i.key for i in O.threemf_items(src)] == ["o1"]
    O.assign_3mf(src, tmp_path / "out.3mf", {"o1": 2})
    assert extruders(config_of(tmp_path / "out.3mf")) == {"1": "2"}


def test_stl_bodies_are_split(tmp_path):
    p = write_stl(tmp_path / "two.stl", cube() + cube(x=20, s=5))
    items = O.stl_items([p])
    assert [i.key for i in items] == ["f0b0", "f0b1"]
    assert [i.name for i in items] == ["Body 1", "Body 2"]
    assert [i.extruder for i in items] == [1, 2]
    assert items[1].size == pytest.approx([5, 5, 5])


def test_ascii_stl(tmp_path):
    p = tmp_path / "a.stl"
    lines = ["solid a"]
    for t in cube():
        lines += ["facet normal 0 0 0", "outer loop"] + [f"vertex {a} {b} {c}" for a, b, c in t] + ["endloop", "endfacet"]
    p.write_text("\n".join(lines + ["endsolid a"]))
    assert len(O.read_stl(p)) == 12 * 9


def test_stls_to_3mf_keeps_positions_and_filaments(tmp_path):
    a = write_stl(tmp_path / "base.stl", cube(s=20))
    b = write_stl(tmp_path / "letters.stl", cube(x=5, y=5, z=20, s=2) + cube(x=12, y=5, z=20, s=2))
    assert O.overlapping([a, write_stl(tmp_path / "in.stl", cube(x=5, y=5, z=5, s=2))])
    assert O.overlapping([a, b])  # letters sit on the base
    assert not O.overlapping([a, write_stl(tmp_path / "far.stl", cube(x=50))])
    out = tmp_path / "out.3mf"
    O.stls_to_3mf([a, b], {"f0": 1, "f1b0": 2, "f1b1": 3}, out, keep_positions=True)
    items = O.threemf_items(out, with_mesh=True)
    assert len(items) == 3 and {i.extruder for i in items} == {1, 2, 3}
    # One object: relative positions kept (letters sit on top of the base).
    assert len({i.key.split(":")[0] for i in items}) == 1
    zs = sorted(O.bbox(i.tris)[0][2] for i in items)
    assert zs == pytest.approx([0, 20, 20])
    O.stls_to_3mf([a, b], {"f0": 1, "f1b0": 2, "f1b1": 2}, out, keep_positions=False)
    keys = [i.key for i in O.threemf_items(out)]
    assert len({k.split(":")[0] for k in keys}) == 2


def test_mesh_blob_layout(tmp_path):
    p = write_stl(tmp_path / "two.stl", cube() + cube(x=20))
    blob = O.mesh_blob(O.items_for([p]))
    count = struct.unpack_from("<I", blob)[0]
    assert count == 2
    n0 = struct.unpack_from("<I", blob, 4)[0]
    assert n0 == 12
    assert len(blob) == 4 + 2 * (4 + 12 * 36)


# ---------------------------------------------------------------- service

@pytest.fixture()
def lib(monkeypatch, tmp_path):
    library = tmp_path / "library"
    monkeypatch.setattr(service, "LIBRARY", library)
    return library


def test_list_objects_route(lib):
    write_stl(lib / "M" / "a.stl", cube(s=20))
    write_stl(lib / "M" / "b.stl", cube(x=5, y=5, z=5, s=2))
    res = service.list_objects({"paths": ["M/a.stl", "M/b.stl"]})
    assert [i["key"] for i in res["items"]] == ["f0", "f1"]
    assert res["overlapping"] is True
    with pytest.raises(service.BadRequest):
        service.list_objects({"paths": ["../x.stl"]})


def test_submit_takes_assignment(lib, monkeypatch):
    write_stl(lib / "M" / "a.stl", cube())
    write_stl(lib / "M" / "b.stl", cube(x=20))
    monkeypatch.setattr(service, "queue_job", lambda job: job)
    base = {"printer": "u1", "path": "M/a.stl", "machine": "m", "process": "p", "filaments": ["A", "B"]}
    job = service.submit({**base, "extra_paths": ["M/b.stl"], "assign": {"f0": 1, "f1": 2},
                          "colours": ["#ff0000", "nope"], "keep_positions": False})
    assert job.extra_paths == ["M/b.stl"] and job.assign == {"f0": 1, "f1": 2}
    assert job.colours == ["#FF0000", ""] and job.keep_positions is False
    with pytest.raises(service.BadRequest):  # T3 without a third filament
        service.submit({**base, "assign": {"f0": 3}})


def test_project_filaments_grow_for_new_toolheads():
    import project
    cfg = {"filament_colour": ["#FF0000"], "filament_type": ["PLA"], "nozzle_temperature": ["220"],
           "filament_map": ["1"], "flush_volumes_matrix": ["0"], "compatible_printers": ["X"],
           "wall_loops": "2", "printer_extruder_id": ["1"]}
    assert project.pad_filaments(cfg, 3, {"nozzle_temperature", "compatible_printers"}, nozzles=4)
    assert cfg["filament_colour"] == ["#FF0000"] * 3 and cfg["filament_type"] == ["PLA"] * 3
    assert cfg["nozzle_temperature"] == ["220"] * 3
    assert cfg["filament_map"] == ["1", "2", "3"]
    assert cfg["flush_volumes_matrix"] == ["0", "280", "280", "280", "0", "280", "280", "280", "0"]
    assert cfg["compatible_printers"] == ["X"] and cfg["printer_extruder_id"] == ["1"]
    assert not project.pad_filaments(cfg, 2, set())  # already enough


def test_filaments_get_distinct_colours():
    fils = [{"filament_colour": ["#FFFFFF"]}, {"filament_colour": ["#ffffff"]}, {}, {"filament_colour": ["#D02020"]}]
    service.distinct_colours(fils)
    cols = [f["filament_colour"][0] for f in fils]
    assert cols[0] == "#FFFFFF" and len(set(c.upper() for c in cols)) == 4


def test_rebuild_project_for_more_toolheads(tmp_path):
    src = bambu_3mf(tmp_path / "a.3mf")
    out = tmp_path / "r.3mf"
    O.rebuild_3mf(src, {"o100": 3, "o101:3": 2}, out)
    items = O.threemf_items(out)
    assert len(items) == 3
    assert sorted(i.extruder for i in items) == [1, 2, 3]
    assert {i.name.split(" · ")[0] for i in items} == {"Obj0", "Obj1"}
    with pytest.raises(O.ObjectsError):
        O.rebuild_3mf(bambu_3mf(tmp_path / "p.3mf", painted=True), {"o100": 3}, out)


def test_plate_with_project_extra_stl_and_skips(tmp_path):
    src = bambu_3mf(tmp_path / "a.3mf")
    clip = write_stl(tmp_path / "clip.stl", cube(s=5))
    items = O.items_for([src, clip])
    assert [i.key for i in items] == ["o100", "o101:2", "o101:3", "f1"]
    out = tmp_path / "plate.3mf"
    O.plate_3mf([src, clip], {"o100": 0, "o101:3": 2, "f1": 3}, out)
    got = O.threemf_items(out)
    assert len(got) == 3 and sorted(i.extruder for i in got) == [1, 2, 3]
    with pytest.raises(O.ObjectsError):
        O.plate_3mf([src], {"o100": 0, "o101:2": 0, "o101:3": 0}, out)


def test_submit_skips_and_extras_on_a_project(lib, monkeypatch):
    write_stl(lib / "M" / "clip.stl", cube())
    bambu_3mf(lib / "M" / "a.3mf")
    monkeypatch.setattr(service, "queue_job", lambda job: job)
    base = {"printer": "u1", "path": "M/a.3mf", "machine": "m", "process": "p", "filaments": ["A", "B"]}
    job = service.submit({**base, "extra_paths": ["M/clip.stl"], "assign": {"o100": 0, "f1": 2}})
    assert job.assign == {"o100": 0, "f1": 2} and job.extra_paths == ["M/clip.stl"]
    with pytest.raises(service.BadRequest):
        service.submit({**base, "assign": {"o100": 0}, "mode": "project"})
    # Only zeros in assign: the pieces not named still print, so it's accepted;
    # an empty plate is caught when the plate is built (plate_3mf).
    job = service.submit({**base, "assign": {"o100": 0}})
    assert job.assign == {"o100": 0}
