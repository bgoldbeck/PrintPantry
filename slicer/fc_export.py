# Runs inside FreeCAD's command line (freecadcmd), started by render.py.
# Reads the job from the JSON file named in PP_RENDER_JOB:
#   {"src", "out_dir", "values": {alias: value}, "cells": {alias: [sheet, cell]},
#    "parts": [object names saved as STLs], "names": {object name: STL file name},
#    "combine": [object names for one multi-colour 3MF], "combine_name": "<file>.3mf",
#    "tolerance": 0.02}
# Sets the spreadsheet cells, recomputes, and writes one STL per part (and,
# when asked, one 3MF with all parts in one object, part N on filament N).
# Prints "PP:" lines for progress and a final "PP:RESULT {json}" line.

import json
import os
import sys
import traceback
import zipfile

# freecadcmd does not always set __file__
sys.path.insert(0, os.environ.get("PP_SLICER_DIR") or os.path.dirname(os.path.abspath(globals().get("__file__", "."))))
import fonts
import FreeCAD as App
import Mesh


def say(msg):
    print("PP:" + msg, flush=True)


def cell_value(value):
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value)


def fix_fonts(doc, idx, warnings):
    """Point Windows font paths at fonts that exist here."""
    for obj in doc.Objects:
        if obj.TypeId == "Spreadsheet::Sheet":
            for alias_cell in obj.getUsedCells():
                content = obj.getContents(alias_cell)
                if content.startswith("'") and content.lower().endswith(fonts.EXTS):
                    path, note = fonts.resolve(content[1:], idx)
                    if path != content[1:]:
                        obj.set(alias_cell, "'" + path)
                    if note:
                        warnings.append(note)
        elif hasattr(obj, "FontFile") and isinstance(obj.FontFile, str) and obj.FontFile:
            if any(p[0] == "FontFile" for p in obj.ExpressionEngine):
                continue  # follows a spreadsheet cell, fixed above
            path, note = fonts.resolve(obj.FontFile, idx)
            if path != obj.FontFile:
                obj.FontFile = path
            if note:
                warnings.append(note)


def mesh_xml(obj_id, name, mesh, shift):
    pts, facets = mesh.Topology
    dx, dy, dz = shift
    out = [' <object id="%d" name="%s" type="model">\n  <mesh>\n   <vertices>\n' % (obj_id, esc(name))]
    out += ['    <vertex x="%.4f" y="%.4f" z="%.4f"/>\n' % (p.x + dx, p.y + dy, p.z + dz) for p in pts]
    out.append("   </vertices>\n   <triangles>\n")
    out += ['    <triangle v1="%d" v2="%d" v3="%d"/>\n' % f for f in facets]
    out.append("   </triangles>\n  </mesh>\n </object>\n")
    return "".join(out)


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def write_3mf(path, title, parts):
    """One object made of parts; part i prints with filament i (1-based).
    Laid around the middle of the bed with its lowest point on the plate."""
    box = None
    for _, mesh in parts:
        bb = mesh.BoundBox
        box = bb if box is None else box.united(bb)
    shift = (-(box.XMin + box.XMax) / 2, -(box.YMin + box.YMax) / 2, -box.ZMin)
    resources, comps, cfg = [], [], []
    for i, (label, mesh) in enumerate(parts, start=1):
        resources.append(mesh_xml(i, label, mesh, shift))
        comps.append('   <component objectid="%d"/>\n' % i)
        cfg.append('  <part id="%d" subtype="normal_part">\n   <metadata key="name" value="%s"/>\n'
                   '   <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>\n'
                   '   <metadata key="extruder" value="%d"/>\n  </part>\n' % (i, esc(label), i))
    oid = len(parts) + 1
    resources.append(' <object id="%d" name="%s" type="model">\n  <components>\n%s  </components>\n </object>\n'
                     % (oid, esc(title), "".join(comps)))
    model = ('<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" '
             'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n'
             ' <metadata name="Title">%s</metadata>\n<resources>\n%s</resources>\n<build>\n'
             '  <item objectid="%d" transform="1 0 0 0 1 0 0 0 1 128 128 0" printable="1"/>\n</build>\n</model>\n'
             % (esc(title), "".join(resources), oid))
    config = ('<?xml version="1.0" encoding="UTF-8"?>\n<config>\n <object id="%d">\n'
              '  <metadata key="name" value="%s"/>\n  <metadata key="extruder" value="1"/>\n%s </object>\n</config>\n'
              % (oid, esc(title), "".join(cfg)))
    types = ('<?xml version="1.0" encoding="UTF-8"?>\n'
             '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
             ' <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
             ' <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
             ' <Default Extension="config" ContentType="text/xml"/>\n</Types>\n')
    rels = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
            ' <Relationship Target="/3D/3dmodel.model" Id="rel-1" '
            'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n</Relationships>\n')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", types)
        z.writestr("_rels/.rels", rels)
        z.writestr("3D/3dmodel.model", model)
        z.writestr("Metadata/model_settings.config", config)


def main():
    job = json.load(open(os.environ["PP_RENDER_JOB"], encoding="utf-8"))
    warnings = []
    say("STEP Opening " + os.path.basename(job["src"]))
    doc = App.openDocument(job["src"])
    say("STEP Setting parameters")
    for alias, value in job.get("values", {}).items():
        sheet_name, cell = job["cells"][alias]
        sheet = doc.getObject(sheet_name)
        if sheet is None:
            raise RuntimeError("No spreadsheet " + sheet_name)
        sheet.set(cell, cell_value(value))
    fix_fonts(doc, fonts.index(), warnings)
    say("STEP Recomputing the model")
    doc.recompute()
    doc.recompute()  # a second pass settles sheets that read back model sizes
    bad = []
    for o in doc.Objects:
        if "Invalid" in o.State or "Error" in o.State:
            msg = ""
            try:
                msg = o.getStatusString()
            except Exception:  # noqa: BLE001
                pass
            bad.append("%s%s" % (o.Label, ": " + msg if msg and msg != "Valid" else ""))
    if bad:
        raise RuntimeError("FreeCAD couldn't rebuild the model with these values: " + "; ".join(bad[:6]))
    tol = float(job.get("tolerance") or 0.02)
    outputs, meshes = [], []
    wanted = list(dict.fromkeys(list(job["parts"]) + list(job.get("combine") or [])))
    for name in wanted:
        obj = doc.getObject(name)
        if obj is None or not hasattr(obj, "Shape"):
            warnings.append("no part named " + name)
            continue
        shape = obj.Shape
        if not shape.Solids:
            warnings.append(obj.Label + " has no solid; skipped")
            continue
        say("STEP Meshing " + obj.Label)
        mesh = Mesh.Mesh(shape.tessellate(tol))
        if name in job["parts"]:
            out = os.path.join(job["out_dir"], job["names"][name])
            mesh.write(out)
            outputs.append(out)
        if name in (job.get("combine") or []):
            meshes.append((obj.Label, mesh))
    if job.get("combine_name") and len(meshes) > 1:
        say("STEP Writing multi-colour 3MF")
        out = os.path.join(job["out_dir"], job["combine_name"])
        write_3mf(out, os.path.splitext(job["combine_name"])[0], meshes)
        outputs.append(out)
    if not outputs:
        raise RuntimeError("Nothing to export: none of the chosen parts is a solid")
    say("RESULT " + json.dumps({"outputs": outputs, "warnings": warnings}))


try:
    main()
except Exception as exc:  # noqa: BLE001
    traceback.print_exc()
    say("ERROR " + (str(exc) or exc.__class__.__name__))
    sys.exit(3)
