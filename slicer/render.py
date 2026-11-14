"""Render parametric models (OpenSCAD, FreeCAD) to STL for the library.

Jobs run on the slicer service's single worker, so a render never runs next
to a slice (the NAS has little free memory). Output goes next to the source:
into <model folder>/generated/ when the source sits in a model folder, else
beside it. Names: "<source> - <variant>.stl", and for FreeCAD files with
several parts "<source> - <variant> - <part>.stl" (plus "<source> - <variant>.3mf"
holding every part in one multi-colour object, when asked).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import fonts

log = logging.getLogger("slicer.render")

OPENSCAD = os.environ.get("OPENSCAD_BIN", "/opt/openscad/AppRun")
FREECAD = os.environ.get("FREECAD_BIN", "/opt/freecad/usr/bin/freecadcmd")
TIMEOUT = int(os.environ.get("RENDER_TIMEOUT", "1200"))
HERE = Path(__file__).resolve().parent
OUT_DIR = "generated"
TYPE_DIRS = {"stl", "stls", "3mf", "3mfs", "gcode", "gcodes", "sliced", "prints", "print files",
             "files", "models", "model", "project", "projects", "source", "sources"}
LOG_KEEP = 20000

_versions: dict[str, str] = {}
_vlock = threading.Lock()


def _version(kind: str) -> str:
    """Tool version, or "" when it isn't installed. Asked once."""
    with _vlock:
        if kind in _versions:
            return _versions[kind]
        v = ""
        try:
            if kind == "openscad" and os.path.exists(OPENSCAD):
                p = subprocess.run([OPENSCAD, "--version"], capture_output=True, text=True, timeout=60, env=tool_env())
                v = (p.stdout + p.stderr).strip().splitlines()[-1].replace("OpenSCAD version", "").strip()
            elif kind == "freecad" and os.path.exists(FREECAD):
                p = subprocess.run([FREECAD, "-c", "import FreeCAD;print('V='+'.'.join(FreeCAD.Version()[:3]))"],
                                   capture_output=True, text=True, timeout=120, env=tool_env())
                m = re.search(r"V=(\S+)", p.stdout)
                v = m.group(1) if m else ""
        except (OSError, subprocess.SubprocessError, IndexError) as exc:
            log.warning("%s check failed: %s", kind, exc)
            v = ""
        if v:
            _versions[kind] = v  # a failed check is retried next time
        else:
            log.warning("%s is not usable here (%s)", kind, OPENSCAD if kind == "openscad" else FREECAD)
        return v


def tools() -> dict:
    return {"openscad": _version("openscad"), "freecad": _version("freecad"), "fonts": fonts.names()}


def tool_env() -> dict:
    env = dict(os.environ)
    env.setdefault("HOME", tempfile.gettempdir())
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PP_SLICER_DIR"] = str(HERE)
    conf = HERE / "fonts.conf"
    if conf.is_file():
        env["FONTCONFIG_FILE"] = str(conf)
    lib = Path(os.environ.get("PP_CONFIG", "/config")) / "openscad"
    env["OPENSCADPATH"] = str(lib)
    return env


def scad_literal(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(scad_literal(v) for v in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def out_folder(library: Path, relpath: str) -> Path:
    src = library / relpath
    parts = [p for p in Path(relpath).parts[:-1] if p.lower() not in TYPE_DIRS]
    return src.parent / OUT_DIR if len(parts) >= 2 else src.parent


def clean_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip(" .") or "part"


def _stem(name: str) -> str:
    return re.sub(r"\.(scad|fcstd)$", "", name, flags=re.IGNORECASE)


def run(job, library: Path, work_root: Path) -> None:
    """Fill job.outputs (library-relative paths), job.warnings, job.message."""
    r = job.render
    src = (library / job.path).resolve()
    if not src.is_file():
        raise RuntimeError(f"{job.path} is gone; rescan the library")
    if r["kind"] == "scad" and not _version("openscad"):
        raise RuntimeError("OpenSCAD isn't installed in the slicer container; rebuild it")
    if r["kind"] == "fcstd" and not _version("freecad"):
        raise RuntimeError("FreeCAD isn't installed in the slicer container; rebuild it")
    stem = _stem(src.name)
    variant = clean_name(r.get("variant") or "custom")
    work_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f"{job.id}-", dir=work_root))
    try:
        out = work / "out"
        out.mkdir()
        if r["kind"] == "scad":
            files = _run_scad(job, src, out, f"{stem} - {variant}.stl")
        else:
            files = _run_freecad(job, src, work, out, stem, variant)
        job.outputs = publish(library, job.path, files)
        job.message = "Made " + ", ".join(Path(o).name for o in job.outputs)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _stream(job, args, cwd: Path, env: dict, on_line) -> int:
    """Run a tool, feeding each output line to on_line; enforce the timeout."""
    proc = subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors="replace", bufsize=1)
    killed = threading.Event()

    def kill():
        killed.set()
        proc.kill()

    timer = threading.Timer(TIMEOUT, kill)
    timer.start()
    try:
        for line in proc.stdout:
            job.log = (job.log + line)[-LOG_KEEP:]
            on_line(line.rstrip("\n"))
        code = proc.wait()
    finally:
        timer.cancel()
    if killed.is_set():
        raise RuntimeError(f"Gave up after {TIMEOUT // 60} minutes")
    if code < 0:
        raise RuntimeError(f"The renderer crashed (signal {-code}; out of memory?)")
    return code


SCAD_STEPS = (("Parsing", "Reading the design"), ("Compiling", "Building the design"),
              ("Rendering", "Rendering the solid"), ("Manifold", "Rendering the solid"),
              ("CGAL", "Rendering the solid"), ("Simple:", "Checking the result"))


def _run_scad(job, src: Path, out: Path, name: str) -> list[Path]:
    target = out / "model.stl"
    args = [OPENSCAD, "-o", str(target), "--export-format", "binstl"]
    year = re.match(r"(\d{4})", _version("openscad"))
    if year and int(year.group(1)) >= 2024:
        args += ["--backend", "manifold"]  # much faster than the old CGAL renderer
    for k, v in job.render.get("values", {}).items():
        args += ["-D", f"{k}={scad_literal(v)}"]
    args.append(str(src))
    job.message = "Starting OpenSCAD"
    errors: list[str] = []

    def line(text: str):
        for key, msg in SCAD_STEPS:
            if key in text:
                job.message = msg
        if text.startswith(("ERROR", "WARNING")):
            (errors if text.startswith("ERROR") else job.warnings).append(text[:300])

    code = _stream(job, args, src.parent, tool_env(), line)
    if code != 0 or not target.is_file() or target.stat().st_size < 100:
        msg = errors[0] if errors else "see the log"
        if "Current top level object is empty" in job.log:
            msg = "the design came out empty with these values"
        raise RuntimeError(f"OpenSCAD failed: {msg}")
    final = out / name
    target.rename(final)
    return [final]


def _run_freecad(job, src: Path, work: Path, out: Path, stem: str, variant: str) -> list[Path]:
    r = job.render
    parts = list(r.get("parts") or [])  # each saved as an STL
    combine = list(r.get("combine") or [])  # together in one 3MF, part N on filament N
    names = {}
    for p in parts:
        label = clean_name(str(r.get("labels", {}).get(p, p)))
        names[p] = f"{stem} - {variant}.stl" if len(parts) == 1 else f"{stem} - {variant} - {label}.stl"
    spec = {"src": str(src), "out_dir": str(out), "values": r.get("values", {}), "cells": r.get("cells", {}),
            "parts": parts, "names": names, "tolerance": 0.02, "combine": combine,
            "combine_name": f"{stem} - {variant}.3mf" if len(combine) > 1 else ""}
    spec_file = work / "job.json"
    spec_file.write_text(json.dumps(spec), "utf-8")
    env = tool_env()
    env["PP_RENDER_JOB"] = str(spec_file)
    result: dict = {}
    error = ""
    job.message = "Starting FreeCAD"

    def line(text: str):
        nonlocal result, error
        if not text.startswith("PP:"):
            return
        kind, _, rest = text[3:].partition(" ")
        if kind == "STEP":
            job.message = rest
        elif kind == "RESULT":
            result = json.loads(rest)
        elif kind == "ERROR":
            error = rest

    # A copy, so FreeCAD never writes into the library's own file.
    local = work / src.name
    shutil.copyfile(src, local)
    spec["src"] = str(local)
    spec_file.write_text(json.dumps(spec), "utf-8")
    _stream(job, [FREECAD, str(HERE / "fc_export.py")], work, env, line)
    if error or not result:
        raise RuntimeError(f"FreeCAD failed: {error or 'see the log'}")
    job.warnings += list(dict.fromkeys(result.get("warnings", [])))
    return [Path(p) for p in result.get("outputs", [])]


def publish(library: Path, relpath: str, files: list[Path]) -> list[str]:
    dest = out_folder(library, relpath)
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for f in files:
        target = dest / f.name
        tmp = target.with_name(".pp-" + target.name)
        shutil.copyfile(f, tmp)
        tmp.replace(target)
        out.append(target.relative_to(library).as_posix())
    return out
