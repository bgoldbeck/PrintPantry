"""Find font files for parametric models made on Windows.

A FreeCAD model made on Windows points at fonts like C:/Windows/Fonts/arialbd.ttf.
On the NAS we look for the same file name in the config folder's "fonts"
directory first (copy fonts there from C:\\Windows\\Fonts), then in the
container's own fonts, then fall back to a look-alike (Liberation for Arial,
Times and Courier, DejaVu for anything else).

Standard library only: imported by the slicer service and by the script that
runs inside FreeCAD.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

EXTS = (".ttf", ".otf", ".ttc")
DIRS = [Path(os.environ.get("PP_CONFIG", "/config")) / "fonts", Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts")]

# Windows file name prefix -> Liberation family (metric-compatible).
LOOKALIKE = {"arial": "LiberationSans", "helvetica": "LiberationSans", "segoeui": "LiberationSans",
             "calibri": "LiberationSans", "verdana": "DejaVuSans", "tahoma": "DejaVuSans",
             "times": "LiberationSerif", "georgia": "LiberationSerif", "cambria": "LiberationSerif",
             "cour": "LiberationMono", "consola": "LiberationMono", "impact": "LiberationSansNarrow"}


def index(dirs: list[Path] | None = None) -> dict[str, Path]:
    """{lower-case file name: path}; earlier folders win."""
    out: dict[str, Path] = {}
    for d in dirs or DIRS:
        if not d.is_dir():
            continue
        for root, _, files in os.walk(d):
            for fn in sorted(files):
                if fn.lower().endswith(EXTS):
                    out.setdefault(fn.lower(), Path(root) / fn)
    return out


def _style(stem: str) -> str:
    s = stem.lower().replace("segoeui", "segoe")
    bold = bool(re.search(r"bold|bd$|bd[^a-z]|b$|black|heavy|semibold", s)) or s.endswith("bi") or s.endswith("z")
    italic = bool(re.search(r"italic|oblique|i$", s)) and not s.endswith("bd")
    if s.endswith("bi") or s.endswith("z"):
        italic = True
    return "BoldItalic" if bold and italic else "Bold" if bold else "Italic" if italic else "Regular"


def resolve(value: str, idx: dict[str, Path] | None = None) -> tuple[str, str]:
    """(path to use, note). The note is empty when the font was found as is."""
    if not value:
        return value, ""
    if os.path.isfile(value):
        return value, ""
    idx = index() if idx is None else idx
    name = re.split(r"[\\/]", value)[-1]
    hit = idx.get(name.lower())
    if hit:
        return str(hit), ""
    stem = Path(name).stem
    family = next((fam for pre, fam in LOOKALIKE.items() if stem.lower().startswith(pre)), "DejaVuSans")
    style = _style(stem)
    candidates = [f"{family}-{style}.ttf", f"{family}-Regular.ttf"]
    if family == "DejaVuSans":
        candidates = [f"DejaVuSans{'-Bold' if 'Bold' in style else ''}.ttf", "DejaVuSans.ttf"]
    if family == "LiberationSansNarrow":
        candidates = [f"LiberationSansNarrow-{style}.ttf", "LiberationSansNarrow-Bold.ttf", "LiberationSans-Bold.ttf"]
    for c in candidates:
        hit = idx.get(c.lower())
        if hit:
            return str(hit), f"font {name} not on the NAS; used {hit.name} (copy {name} into config/fonts)"
    any_font = next(iter(idx.values()), None)
    if any_font:
        return str(any_font), f"font {name} not on the NAS; used {any_font.name} (copy {name} into config/fonts)"
    return value, f"font {name} not found and no fonts installed"


def names(idx: dict[str, Path] | None = None) -> list[str]:
    idx = index() if idx is None else idx
    return sorted({p.name for p in idx.values()}, key=str.lower)
