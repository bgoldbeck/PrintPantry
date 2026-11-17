import struct

from app import toolpath

GCODE = """; printable_area = 0x0,270x0,270x270,0x270
G90
M83
T0
G1 Z0.2
G1 X10 Y10
G1 X20 Y10 E1
G1 X20 Y20 E1
T1
G1 X30 Y30
G3 X40 Y30 I5 J0 E1
G1 X50 Y50
"""


def test_toolpath_splits_by_tool(tmp_path):
    p = tmp_path / "a.gcode"
    p.write_text(GCODE)
    bed, segs = toolpath.parse(p)
    assert bed == (0, 0, 270, 270)
    assert segs[0] == [10, 10, 0.2, 20, 10, 0.2, 20, 10, 0.2, 20, 20, 0.2]
    assert len(segs[1]) // 6 > 3  # arc is broken into pieces
    data = toolpath.encode(p)
    assert data[:4] == b"PPTP"
    assert struct.unpack_from("<4fI", data, 4) == (0, 0, 270, 270, 2)


def test_bambu_ams_loads_and_macros(tmp_path):
    p = tmp_path / "b.gcode"
    p.write_text("G90\nM83\nM620 S2A\nT2\nM621 S2A\nT1000\nG1 Z0.2\nG1 X10 Y10\nG1 X20 Y10 E1\n"
                 "M620 S3A\nT3\nM621 S3A\nG1 X30 Y30 E1\nM620 S255\nT255\n")
    _, segs = toolpath.parse(p)
    assert set(segs) == {2, 3}
