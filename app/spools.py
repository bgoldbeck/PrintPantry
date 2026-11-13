"""Spool inventory and cost per print.

Print Pantry can't weigh a spool, so this is bookkeeping:

* A spool is added once with how much filament is on it (1000 g for a new
  1 kg roll) and what it cost. A used roll can be weighed on a kitchen scale;
  the empty spool's own weight is subtracted.
* When a print ends, the grams the slicer worked out for each filament are
  taken off the spool in the toolhead that printed it. Cancelled and failed
  prints are charged by how far they got. Print history calls
  :meth:`Spools.on_print_finished` for every finished job.
* Spools follow the printer: each toolhead's material, colour and brand (the
  U1 reads them from the spool's RFID tag) are compared with the spools on
  the shelf. A toolhead with no spool gets the matching one; when the printer
  reports something different for a toolhead (spool swapped), its spool goes
  back on the shelf. A spool can also be put on a toolhead by hand.

Cost = grams x the spool's price per gram, plus optional electricity
(printer watts x hours x price per kWh).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

log = logging.getLogger("printpantry.spools")

SCHEMA = """
CREATE TABLE IF NOT EXISTS spools (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL DEFAULT '',
    vendor      TEXT NOT NULL DEFAULT '',
    material    TEXT NOT NULL DEFAULT '',
    color       TEXT NOT NULL DEFAULT '',
    total_g     REAL NOT NULL,          -- filament on a full spool
    remaining_g REAL NOT NULL,
    empty_g     REAL NOT NULL DEFAULT 200,  -- the empty spool's own weight, for weigh-ins
    price       REAL,                   -- what the whole spool cost
    printer     TEXT,                   -- where it is loaded now, NULL = on the shelf
    toolhead    INTEGER,                -- 0-based
    seen        TEXT,                   -- what the printer reported when it was put there
    auto        INTEGER NOT NULL DEFAULT 1,  -- may be put on a toolhead automatically
    archived    INTEGER NOT NULL DEFAULT 0,
    note        TEXT NOT NULL DEFAULT '',
    created     REAL NOT NULL,
    updated     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS spool_charges (
    job_key TEXT PRIMARY KEY,
    printer TEXT NOT NULL,
    at      REAL NOT NULL,
    result  TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "currency": "$",
    "price_per_kg": 20.0,  # used for spools without a price and for estimates with no matching spool
    "kwh_price": 0.0,  # 0 = leave electricity out
    "watts": {"u1": 150.0, "p1s": 110.0},  # average draw while printing
    "low_grams": 100.0,  # spools below this show as running low
}


def _material(value: str) -> str:
    return "".join(ch for ch in (value or "").upper() if ch.isalnum())


def same_material(a: str, b: str) -> bool:
    x, y = _material(a), _material(b)
    if not x or not y:
        return True
    return x.startswith(y) or y.startswith(x)


def _rgb(c: str) -> tuple[int, int, int] | None:
    c = (c or "").strip().lstrip("#")
    if len(c) < 6:
        return None
    try:
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except ValueError:
        return None


def color_distance(a: str, b: str) -> float | None:
    x, y = _rgb(a), _rgb(b)
    if not x or not y:
        return None
    return sum((p - q) ** 2 for p, q in zip(x, y)) ** 0.5


def _sig(t) -> str:
    return f"{t.type}|{t.color}|{t.vendor}|{t.loaded}"


class SpoolIn(BaseModel):
    name: str | None = None
    vendor: str | None = None
    material: str | None = None
    color: str | None = None
    total_g: float | None = None
    remaining_g: float | None = None
    empty_g: float | None = None
    price: float | None = None
    auto: bool | None = None
    archived: bool | None = None
    note: str | None = None


class WeighIn(BaseModel):
    gross_g: float | None = None  # spool on the scale, empty spool included
    remaining_g: float | None = None  # or the filament left, if known


class AssignIn(BaseModel):
    printer: str | None = None  # None = take it off the printer
    toolhead: int | None = None


class SettingsIn(BaseModel):
    currency: str | None = None
    price_per_kg: float | None = None
    kwh_price: float | None = None
    watts: dict[str, float] | None = None
    low_grams: float | None = None


class Spools:
    def __init__(self, db_path: Path, store, hub=None):
        self.store = store
        self.hub = hub
        self._lock = threading.RLock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._db.commit()

    # ------------------------------------------------------------ settings
    def settings(self) -> dict:
        saved = self.store.get("spool_settings", {}) or {}
        out = {**DEFAULT_SETTINGS, **saved}
        out["watts"] = {**DEFAULT_SETTINGS["watts"], **(saved.get("watts") or {})}
        return out

    def update_settings(self, patch: SettingsIn) -> dict:
        s = self.settings()
        for k, v in patch.model_dump(exclude_none=True).items():
            if k == "watts":
                for pid, w in v.items():
                    if w < 0 or w > 5000:
                        raise ValueError("Watts must be between 0 and 5000")
                    s["watts"][pid] = float(w)
            elif k == "currency":
                s[k] = v.strip()[:4]
            else:
                if v < 0:
                    raise ValueError(f"{k} can't be negative")
                s[k] = float(v)
        self.store.set("spool_settings", s)
        return s

    # ------------------------------------------------------------ spools
    def _row(self, r: sqlite3.Row) -> dict:
        d = dict(r)
        d["auto"] = bool(d["auto"])
        d["archived"] = bool(d["archived"])
        d.pop("seen", None)
        s = self.settings()
        d["price_per_kg"] = self._per_kg(r, s)
        d["low"] = d["remaining_g"] < s["low_grams"]
        d["label"] = self._label(d)
        return d

    @staticmethod
    def _label(d) -> str:
        return d.get("name") or " ".join(x for x in (d.get("vendor"), d.get("material")) if x) or f"Spool {d['id']}"

    @staticmethod
    def _per_kg(r, s) -> float:
        if r["price"] is not None and r["total_g"]:
            return r["price"] / r["total_g"] * 1000
        return s["price_per_kg"]

    def list(self, include_archived: bool = False) -> list[dict]:
        self.sync()
        q = "SELECT * FROM spools" + ("" if include_archived else " WHERE archived = 0") + " ORDER BY archived, printer IS NULL, printer, toolhead, id"
        with self._lock:
            return [self._row(r) for r in self._db.execute(q)]

    def get(self, sid: int) -> dict:
        with self._lock:
            r = self._db.execute("SELECT * FROM spools WHERE id = ?", (sid,)).fetchone()
        if not r:
            raise KeyError(sid)
        return self._row(r)

    def create(self, data: SpoolIn) -> dict:
        d = data.model_dump(exclude_none=True)
        total = float(d.get("total_g") or 1000)
        if total <= 0:
            raise ValueError("Filament on a full spool must be more than 0 g")
        remaining = float(d.get("remaining_g", total))
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO spools (name, vendor, material, color, total_g, remaining_g, empty_g, price, auto, note, created, updated)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (d.get("name", "").strip(), d.get("vendor", "").strip(), d.get("material", "").strip().upper(),
                 _norm_color(d.get("color", "")), total, max(0.0, remaining), float(d.get("empty_g", 200)),
                 d.get("price"), int(d.get("auto", True)), d.get("note", ""), now, now),
            )
            self._db.commit()
            sid = cur.lastrowid
        self.sync()
        return self.get(sid)

    def update(self, sid: int, data: SpoolIn) -> dict:
        self.get(sid)
        d = data.model_dump(exclude_unset=True)
        sets, vals = [], []
        for k, v in d.items():
            if k in ("auto", "archived"):
                v = int(bool(v))
            elif k == "material":
                v = (v or "").strip().upper()
            elif k == "color":
                v = _norm_color(v or "")
            elif k in ("name", "vendor", "note"):
                v = (v or "").strip()
            elif k in ("total_g", "remaining_g", "empty_g"):
                if v is None or v < 0:
                    raise ValueError(f"{k} must be 0 or more")
            elif k == "price" and v is not None and v < 0:
                raise ValueError("Price can't be negative")
            sets.append(f"{k} = ?")
            vals.append(v)
        if d.get("archived"):
            sets += ["printer = NULL", "toolhead = NULL", "seen = NULL"]
        if sets:
            with self._lock:
                self._db.execute(f"UPDATE spools SET {', '.join(sets)}, updated = ? WHERE id = ?", (*vals, time.time(), sid))
                self._db.commit()
        return self.get(sid)

    def delete(self, sid: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM spools WHERE id = ?", (sid,))
            self._db.commit()

    def weigh(self, sid: int, w: WeighIn) -> dict:
        sp = self.get(sid)
        if w.remaining_g is not None:
            left = w.remaining_g
        elif w.gross_g is not None:
            left = w.gross_g - sp["empty_g"]
        else:
            raise ValueError("Give the scale reading or the grams left")
        left = max(0.0, round(left, 1))
        with self._lock:
            self._db.execute("UPDATE spools SET remaining_g = ?, updated = ? WHERE id = ?", (left, time.time(), sid))
            self._db.commit()
        log.info("spool %s weighed: %.1f g left (was %.1f)", sid, left, sp["remaining_g"])
        return self.get(sid)

    # ------------------------------------------------------------ toolheads
    def _heads(self, printer_id: str) -> dict:
        p = self.hub.get(printer_id) if self.hub else None
        if not p or not p.status.online:
            return {}
        return {t.index: t for t in p.status.toolheads}

    def assign(self, sid: int, a: AssignIn) -> dict:
        self.get(sid)
        with self._lock:
            if a.printer is None:
                # Off the printer by hand: remember where from, so the next sync
                # doesn't put it straight back on the same toolhead.
                cur = self._db.execute("SELECT printer, toolhead, seen FROM spools WHERE id = ?", (sid,)).fetchone()
                mark = f"off|{cur['printer']}|{cur['toolhead']}|{cur['seen']}" if cur["printer"] else None
                self._db.execute("UPDATE spools SET printer = NULL, toolhead = NULL, seen = ?, updated = ? WHERE id = ?",
                                 (mark, time.time(), sid))
                self._db.commit()
                return self.get(sid)
            if a.toolhead is None or a.toolhead < 0:
                raise ValueError("Pick a toolhead")
            if self.hub and not self.hub.get(a.printer):
                raise ValueError("No such printer")
            head = self._heads(a.printer).get(a.toolhead)
            seen = _sig(head) if head else None
            self._db.execute("UPDATE spools SET printer = NULL, toolhead = NULL, seen = NULL WHERE printer = ? AND toolhead = ?",
                             (a.printer, a.toolhead))
            self._db.execute("UPDATE spools SET printer = ?, toolhead = ?, seen = ?, archived = 0, updated = ? WHERE id = ?",
                             (a.printer, a.toolhead, seen, time.time(), sid))
            self._db.commit()
        return self.get(sid)

    def sync(self) -> None:
        """Follow spool swaps on every online printer."""
        if not self.hub:
            return
        for pid in list(self.hub.printers):
            try:
                self._sync_printer(pid)
            except Exception as exc:  # noqa: BLE001 - never break a page over this
                log.warning("spool sync for %s failed: %s", pid, exc)

    def _sync_printer(self, pid: str) -> None:
        heads = self._heads(pid)
        if not heads:
            return
        with self._lock:
            changed = False
            rows = {r["toolhead"]: r for r in self._db.execute("SELECT * FROM spools WHERE printer = ?", (pid,))}
            for idx, r in rows.items():
                t = heads.get(idx)
                if t is None:
                    continue
                sig = _sig(t)
                if r["seen"] is None:
                    self._db.execute("UPDATE spools SET seen = ? WHERE id = ?", (sig, r["id"]))
                    changed = True
                elif r["seen"] != sig:
                    log.info("%s T%d changed (%s -> %s); spool %s back on the shelf", pid, idx + 1, r["seen"], sig, r["id"])
                    self._db.execute("UPDATE spools SET printer = NULL, toolhead = NULL, seen = NULL, updated = ? WHERE id = ?",
                                     (time.time(), r["id"]))
                    rows[idx] = None
                    changed = True
            free = [dict(r) for r in self._db.execute(
                "SELECT * FROM spools WHERE printer IS NULL AND archived = 0 AND auto = 1 AND remaining_g > 0")]
            for idx, t in sorted(heads.items()):
                if rows.get(idx) is not None or t.loaded is False or not t.type:
                    continue
                skip = f"off|{pid}|{idx}|{_sig(t)}"
                best = self._best_match(t, [s for s in free if s["seen"] != skip])
                if best is None:
                    continue
                free.remove(best)
                log.info("%s T%d: matched spool %s (%s)", pid, idx + 1, best["id"], self._label(best))
                self._db.execute("UPDATE spools SET printer = ?, toolhead = ?, seen = ?, updated = ? WHERE id = ?",
                                 (pid, idx, _sig(t), time.time(), best["id"]))
                changed = True
            if changed:
                self._db.commit()

    @staticmethod
    def _best_match(t, free: list[dict]) -> dict | None:
        """Same material, colour within a small distance, brand if both say one.
        Among equals, the most used spool goes first (it is the one most likely
        to have been loaded already)."""
        scored = []
        for s in free:
            if not s["material"] or not same_material(s["material"], t.type):
                continue
            if s["vendor"] and t.vendor and _material(s["vendor"]) not in _material(t.vendor) \
                    and _material(t.vendor) not in _material(s["vendor"]):
                continue
            dist = color_distance(s["color"], t.color)
            if dist is None:
                if s["color"] and t.color:
                    continue
                dist = 60.0  # one side has no colour: a weak match
            if dist > 80:
                continue
            scored.append((dist, s["remaining_g"], s["id"], s))
        if not scored:
            return None
        scored.sort(key=lambda x: x[:3])
        return scored[0][3]

    def on_head(self, printer_id: str, toolhead: int) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM spools WHERE printer = ? AND toolhead = ?", (printer_id, toolhead)).fetchone()
        return self._row(r) if r else None

    # ------------------------------------------------------------ cost
    def power_cost(self, printer_id: str, seconds: float | None) -> float:
        s = self.settings()
        if not seconds or not s["kwh_price"]:
            return 0.0
        watts = s["watts"].get(printer_id, 120.0)
        return watts * seconds / 3600 / 1000 * s["kwh_price"]

    def per_kg_for(self, material: str) -> float:
        """Average price of the spools of this material, else the default."""
        s = self.settings()
        with self._lock:
            rows = self._db.execute("SELECT * FROM spools WHERE price IS NOT NULL AND total_g > 0").fetchall()
        prices = [self._per_kg(r, s) for r in rows if same_material(r["material"], material) and r["material"]]
        return sum(prices) / len(prices) if prices else s["price_per_kg"]

    def pricing(self) -> dict:
        """What the browser needs to estimate a file's cost by itself."""
        s = self.settings()
        mats: dict[str, list[float]] = {}
        with self._lock:
            rows = self._db.execute("SELECT * FROM spools WHERE price IS NOT NULL AND total_g > 0").fetchall()
        for r in rows:
            if r["material"]:
                mats.setdefault(_material(r["material"]), []).append(self._per_kg(r, s))
        return {**s, "materials": {k: sum(v) / len(v) for k, v in mats.items()}}

    def charge_job(self, job_key: str, printer_id: str, usage: list[dict], seconds: float | None = None,
                   deduct: bool = True) -> dict:
        """Take a finished job's filament off its spools, once per ``job_key``.

        ``usage`` is [{"toolhead": 0-based, "grams": float, "type": optional material}],
        already scaled to how much was printed. Returns grams and cost.
        """
        with self._lock:
            old = self._db.execute("SELECT result FROM spool_charges WHERE job_key = ?", (job_key,)).fetchone()
            if old:
                return json.loads(old[0])
            if deduct and self.hub:
                self._sync_printer(printer_id)
            s = self.settings()
            by_tool = []
            for u in usage:
                g = max(0.0, float(u.get("grams") or 0))
                idx = int(u.get("toolhead", 0))
                r = self._db.execute("SELECT * FROM spools WHERE printer = ? AND toolhead = ?", (printer_id, idx)).fetchone()
                if r:
                    per_kg = self._per_kg(r, s)
                    if deduct and g:
                        self._db.execute("UPDATE spools SET remaining_g = MAX(0, remaining_g - ?), updated = ? WHERE id = ?",
                                         (g, time.time(), r["id"]))
                    spool = {"spool_id": r["id"], "spool_name": self._label(dict(r))}
                else:
                    per_kg = self.per_kg_for(u.get("type", ""))
                    spool = {"spool_id": None, "spool_name": None}
                by_tool.append({"toolhead": idx, "grams": round(g, 2), **spool, "cost": round(g / 1000 * per_kg, 4)})
            fil = sum(b["cost"] for b in by_tool)
            power = self.power_cost(printer_id, seconds)
            result = {
                "grams": round(sum(b["grams"] for b in by_tool), 2),
                "filament_cost": round(fil, 2),
                "power_cost": round(power, 2),
                "cost": round(fil + power, 2),
                "currency": s["currency"],
                "by_tool": by_tool,
                "deducted": deduct,
            }
            self._db.execute("INSERT INTO spool_charges VALUES (?, ?, ?, ?)",
                             (job_key, printer_id, time.time(), json.dumps(result)))
            self._db.commit()
        if deduct:
            log.info("charged %s: %.1f g, %s%.2f", job_key, result["grams"], s["currency"], result["cost"])
        return result

    def on_print_finished(self, rec: dict, history=None) -> dict:
        """Print history calls this once per ended job (see app.history)."""
        # Success = the whole file. Cancelled/failed = as far as it got; when
        # that isn't known, charge it all (better to warn about a spool early).
        progress = rec.get("progress")
        if rec.get("status") == "success" or progress is None:
            frac = 1.0
        else:
            frac = max(0.0, min(1.0, float(progress)))
        usage = [
            {"toolhead": int(f.get("toolhead", f.get("slot", 0)) or 0), "grams": (f.get("grams") or 0) * frac,
             "type": f.get("type", "")}
            for f in rec.get("filament") or []
        ]
        key = rec.get("job_key") or f"{rec.get('printer')}:{rec.get('job_id') or rec.get('id')}"
        res = self.charge_job(key, rec.get("printer", ""), usage, seconds=rec.get("duration"),
                              deduct=not rec.get("backfill"))
        if history is not None and rec.get("id") is not None:
            fil = []
            src = {int(f.get("toolhead", 0) or 0): f for f in rec.get("filament") or []}
            for b in res["by_tool"]:
                fil.append({**src.get(b["toolhead"], {}), **b})
            history.set_usage(rec["id"], grams=res["grams"], cost=res["cost"], filament=fil)
        return res

    # ------------------------------------------------------------ print dialog
    def check_tools(self, printer_id: str, rows: list[dict]) -> list[str]:
        """Add the spool on each toolhead to the print dialog's rows and warn
        when it hasn't enough filament left. Rows come from printers.api.preflight."""
        self.sync()
        need: dict[int, float] = {}
        for row in rows:
            need[row["index"]] = need.get(row["index"], 0.0) + float((row.get("file") or {}).get("grams") or 0)
        warnings = []
        for row in rows:
            sp = self.on_head(printer_id, row["index"])
            if not sp:
                row["spool"] = None
                continue
            row["spool"] = {"id": sp["id"], "label": sp["label"], "remaining_g": round(sp["remaining_g"], 1)}
            n = need.pop(row["index"], None)
            if n and n > sp["remaining_g"]:
                row["ok"] = False
                warnings.append(
                    f"T{row['index'] + 1}: {sp['label']} has about {sp['remaining_g']:.0f} g left, "
                    f"but this print needs {n:.0f} g"
                )
        return warnings


def _norm_color(c: str) -> str:
    c = (c or "").strip().upper()
    if not c:
        return ""
    if not c.startswith("#"):
        c = "#" + c
    if len(c) >= 7 and all(ch in "0123456789ABCDEF" for ch in c[1:7]):
        return c[:7]
    raise ValueError("Colour must look like #RRGGBB")


def make_router(spools: Spools) -> APIRouter:
    router = APIRouter(prefix="/api/spools")

    def guard(fn, *a):
        try:
            return fn(*a)
        except KeyError:
            raise HTTPException(404, "No such spool") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("")
    def list_spools(archived: bool = False):
        return {"spools": spools.list(archived), "settings": spools.settings()}

    @router.post("")
    def create(body: SpoolIn):
        return guard(spools.create, body)

    @router.get("/pricing")
    def pricing():
        return spools.pricing()

    @router.put("/settings")
    def put_settings(body: SettingsIn):
        return guard(spools.update_settings, body)

    @router.get("/{sid}")
    def one(sid: int):
        return guard(spools.get, sid)

    @router.put("/{sid}")
    def update(sid: int, body: SpoolIn):
        return guard(spools.update, sid, body)

    @router.delete("/{sid}")
    def delete(sid: int):
        guard(spools.get, sid)
        spools.delete(sid)
        return {"ok": True}

    @router.post("/{sid}/weigh")
    def weigh(sid: int, body: WeighIn):
        return guard(spools.weigh, sid, body)

    @router.post("/{sid}/assign")
    def assign(sid: int, body: AssignIn):
        return guard(spools.assign, sid, body)

    return router
