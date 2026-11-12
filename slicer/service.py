"""Print Pantry slicer service: a small HTTP API around the OrcaSlicer CLI.

Runs in its own container next to the main app (see slicer/Dockerfile) and is
only reachable on the compose network. Standard library only.

    GET  /health                       slicer version, preset counts
    GET  /options?printer=u1&path=..   presets to choose from, sensible defaults
    POST /jobs                         queue a slice; returns {"id": ...}
    GET  /jobs                         recent jobs
    GET  /jobs/<id>                    one job: state, message, outputs, log tail

One job runs at a time (the NAS has little free memory). Output files are
written into the library next to the source, in a "sliced" folder, named
<source>_U1.gcode or <source>_P1S.gcode.3mf, so the library scanner files them
under the same model.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import presets as P
import project

log = logging.getLogger("slicer")

BIN = os.environ.get("SLICER_BIN", "/opt/orca/AppRun")
PROFILES = Path(os.environ.get("SLICER_PROFILES", "/opt/orca/resources/profiles"))
VERSION = os.environ.get("SLICER_VERSION", "")
LIBRARY = Path(os.environ.get("PP_LIBRARY", "/library"))
CONFIG = Path(os.environ.get("PP_CONFIG", "/config"))
WORK = Path(os.environ.get("SLICER_WORK", tempfile.gettempdir())) / "pp-slicer"
TIMEOUT = int(os.environ.get("SLICER_TIMEOUT", "1200"))
PORT = int(os.environ.get("PP_SLICER_PORT", "8485"))
OUT_DIR = "sliced"
TYPE_DIRS = {"stl", "stls", "3mf", "3mfs", "gcode", "gcodes", "sliced", "prints", "print files",
             "files", "models", "model", "project", "projects", "source", "sources"}


@dataclass(frozen=True)
class Printer:
    id: str
    name: str
    vendor: str  # profile vendor folder
    model: str  # printer_model in the profiles
    output: str  # "gcode" or "gcode.3mf"
    tag: str  # file name suffix
    max_filaments: int
    default_machine: str


PRINTERS = {
    "u1": Printer("u1", "Snapmaker U1", "Snapmaker", "Snapmaker U1", "gcode", "U1", 4,
                  "Snapmaker U1 (0.4 nozzle)"),
    "p1s": Printer("p1s", "Bambu P1S", "BBL", "Bambu Lab P1S", "gcode.3mf", "P1S", 16,
                   "Bambu Lab P1S 0.4 nozzle"),
}

# Bed type name in the slicer -> filament key holding its temperature, label.
BED_TYPES = [
    ("Textured PEI Plate", "textured_plate_temp", "Textured PEI"),
    ("High Temp Plate", "hot_plate_temp", "Smooth PEI / High Temp"),
    ("Cool Plate", "cool_plate_temp", "Cool Plate"),
    ("Engineering Plate", "eng_plate_temp", "Engineering Plate"),
    ("Textured Cool Plate", "textured_cool_plate_temp", "Textured Cool Plate"),
    ("Supertack Plate", "supertack_plate_temp", "Supertack Plate"),
]
BED_KEYS = {b[0]: b[1] for b in BED_TYPES}


# ---------------------------------------------------------------- presets

class PresetCache:
    """Loads presets once, and again when the config folder changes."""

    def __init__(self):
        self.lock = threading.Lock()
        self.lib: P.PresetLibrary | None = None
        self.stamp = None
        self.options_cache: dict = {}

    def _stamp(self):
        if not CONFIG.is_dir():
            return None
        return tuple(sorted((str(p), p.stat().st_mtime, p.stat().st_size) for p in CONFIG.rglob("*") if p.is_file()))

    def get(self) -> P.PresetLibrary:
        with self.lock:
            stamp = self._stamp()
            if self.lib is None or stamp != self.stamp:
                vendors = tuple(dict.fromkeys(p.vendor for p in PRINTERS.values()))
                self.lib = P.PresetLibrary(PROFILES, CONFIG, vendors=vendors).load()
                self.stamp = stamp
                self.options_cache = {}
                log.info("loaded %d system and %d user presets", len(self.lib.system), len(self.lib.user))
            return self.lib


PRESETS = PresetCache()


def _first(v):
    return v[0] if isinstance(v, list) and v else v


def _temp(fil: dict, key: str) -> int:
    try:
        return int(float(_first(fil.get(key, 0)) or 0))
    except (TypeError, ValueError):
        return 0


def printer_options(printer: Printer) -> dict:
    """Everything the dialog needs for one printer (cached per preset load)."""
    lib = PRESETS.get()
    cached = PRESETS.options_cache.get(printer.id)
    if cached:
        return cached
    machines = lib.machines_for(printer.model, printer.vendor)
    machines.sort(key=lambda m: (m.source != "user", m.name != printer.default_machine, m.name))
    out = {"machines": [], "processes": {}, "filaments": {}}
    for m in machines:
        out["machines"].append({"name": m.name, "user": m.source == "user"})
        procs = lib.compatible("process", m.name, printer.vendor)
        procs.sort(key=lambda p: (p.source != "user", p.name))
        out["processes"][m.name] = {
            "names": [p.name for p in procs],
            "user": [p.name for p in procs if p.source == "user"],
            "default": next((p.name for p in procs if p.source == "user"), "") or P.PresetLibrary.pick_process(procs),
        }
        fils = []
        for f in lib.compatible("filament", m.name, printer.vendor):
            try:
                flat = lib.flatten("filament", f.name, printer.vendor)
            except P.PresetError:
                continue
            fils.append({
                "name": f.name,
                "type": str(_first(flat.get("filament_type", "")) or ""),
                "user": f.source == "user",
                "beds": {b: _temp(flat, k) for b, k, _ in BED_TYPES},
            })
        fils.sort(key=lambda f: (not f["user"], f["name"].lower()))
        out["filaments"][m.name] = fils
    PRESETS.options_cache[printer.id] = out
    return out


def default_filament(lib: P.PresetLibrary, printer: Printer, machine: str, wanted_type: str, wanted_name: str = "") -> str:
    choices = lib.compatible("filament", machine, printer.vendor)
    names = {c.name for c in choices}
    if wanted_name in names:
        return wanted_name
    return lib.pick_filament(wanted_type, choices, printer.vendor)


def default_bed(machine_flat: dict, filaments: list[dict]) -> str:
    """The machine's default plate, unless the filament has no temperature for it."""
    order = [b[0] for b in BED_TYPES]
    preferred = str(machine_flat.get("default_bed_type") or "Textured PEI Plate")
    if preferred in order:
        order.remove(preferred)
        order.insert(0, preferred)
    for bed in order:
        if all(_temp(f, BED_KEYS[bed]) > 0 for f in filaments):
            return bed
    return preferred


def options(printer_id: str, relpath: str) -> dict:
    printer = PRINTERS.get(printer_id)
    if not printer:
        raise BadRequest(f"Unknown printer {printer_id!r}")
    lib = PRESETS.get()
    opts = printer_options(printer)
    src = library_path(relpath) if relpath else None
    info = project.inspect(src) if src else project.ProjectInfo(kind="stl")
    matches = bool(info.kind == "3mf" and info.printer_model and info.printer_model.lower() == printer.model.lower())
    names = [m["name"] for m in opts["machines"]]
    machine = info.printer_preset if (matches and info.printer_preset in names) else (names[0] if names else "")
    fil_defaults = []
    flats = []
    wanted = info.filaments or [{"type": "PLA", "colour": "", "preset": ""}]
    for f in wanted:
        name = default_filament(lib, printer, machine, f.get("type", ""), f.get("preset", "") if matches else "")
        fil_defaults.append(name)
        try:
            flats.append(lib.flatten("filament", name, printer.vendor))
        except P.PresetError:
            pass
    try:
        machine_flat = lib.flatten("machine", machine, printer.vendor) if machine else {}
    except P.PresetError:
        machine_flat = {}
    proc = opts["processes"].get(machine, {})
    process_default = proc.get("default", "")
    if matches and info.process_preset in proc.get("names", []):
        process_default = info.process_preset
    return {
        "printer": {"id": printer.id, "name": printer.name, "max_filaments": printer.max_filaments,
                    "output": printer.output},
        "project": info.to_json(),
        "project_matches": matches,
        "bed_types": [{"name": b, "label": label} for b, _, label in BED_TYPES],
        **opts,
        "defaults": {
            "mode": "project" if matches else "presets",
            "machine": machine,
            "process": process_default,
            "filaments": fil_defaults,
            "bed_type": (info.bed_type if matches and info.bed_type in BED_KEYS else default_bed(machine_flat, flats)),
            "plate": 0,
        },
        "problems": lib.problems,
    }


# ---------------------------------------------------------------- paths

class BadRequest(Exception):
    pass


def library_path(relpath: str) -> Path:
    rel = Path(relpath)
    if rel.is_absolute() or ".." in rel.parts:
        raise BadRequest("Bad path")
    path = (LIBRARY / rel).resolve()
    if LIBRARY.resolve() not in path.parents:
        raise BadRequest("Bad path")
    if not path.is_file():
        raise BadRequest(f"{relpath} not found")
    if not path.name.lower().endswith((".3mf", ".stl")) or path.name.lower().endswith(".gcode.3mf"):
        raise BadRequest("Only project 3MF and STL files can be sliced")
    return path


def output_dir(src: Path) -> Path:
    parent = src.parent
    if parent.name.lower() in TYPE_DIRS and parent != LIBRARY:
        parent = parent.parent
    return parent / OUT_DIR


PRINTER_TAG = re.compile(r"([ _.\-]+(p1s|u1|snapmaker[ _]?u1|bambu[ _]?p1s))+$", re.I)


def output_name(src: Path, printer: Printer, plate: int | None) -> str:
    stem = src.name[: -len(".3mf")] if src.name.lower().endswith(".3mf") else src.stem
    stem = PRINTER_TAG.sub("", stem) or stem
    if printer.output == "gcode.3mf":
        return f"{stem}_{printer.tag}.gcode.3mf"
    if plate:
        return f"{stem}_{printer.tag}_plate_{plate}.gcode"
    return f"{stem}_{printer.tag}.gcode"


# ---------------------------------------------------------------- jobs

@dataclass
class Job:
    id: str
    path: str
    printer: str
    mode: str  # "project" (use the 3MF's own settings) or "presets"
    machine: str = ""
    process: str = ""
    filaments: list[str] = field(default_factory=list)
    bed_type: str = ""
    plate: int = 0
    state: str = "queued"  # queued | running | done | failed
    message: str = "Waiting for the slicer"
    outputs: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    log: str = ""
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None

    def to_json(self) -> dict:
        d = dict(self.__dict__)
        d["log"] = self.log[-4000:]
        return d


JOBS: dict[str, Job] = {}
QUEUE: "queue.Queue[str]" = queue.Queue()
JOBS_LOCK = threading.Lock()


def submit(body: dict) -> Job:
    printer = PRINTERS.get(str(body.get("printer", "")))
    if not printer:
        raise BadRequest("Unknown printer")
    path = str(body.get("path", ""))
    src = library_path(path)
    mode = body.get("mode") or "presets"
    if mode not in ("project", "presets"):
        raise BadRequest("mode must be project or presets")
    if src.suffix.lower() == ".stl":
        mode = "presets"
    filaments = [str(f) for f in body.get("filaments") or []]
    if mode == "presets":
        if not body.get("machine") or not body.get("process"):
            raise BadRequest("Pick a printer preset and a process preset")
        if not filaments:
            raise BadRequest("Pick at least one filament")
        if len(filaments) > printer.max_filaments:
            raise BadRequest(f"{printer.name} takes at most {printer.max_filaments} filaments; this needs {len(filaments)}")
    plate = int(body.get("plate") or 0)
    job = Job(
        id=uuid.uuid4().hex[:12], path=path, printer=printer.id, mode=mode,
        machine=str(body.get("machine", "")), process=str(body.get("process", "")),
        filaments=filaments, bed_type=str(body.get("bed_type", "")), plate=max(0, plate),
    )
    with JOBS_LOCK:
        JOBS[job.id] = job
        # Keep the last 50 jobs.
        for old in sorted(JOBS.values(), key=lambda j: j.created)[:-50]:
            if old.state in ("done", "failed"):
                JOBS.pop(old.id, None)
    QUEUE.put(job.id)
    return job


def worker():
    while True:
        job = JOBS.get(QUEUE.get())
        if not job:
            continue
        job.state, job.started, job.message = "running", time.time(), "Slicing..."
        try:
            run_job(job)
            job.state = "done"
        except Exception as exc:  # noqa: BLE001 - report every failure to the user
            log.exception("job %s failed", job.id)
            job.state = "failed"
            job.message = str(exc) or exc.__class__.__name__
        job.finished = time.time()


def write_json(path: Path, data: dict) -> str:
    path.write_text(json.dumps(data, indent=1))
    return path.name


def build_settings(job: Job, printer: Printer, work: Path, info: project.ProjectInfo) -> list[str]:
    """Flat preset files for the CLI; returns the extra arguments."""
    lib = PRESETS.get()
    try:
        machine = lib.flatten("machine", job.machine, printer.vendor)
        process = lib.flatten("process", job.process, printer.vendor)
        fils = [lib.flatten("filament", n, printer.vendor) for n in job.filaments]
    except P.PresetError as exc:
        raise RuntimeError(str(exc)) from exc
    if str(machine.get("printer_model", "")).lower() != printer.model.lower():
        raise RuntimeError(f'"{job.machine}" is not a {printer.name} preset')
    # The process file must carry the nozzle sizes, or the slicer crashes while
    # normalising it, and the plate type (a project setting, not a preset one).
    project.pad_per_extruder(machine)
    process["nozzle_diameter"] = machine.get("nozzle_diameter", ["0.4"])
    # When the project was made for another printer, the CLI only switches if
    # the process lists the new printer's built-in name as compatible.
    machine_system = machine["name"] if machine.get("from") == "system" else machine.get("inherits", "")
    compat = process.get("compatible_printers")
    compat = [c for c in compat.split(";") if c] if isinstance(compat, str) else list(compat or [])
    for name in (machine_system, machine["name"]):
        if name and name not in compat:
            compat.append(name)
    process["compatible_printers"] = compat
    # Shown in the job log, since the CLI says little when it rejects presets.
    job.log = (f'machine "{machine["name"]}" ({machine.get("from")}, built-in "{machine_system}"); '
               f'process "{process["name"]}" ({process.get("from")}, built-in "{process.get("inherits", "")}", '
               f'compatible {compat})\n')
    bed = job.bed_type if job.bed_type in BED_KEYS else default_bed(machine, fils)
    process["curr_bed_type"] = bed
    job.bed_type = bed
    for i, f in enumerate(fils):
        if i < len(info.filaments) and info.filaments[i].get("colour"):
            f["filament_colour"] = [info.filaments[i]["colour"]]
        if _temp(f, BED_KEYS[bed]) <= 0:
            job.warnings.append(f'{f["name"]} has no bed temperature for {bed}; the bed will not heat')
    names = [write_json(work / "machine.json", machine), write_json(work / "process.json", process)]
    fil_names = [write_json(work / f"filament_{i + 1}.json", f) for i, f in enumerate(fils)]
    return ["--load-settings", ";".join(names), "--load-filaments", ";".join(fil_names)]


def run_job(job: Job) -> None:
    printer = PRINTERS[job.printer]
    src = library_path(job.path)
    info = project.inspect(src)
    if info.kind == "3mf" and job.mode == "presets" and info.filaments and len(job.filaments) != len(info.filaments):
        raise RuntimeError(f"This project uses {len(info.filaments)} filaments; pick one for each")
    if len(info.filaments) > printer.max_filaments:
        raise RuntimeError(f"This project uses {len(info.filaments)} filaments; {printer.name} has {printer.max_filaments}")
    if job.mode == "project" and info.printer_model.lower() != printer.model.lower():
        raise RuntimeError(f"This project is set up for {info.printer_model or 'another printer'}; pick presets instead")

    WORK.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f"{job.id}-", dir=WORK))
    try:
        out = work / "out"
        out.mkdir()
        args = [BIN, "--allow-newer-file", "--debug", "2", "--slice", str(job.plate or 0), "--outputdir", str(out)]
        if job.mode == "presets":
            args += build_settings(job, printer, work, info)
        if info.kind == "stl":
            args += ["--arrange", "1", "--orient", "0"]
        if printer.output == "gcode.3mf":
            args += ["--export-3mf", "result.gcode.3mf"]  # relative to --outputdir
        # Presets passed on the command line override the project's own
        # settings. (Removing the project settings from the 3MF instead makes
        # the slicer crash.)
        inp = work / ("input" + src.suffix.lower())
        if info.kind == "3mf":
            project.copy_for_slicing(src, inp)
        else:
            shutil.copyfile(src, inp)
        args.append(str(inp))
        log.info("job %s: %s", job.id, " ".join(args))
        # In case OrcaSlicer's command line still crashes now and then on a
        # file that slices fine the next time, a crash gets two more tries.
        for attempt in range(1, CRASH_TRIES + 1):
            proc, result = run_slicer(args, work, out)
            crashed = proc.returncode < 0 or proc.returncode == 139
            if not crashed or attempt == CRASH_TRIES:
                break
            log.warning("job %s: slicer crashed (%s), retrying", job.id, proc.returncode)
            job.message = f"Slicing... (retry {attempt})"
        job.log += (proc.stdout or "") + (proc.stderr or "")
        code = result.get("return_code", proc.returncode)
        if code != 0 or proc.returncode != 0:
            msg = result.get("error_string") or ""
            if crashed:
                msg = "the slicer crashed"
            raise RuntimeError(f"Slicer failed ({code}): {msg or 'see log'}")
        for plate in result.get("sliced_plates", []) or []:
            if plate.get("warning_message"):
                job.warnings.append(f"Plate {plate.get('id')}: {plate['warning_message']}")
        job.outputs = publish(job, printer, src, out)
        if not job.outputs:
            raise RuntimeError("The slicer finished but wrote no files")
        job.message = "Sliced: " + ", ".join(Path(o).name for o in job.outputs)
    finally:
        shutil.rmtree(work, ignore_errors=True)


CRASH_TRIES = 3


def run_slicer(args: list[str], work: Path, out: Path) -> tuple[subprocess.CompletedProcess, dict]:
    for old in out.iterdir():
        old.unlink()
    try:
        proc = subprocess.run(args, cwd=work, capture_output=True, text=True, timeout=TIMEOUT,
                              env={**os.environ, "HOME": str(work)})
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Slicing took longer than {TIMEOUT // 60} minutes and was stopped") from exc
    result = {}
    if (out / "result.json").is_file():
        try:
            result = json.loads((out / "result.json").read_text())
        except ValueError:
            result = {}
    return proc, result


def publish(job: Job, printer: Printer, src: Path, out: Path) -> list[str]:
    dest_dir = output_dir(src)
    dest_dir.mkdir(parents=True, exist_ok=True)
    produced: list[tuple[Path, str]] = []
    if printer.output == "gcode.3mf":
        f = out / "result.gcode.3mf"
        if f.is_file():
            produced.append((f, output_name(src, printer, None)))
    else:
        plates = sorted(out.glob("plate_*.gcode"), key=lambda p: int(re.sub(r"\D", "", p.stem) or 0))
        for f in plates:
            n = int(re.sub(r"\D", "", f.stem) or 1)
            produced.append((f, output_name(src, printer, n if len(plates) > 1 or job.plate else None)))
    written = []
    for f, name in produced:
        target = dest_dir / name
        tmp = dest_dir / f".{name}.part"
        shutil.copyfile(f, tmp)
        os.chmod(tmp, 0o664)
        os.replace(tmp, target)
        written.append(target.relative_to(LIBRARY).as_posix())
    return written


# ---------------------------------------------------------------- HTTP

def slicer_version() -> str:
    return VERSION


class Handler(BaseHTTPRequestHandler):
    server_version = "PrintPantrySlicer/1"

    def log_message(self, fmt, *args):  # quieter access log
        log.debug("%s " + fmt, self.address_string(), *args)

    def _send(self, code: int, data) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route(self, method: str):
        url = urlparse(self.path)
        qs = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            if method == "GET" and url.path == "/health":
                lib = PRESETS.get()
                return self._send(200, {
                    "ok": True, "slicer": "OrcaSlicer", "version": slicer_version(),
                    "printers": [{"id": p.id, "name": p.name} for p in PRINTERS.values()],
                    "system_presets": len(lib.system), "user_presets": len(lib.user),
                    "problems": lib.problems, "busy": any(j.state == "running" for j in JOBS.values()),
                })
            if method == "GET" and url.path == "/options":
                return self._send(200, options(qs.get("printer", "u1"), qs.get("path", "")))
            if method == "GET" and url.path == "/jobs":
                jobs = sorted(JOBS.values(), key=lambda j: j.created, reverse=True)[:20]
                return self._send(200, {"jobs": [j.to_json() for j in jobs]})
            m = re.fullmatch(r"/jobs/([0-9a-f]+)", url.path)
            if method == "GET" and m:
                job = JOBS.get(m.group(1))
                return self._send(200, job.to_json()) if job else self._send(404, {"detail": "No such job"})
            if method == "POST" and url.path == "/jobs":
                length = int(self.headers.get("Content-Length") or 0)
                if length > 64 * 1024:
                    raise BadRequest("Request too large")
                body = json.loads(self.rfile.read(length) or b"{}")
                return self._send(202, submit(body).to_json())
            return self._send(404, {"detail": "Not found"})
        except BadRequest as exc:
            return self._send(400, {"detail": str(exc)})
        except (ValueError, OSError, P.PresetError) as exc:
            log.exception("request failed")
            return self._send(400, {"detail": str(exc)})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    threading.Thread(target=worker, name="slicer-worker", daemon=True).start()
    threading.Thread(target=PRESETS.get, daemon=True).start()  # warm the preset cache
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log.info("slicer service on :%d (OrcaSlicer %s)", PORT, VERSION)
    server.serve_forever()


if __name__ == "__main__":
    main()
