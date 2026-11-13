"""Print history: one row per print job, on every printer that can tell us.

Where rows come from:

* Prints started from Print Pantry get a row the moment they start, with the
  library file, the model and the toolhead map used.
* Every printer driver that offers ``job_history()`` (Moonraker on the U1) is
  synced in the background. Jobs started from the touchscreen or a desktop
  slicer show up too, and a Print Pantry row is matched to its Moonraker job by
  file name and start time. The first sync of a printer imports its whole
  history (marked ``backfill``).
* When a job ends (success, failed, cancelled) the camera is asked for a
  picture of the result, and anyone registered with ``on_finish`` is told once.
  The spool thread uses that hook to charge filament and write grams and cost
  back with ``set_usage``.

People can change the outcome (for example mark a "completed" print as failed
because it came off the bed) and add a note. A status set by hand is never
overwritten by a sync.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .printers.base import BUSY_STATES
from .scanner import group_key

log = logging.getLogger("printpantry.history")

SCHEMA = """
CREATE TABLE IF NOT EXISTS prints (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    printer     TEXT NOT NULL,
    job_id      TEXT,
    source      TEXT NOT NULL DEFAULT 'printer',  -- app | printer
    file        TEXT NOT NULL DEFAULT '',
    file_id     TEXT,
    model_id    TEXT,
    started     REAL,
    ended       REAL,
    duration    REAL,               -- seconds actually printing
    total       REAL,               -- seconds from start to end, pauses included
    status      TEXT NOT NULL DEFAULT 'printing',  -- printing | success | failed | cancelled
    status_by_user INTEGER NOT NULL DEFAULT 0,
    printer_status TEXT,            -- the printer's own word, e.g. completed, klippy_shutdown
    progress    REAL,
    note        TEXT NOT NULL DEFAULT '',
    photo       TEXT,               -- file name in <data>/history
    filament    TEXT NOT NULL DEFAULT '[]',
    tool_map    TEXT,
    grams       REAL,
    cost        REAL,
    usage       TEXT,               -- whatever the spool code wrote back (JSON)
    backfill    INTEGER NOT NULL DEFAULT 0,
    finished    INTEGER NOT NULL DEFAULT 0,  -- end-of-print work (photo) done
    notified    INTEGER NOT NULL DEFAULT 0,  -- on_finish listeners told
    created     REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS prints_job ON prints(printer, job_id);
CREATE INDEX IF NOT EXISTS prints_model ON prints(model_id);
CREATE INDEX IF NOT EXISTS prints_started ON prints(started);
"""

DONE = ("success", "failed", "cancelled")
STATUSES = ("printing", *DONE)
# Moonraker job status -> ours
JOB_STATUS = {
    "completed": "success",
    "cancelled": "cancelled",
    "in_progress": "printing",
}
PHOTO_MAX = 10 * 1024 * 1024
MATCH_WINDOW = 15 * 60  # an app row and a printer job this close in start time are the same print
FRESH = 30 * 60  # only take an end-of-print photo if the job ended this recently
SYNC_EVERY = 60.0
TICK = 4.0
_TIME_SUFFIX = re.compile(r"[ _-]+(\d+d)?(\d+h)?(\d+m)?(\d+s)?$", re.I)


def _norm_job_name(name: str) -> str:
    """'Body7_PLA_6m30s.gcode' -> 'body7'; 'X - X(4)_PLA_49m10s.gcode' -> 'x'."""
    base = re.sub(r"\.(gcode|bgcode|gcode\.3mf|3mf)$", "", name, flags=re.I)
    base = _TIME_SUFFIX.sub("", base) or base
    return group_key(base + ".gcode")


def _split(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [v.strip() for v in value.split(";")]
    if value is None:
        return []
    return [value]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def job_filament(meta: dict, tool_map: dict[int, int] | None) -> list[dict]:
    """Per-filament use from Moonraker's file metadata (full-file amounts)."""
    grams = [_num(g) for g in _split(meta.get("filament_weight"))]
    mm = [_num(g) for g in _split(meta.get("filament_used_mm"))]
    types = _split(meta.get("filament_type"))
    colours = _split(meta.get("filament_colour"))
    n = max(len(grams), len(mm))
    if not n and meta.get("filament_weight_total"):
        grams, n = [_num(meta["filament_weight_total"])], 1
    out = []
    for i in range(n):
        g = grams[i] if i < len(grams) else None
        m = mm[i] if i < len(mm) else None
        if n > 1 and not g and not m:
            continue  # unused slot of a multi-filament file
        colour = str(colours[i] if i < len(colours) else (colours[0] if len(colours) == 1 else "") or "")
        colour = colour[:7].upper() if re.fullmatch(r"#[0-9A-Fa-f]{6,8}", colour) else ""
        out.append({
            "slot": i,
            "toolhead": (tool_map or {}).get(i, i),
            "type": str(types[i] if i < len(types) else (types[0] if len(types) == 1 else "") or ""),
            "color": colour,
            "grams": g,
            "mm": m,
        })
    return out


def file_filament(f, tool_map: dict[int, int] | None) -> list[dict]:
    """Per-filament use from a library file's first plate."""
    plates = f.info.plates if f and f.info else []
    if not plates:
        return []
    return [{
        "slot": fl.slot - 1,
        "toolhead": (tool_map or {}).get(fl.slot - 1, fl.slot - 1),
        "type": fl.type,
        "color": fl.color,
        "grams": fl.grams,
        "mm": None,
    } for fl in plates[0].filaments]


class Patch(BaseModel):
    status: str | None = None
    note: str | None = None
    model_id: str | None = None


class History:
    def __init__(self, db_path: Path, folder: Path, hub=None, library: Callable | None = None, store=None):
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        self.hub = hub
        self.library = library or (lambda: None)
        self.store = store
        self._lock = threading.RLock()
        self._sync_lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._db.commit()
        self._listeners: list[Callable[[dict], None]] = []
        self._stop = threading.Event()
        self._last_sync: dict[str, float] = {}
        self._last_state: dict[str, str] = {}

    # ------------------------------------------------------------ hooks for other parts of the app
    def on_finish(self, fn: Callable[[dict], None]) -> None:
        """``fn(record)`` is called once per finished job (also for backfilled
        ones, with ``record["backfill"] = True``). If it raises, it is called
        again on the next sync."""
        self._listeners.append(fn)
        self._notify_pending()

    def set_usage(self, print_id: int, grams: float | None = None, cost: float | None = None,
                  filament: list | None = None, **extra) -> dict:
        """Spool/cost code writes its numbers back here."""
        row = self.get(print_id)
        if not row:
            raise KeyError(print_id)
        usage = {**(row.get("usage") or {}), **extra}
        if filament is not None:
            usage["filament"] = filament
        with self._lock:
            self._db.execute(
                "UPDATE prints SET grams = ?, cost = ?, usage = ? WHERE id = ?",
                (grams, cost, json.dumps(usage), print_id),
            )
            self._db.commit()
        return self.get(print_id)

    # ------------------------------------------------------------ reading
    def _row(self, r: sqlite3.Row) -> dict:
        d = dict(r)
        for k in ("filament", "tool_map", "usage"):
            try:
                d[k] = json.loads(d[k]) if d[k] else ([] if k == "filament" else None)
            except ValueError:
                d[k] = None
        if d.get("tool_map"):
            d["tool_map"] = {int(k): v for k, v in d["tool_map"].items()}
        d["backfill"] = bool(d["backfill"])
        d["status_by_user"] = bool(d["status_by_user"])
        d["job_key"] = f"{d['printer']}:{d['job_id']}" if d.get("job_id") else f"{d['printer']}:pp{d['id']}"
        d["photo_url"] = f"/api/history/{d['id']}/photo?v={d['photo']}" if d.get("photo") else None
        d["toolheads"] = sorted({f["toolhead"] for f in d["filament"] if f.get("toolhead") is not None})
        for k in ("finished", "notified"):
            d.pop(k, None)
        return d

    def get(self, print_id: int) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM prints WHERE id = ?", (print_id,)).fetchone()
        return self._row(r) if r else None

    def list(self, model_id: str = "", printer: str = "", status: str = "", limit: int = 100,
             offset: int = 0, hide_empty: bool = False) -> dict:
        where, args = [], []
        if model_id:
            where.append("model_id = ?")
            args.append(model_id)
        if printer:
            where.append("printer = ?")
            args.append(printer)
        if status:
            where.append("status = ?")
            args.append(status)
        if hide_empty:  # cancelled before anything was printed
            where.append("NOT (status = 'cancelled' AND COALESCE(duration, 0) < 1)")
        sql = (" WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM prints{sql} ORDER BY COALESCE(started, created) DESC, id DESC LIMIT ? OFFSET ?",
                (*args, max(1, min(limit, 1000)), max(0, offset)),
            ).fetchall()
            agg = self._db.execute(
                "SELECT COUNT(*), SUM(status = 'success'), SUM(status = 'failed'), SUM(status = 'cancelled'),"
                f" SUM(COALESCE(duration, 0)), SUM(grams), SUM(cost), MAX(started) FROM prints{sql}",
                args,
            ).fetchone()
        return {
            "total": agg[0],
            "stats": {
                "count": agg[0], "success": agg[1] or 0, "failed": agg[2] or 0, "cancelled": agg[3] or 0,
                "seconds": agg[4] or 0, "grams": agg[5], "cost": agg[6], "last": agg[7],
            },
            "prints": [self._row(r) for r in rows],
        }

    # ------------------------------------------------------------ editing
    def patch(self, print_id: int, p: Patch) -> dict:
        row = self.get(print_id)
        if not row:
            raise KeyError(print_id)
        sets, args = [], []
        if p.status is not None:
            if p.status not in DONE:
                raise ValueError("Status must be success, failed or cancelled")
            sets += ["status = ?", "status_by_user = 1"]
            args.append(p.status)
        if p.note is not None:
            sets.append("note = ?")
            args.append(p.note.strip()[:2000])
        if p.model_id is not None:
            sets.append("model_id = ?")
            args.append(p.model_id or None)
        if sets:
            with self._lock:
                self._db.execute(f"UPDATE prints SET {', '.join(sets)} WHERE id = ?", (*args, print_id))
                self._db.commit()
        return self.get(print_id)

    def save_photo(self, print_id: int, data: bytes) -> dict:
        if data.startswith(b"\xff\xd8"):
            ext = "jpg"
        elif data.startswith(b"\x89PNG\r\n\x1a\n"):
            ext = "png"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            ext = "webp"
        else:
            raise ValueError("Expected a JPEG, PNG or WebP picture")
        if len(data) > PHOTO_MAX:
            raise ValueError("Picture is over 10 MB")
        row = self.get(print_id)
        if not row:
            raise KeyError(print_id)
        name = f"{print_id}-{int(time.time())}.{ext}"
        (self.folder / name).write_bytes(data)
        if row.get("photo"):
            (self.folder / row["photo"]).unlink(missing_ok=True)
        with self._lock:
            self._db.execute("UPDATE prints SET photo = ? WHERE id = ?", (name, print_id))
            self._db.commit()
        return self.get(print_id)

    def take_photo(self, print_id: int) -> dict:
        row = self.get(print_id)
        if not row:
            raise KeyError(print_id)
        drv = self.hub.get(row["printer"]) if self.hub else None
        if not drv or not (drv.can_camera and drv.configured):
            raise ValueError("That printer has no camera")
        data, _ = drv.snapshot()
        return self.save_photo(print_id, data)

    # ------------------------------------------------------------ model matching
    def model_for_file(self, file_id: str) -> str | None:
        lib = self.library()
        if not lib:
            return None
        for m in lib.models.values():
            if any(f.id == file_id for f in m.files):
                return m.id
        return None

    def match_file(self, name: str) -> tuple[str | None, str | None]:
        """(file_id, model_id) for a file name the printer reports."""
        lib = self.library()
        if not lib or not name:
            return None, None
        base = name.rsplit("/", 1)[-1]
        low = base.lower()
        owner = {f.id: m.id for m in lib.models.values() for f in m.files}
        for f in lib.files.values():
            if f.kind in ("gcode", "sliced3mf") and f.name.lower() == low:
                return f.id, owner.get(f.id)
        key = _norm_job_name(base)
        if not key:
            return None, None
        keys = {key}
        if " - " in base:  # "Part - Part(4)_PLA_49m10s.gcode" from a multi-object plate
            keys.add(_norm_job_name(base.split(" - ", 1)[0] + ".gcode"))
        for m in lib.models.values():
            if group_key(m.name + ".stl") in keys:
                return None, m.id
        for m in lib.models.values():
            for f in m.files:
                if f.kind in ("stl", "project3mf", "gcode", "sliced3mf") and (
                    group_key(f.name) in keys or _norm_job_name(f.name) in keys
                ):
                    return None, m.id
        return None, None

    def rematch(self) -> int:
        """Give printer jobs that had no model a second chance after a rescan."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, file FROM prints WHERE model_id IS NULL AND file != ''"
            ).fetchall()
        n = 0
        for r in rows:
            fid, mid = self.match_file(r["file"])
            if mid:
                with self._lock:
                    self._db.execute(
                        "UPDATE prints SET model_id = ?, file_id = COALESCE(file_id, ?) WHERE id = ?",
                        (mid, fid, r["id"]),
                    )
                n += 1
        if n:
            with self._lock:
                self._db.commit()
        return n

    # ------------------------------------------------------------ recording
    def record_start(self, printer_id: str, f, remote: str | None, tool_map: dict[int, int] | None) -> int:
        """A print Print Pantry just started."""
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO prints (printer, source, file, file_id, model_id, started, status, filament,"
                " tool_map, created) VALUES (?, 'app', ?, ?, ?, ?, 'printing', ?, ?, ?)",
                (printer_id, remote or f.name, f.id, self.model_for_file(f.id), now,
                 json.dumps(file_filament(f, tool_map)),
                 json.dumps({str(k): v for k, v in (tool_map or {}).items()}), now),
            )
            self._db.commit()
            pid = cur.lastrowid
        log.info("history: %s started %s (#%d)", printer_id, f.name, pid)
        return pid

    def upsert_job(self, printer_id: str, job: dict, backfill: bool = False) -> int | None:
        job_id = str(job.get("job_id") or "")
        if not job_id:
            return None
        name = job.get("filename") or ""
        start = _num(job.get("start_time"))
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM prints WHERE printer = ? AND job_id = ?", (printer_id, job_id)
            ).fetchone()
            if row is None and start is not None:
                row = self._db.execute(
                    "SELECT * FROM prints WHERE printer = ? AND job_id IS NULL AND source = 'app' AND file = ?"
                    " AND ABS(started - ?) < ? ORDER BY ABS(started - ?) LIMIT 1",
                    (printer_id, name, start, MATCH_WINDOW, start),
                ).fetchone()
        old = dict(row) if row else None

        raw = job.get("status") or ""
        status = JOB_STATUS.get(raw, "failed" if raw else "printing")
        meta = job.get("metadata") or {}
        tool_map = None
        if old and old.get("tool_map"):
            tool_map = {int(k): v for k, v in json.loads(old["tool_map"]).items()}
        filament = job_filament(meta, tool_map)
        if not filament and old:
            filament = json.loads(old["filament"] or "[]")
        used, total = _num(job.get("filament_used")), _num(meta.get("filament_total"))
        if status == "success":
            progress = 1.0
        elif used is not None and total:
            progress = max(0.0, min(1.0, used / total))
        else:
            progress = None
        ended = _num(job.get("end_time"))
        fields = {
            "job_id": job_id,
            "file": name,
            "started": start,
            "ended": ended,
            "duration": _num(job.get("print_duration")),
            "total": _num(job.get("total_duration")),
            "printer_status": raw,
            "progress": progress,
            "filament": json.dumps(filament),
        }
        if not (old and old["status_by_user"]):
            fields["status"] = status
        if old is None:
            fid, mid = self.match_file(name)
            fields.update(printer=printer_id, source="printer", file_id=fid, model_id=mid,
                          backfill=int(backfill), created=time.time())
            cols = ", ".join(fields)
            with self._lock:
                cur = self._db.execute(
                    f"INSERT INTO prints ({cols}) VALUES ({', '.join('?' * len(fields))})", tuple(fields.values())
                )
                self._db.commit()
                pid = cur.lastrowid
        else:
            pid = old["id"]
            with self._lock:
                self._db.execute(
                    f"UPDATE prints SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                    (*fields.values(), pid),
                )
                self._db.commit()
        if status in DONE:
            self._finish(pid, ended)
        return pid

    def _finish(self, pid: int, ended: float | None) -> None:
        """End-of-print work, once: a photo of the bed, then tell listeners."""
        with self._lock:
            r = self._db.execute("SELECT finished, backfill, printer FROM prints WHERE id = ?", (pid,)).fetchone()
        if not r or r["finished"]:
            return
        with self._lock:
            self._db.execute("UPDATE prints SET finished = 1 WHERE id = ?", (pid,))
            self._db.commit()
        if not r["backfill"] and (ended is None or time.time() - ended < FRESH):
            try:
                self.take_photo(pid)
            except Exception as exc:  # noqa: BLE001 - no picture is fine
                log.info("history: no end-of-print photo for #%d: %s", pid, exc)
        self._notify_pending()

    def _notify_pending(self) -> None:
        if not self._listeners:
            return
        with self._lock:
            ids = [r[0] for r in self._db.execute(
                "SELECT id FROM prints WHERE finished = 1 AND notified = 0 ORDER BY id"
            ).fetchall()]
        for pid in ids:
            rec = self.get(pid)
            ok = True
            for fn in self._listeners:
                try:
                    fn(rec)
                except Exception:  # noqa: BLE001 - try again next sync
                    ok = False
                    log.exception("history: on_finish listener failed for #%d", pid)
            if ok:
                with self._lock:
                    self._db.execute("UPDATE prints SET notified = 1 WHERE id = ?", (pid,))
                    self._db.commit()

    # ------------------------------------------------------------ syncing printers
    def sync(self, printer_id: str | None = None) -> int:
        """Pull job history from every printer that has one. Returns jobs seen."""
        if not self.hub:
            return 0
        seen = 0
        with self._sync_lock:
            for pid, drv in self.hub.printers.items():
                if printer_id and pid != printer_id:
                    continue
                if not drv.configured or not hasattr(drv, "job_history"):
                    continue
                key = f"history_synced_{pid}"
                first = not (self.store.get(key) if self.store else self._last_sync.get(pid))
                try:
                    jobs = drv.job_history(limit=1000 if first else 30)
                except Exception as exc:  # noqa: BLE001 - offline printer
                    log.debug("history: %s job history unavailable: %s", pid, exc)
                    continue
                for job in sorted(jobs, key=lambda j: _num(j.get("start_time")) or 0):
                    try:
                        self.upsert_job(pid, job, backfill=first)
                        seen += 1
                    except Exception:  # noqa: BLE001
                        log.exception("history: couldn't record job %s", job.get("job_id"))
                self._last_sync[pid] = time.monotonic()
                if first:
                    if self.store:
                        self.store.set(key, True)
                    log.info("history: imported %d past jobs from %s", len(jobs), pid)
            self.rematch()
            self._notify_pending()
        return seen

    def tick(self) -> None:
        """Sync a printer now if its state changed, otherwise every SYNC_EVERY seconds."""
        if not self.hub:
            return
        now = time.monotonic()
        for pid, drv in self.hub.printers.items():
            if not drv.configured or not hasattr(drv, "job_history"):
                continue
            state = drv.status.state
            prev = self._last_state.get(pid)
            self._last_state[pid] = state
            changed = prev is not None and (prev in BUSY_STATES) != (state in BUSY_STATES)
            due = now - self._last_sync.get(pid, 0) > (SYNC_EVERY if state in BUSY_STATES else SYNC_EVERY * 5)
            if (changed or due or pid not in self._last_sync) and drv.status.online:
                self.sync(pid)

    def start(self) -> None:
        def loop():
            while not self._stop.wait(TICK):
                try:
                    self.tick()
                except Exception:  # noqa: BLE001
                    log.exception("history sync failed")
        threading.Thread(target=loop, name="history", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


def make_router(history: History) -> APIRouter:
    router = APIRouter(prefix="/api/history")

    def one(print_id: int) -> dict:
        row = history.get(print_id)
        if not row:
            raise HTTPException(404, "No such print")
        return row

    @router.get("")
    def list_prints(model_id: str = "", printer: str = "", status: str = "", limit: int = 100,
                    offset: int = 0, hide_empty: bool = False):
        return history.list(model_id, printer, status, limit, offset, hide_empty)

    @router.post("/sync")
    def sync():
        return {"jobs": history.sync()}

    @router.get("/{print_id}")
    def get(print_id: int):
        return one(print_id)

    @router.patch("/{print_id}")
    def patch(print_id: int, p: Patch):
        one(print_id)
        try:
            return history.patch(print_id, p)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/{print_id}/photo")
    def photo(print_id: int):
        row = one(print_id)
        if not row.get("photo"):
            raise HTTPException(404, "No picture")
        path = history.folder / row["photo"]
        if not path.is_file():
            raise HTTPException(404, "No picture")
        return FileResponse(path, headers={"Cache-Control": "private, max-age=31536000"})

    @router.put("/{print_id}/photo")
    async def upload(print_id: int, request: Request):
        one(print_id)
        body = await request.body()
        try:
            return history.save_photo(print_id, body)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/{print_id}/photo")
    def snap(print_id: int):
        one(print_id)
        try:
            return history.take_photo(print_id)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(503, f"Couldn't get a picture: {exc}") from exc

    return router
