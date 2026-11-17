"""Build app/static/process_catalog.json from OrcaSlicer's source.

The Slice dialog's "Process settings" panel shows the same tabs, groups and
settings as OrcaSlicer's process tab (TabPrint::build in Tab.cpp), with the
labels, tooltips, units, limits and choices from PrintConfig.cpp.

    curl -LO https://raw.githubusercontent.com/OrcaSlicer/OrcaSlicer/v2.4.2/src/libslic3r/PrintConfig.cpp
    curl -LO https://raw.githubusercontent.com/OrcaSlicer/OrcaSlicer/v2.4.2/src/slic3r/GUI/Tab.cpp
    python tools/gen_process_catalog.py PrintConfig.cpp Tab.cpp v2.4.2
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "app" / "static" / "process_catalog.json"
# G-code snippets, scripts and file naming: not something to change per slice.
SKIP = {"post_process", "filename_format", "notes", "process_change_extrusion_role_gcode",
        "small_area_infill_flow_compensation_model", "gcode_label_objects", "gcode_comments",
        "gcode_add_line_number", "exclude_object", "reduce_infill_retraction"}
# Shown without "Advanced" (on top of OrcaSlicer's own Simple mode settings):
# the ones people change most from print to print.
BASIC = {"outer_wall_speed", "inner_wall_speed", "sparse_infill_speed", "internal_solid_infill_speed",
         "top_surface_speed", "initial_layer_speed", "initial_layer_infill_speed", "travel_speed",
         "gap_infill_speed", "support_speed", "bridge_speed", "default_acceleration", "ironing_type",
         "line_width", "brim_type", "support_style", "raft_layers", "support_object_xy_distance",
         "support_bottom_z_distance", "internal_solid_infill_pattern", "detect_thin_wall",
         "slow_down_layers", "enable_overhang_speed", "skirt_distance"}
TYPES = {"coBool": "bool", "coBools": "bool", "coInt": "int", "coInts": "int",
         "coFloat": "float", "coFloats": "float", "coPercent": "percent", "coPercents": "percent",
         "coFloatOrPercent": "floatpct", "coFloatsOrPercents": "floatpct", "coEnum": "enum",
         "coEnums": "enum", "coString": "string"}
STR = re.compile(r'"((?:[^"\\]|\\.)*)"')


def text(expr: str) -> str:
    s = "".join(STR.findall(expr))
    s = re.sub(r'\\(["\\])', r"\1", s.replace("\\n", "\n").replace("\\t", " "))
    return s.strip()


def num(expr: str):
    expr = expr.strip().rstrip(";").strip()
    try:
        v = float(expr)
    except ValueError:
        return None
    return int(v) if v.is_integer() else v


def parse_defs(src: str) -> dict:
    defs = {}
    # "def = this->add(" or "auto name = def = this->add(" (name is copied from later)
    parts = re.split(r'\n\s*(?:auto\s+\w+\s*=\s*)?def\s*=\s*this->add(?:_nullable)?\(\s*"', src)
    names = dict(re.findall(r'auto\s+(\w+)\s*=\s*def\s*=\s*this->add\(\s*"([a-z0-9_]+)"', src))
    for part in parts[1:]:
        m = re.match(r'([a-z0-9_]+)"\s*,\s*(co[A-Za-z]+)\)', part)
        if not m:
            continue
        key, ctype = m.groups()
        body = re.sub(r'^\s*//.*$', "", part[m.end():], flags=re.M)
        body = re.sub(r';[ \t]*//[^"\n]*$', ";", body, flags=re.M)  # trailing comments
        # Statements up to the next definition (the split already cut there).
        d = {"ctype": ctype, "enum": [], "labels": [], "mode": "comSimple"}
        for stmt in re.split(r';\s*\n', body):
            stmt = stmt.strip()
            c = re.match(r'def->enum_(values|labels)\s*=\s*(\w+)->enum_\w+$', stmt)
            if c and c.group(2) in names:
                d["copy"] = names[c.group(2)]
                continue
            a = re.match(r'def->(\w+)\s*=\s*(.*)', stmt, re.S)
            if a:
                field, val = a.groups()
                if field in ("label", "full_label", "tooltip", "sidetext"):
                    d[field] = text(val)
                elif field in ("min", "max"):
                    d[field] = num(val)
                elif field == "mode":
                    d["mode"] = val.strip()
                continue
            e = re.match(r'def->enum_(values|labels)\.(?:push|emplace)_back\((.*)\)$', stmt, re.S)
            if e:
                (d["enum"] if e.group(1) == "values" else d["labels"]).append(text(e.group(2)))
                continue
            dv = re.search(r'set_default_value\(new ConfigOption\w+(?:<\w+>)?\((.*)\)\)$', stmt, re.S)
            if dv:
                d["default"] = dv.group(1)
        defs[key] = d
    for d in defs.values():
        if d.get("copy") in defs and not d["enum"]:
            d["enum"], d["labels"] = defs[d["copy"]]["enum"], defs[d["copy"]]["labels"]
    return defs


def parse_tab(src: str) -> list[dict]:
    start = src.index("void TabPrint::build()")
    end = src.index("\nvoid TabPrint::", start + 10)
    body = src[start:end]
    tabs: list[dict] = []
    line_label = ""
    for raw in body.splitlines():
        s = raw.strip()
        if s.startswith("//"):
            continue
        m = re.search(r'add_options_page\(L\("([^"]+)"\)', s)
        if m:
            tabs.append({"name": m.group(1), "groups": []})
            continue
        m = re.search(r'new_optgroup\(L\("([^"]+)"\)', s)
        if m and tabs:
            tabs[-1]["groups"].append({"name": m.group(1), "keys": []})
            continue
        m = re.search(r'Line line\s*=\s*\{\s*L\("([^"]+)"\)|^line\s*=\s*\{\s*L\("([^"]+)"\)', s)
        if m:
            line_label = m.group(1) or m.group(2)
            continue
        m = re.search(r'append_single_option_line\("([a-z0-9_]+)"', s)
        if m and tabs and tabs[-1]["groups"]:
            tabs[-1]["groups"][-1]["keys"].append((m.group(1), ""))
            continue
        m = re.search(r'line\.append_option\(optgroup->get_option\("([a-z0-9_]+)"', s)
        if m and tabs and tabs[-1]["groups"]:
            tabs[-1]["groups"][-1]["keys"].append((m.group(1), line_label))
    return tabs


def build(defs: dict, layout: list[dict], version: str) -> dict:
    out = {"orca": version, "tabs": []}
    seen = set()
    for tab in layout:
        groups = []
        for g in tab["groups"]:
            opts = []
            for key, line in g["keys"]:
                d = defs.get(key)
                if not d or key in SKIP or key in seen or d["ctype"] not in TYPES:
                    continue
                seen.add(key)
                label = d.get("label") or d.get("full_label") or key
                if line and line.lower() not in label.lower():
                    label = f"{line}: {label}"
                o = {"key": key, "label": label, "type": TYPES[d["ctype"]]}
                if d["ctype"].endswith("s") and d["ctype"] != "coFloatOrPercent":
                    o["list"] = True
                if d["mode"] != "comSimple" and key not in BASIC:
                    o["advanced"] = True
                for f in ("tooltip", "sidetext", "min", "max"):
                    if d.get(f) not in (None, ""):
                        o["unit" if f == "sidetext" else f] = d[f]
                if o["type"] == "enum":
                    labels = d["labels"] + d["enum"][len(d["labels"]):]
                    o["choices"] = [[v, labels[i] or v] for i, v in enumerate(d["enum"])]
                    if not o["choices"]:
                        continue
                opts.append(o)
            if opts:
                groups.append({"name": g["name"], "options": opts})
        if groups:
            out["tabs"].append({"name": tab["name"], "groups": groups})
    return out


def main():
    pc, tab, version = sys.argv[1], sys.argv[2], (sys.argv[3] if len(sys.argv) > 3 else "")
    defs = parse_defs(Path(pc).read_text(encoding="utf-8"))
    layout = parse_tab(Path(tab).read_text(encoding="utf-8"))
    cat = build(defs, layout, version)
    OUT.write_text(json.dumps(cat, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    n = sum(len(g["options"]) for t in cat["tabs"] for g in t["groups"])
    basic = sum(1 for t in cat["tabs"] for g in t["groups"] for o in g["options"] if not o.get("advanced"))
    print(f"{OUT}: {len(cat['tabs'])} tabs, {n} settings ({basic} basic)")


if __name__ == "__main__":
    main()
