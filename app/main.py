"""Print Pantry web app: JSON API plus the static single-page UI."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, formats
from .auth import COOKIE, MAX_AGE, Auth, LoginLimiter, client_ip, is_https
from .calibration import CalibrationStore
from .calibration import make_router as calibration_router
from .config import Settings
from .history import History
from .history import make_router as history_router
from .importer import SOURCE_FILE, Importer, ImportError_, safe_name, suggest_category
from .printers import PrinterHub
from .printers.api import make_router as printers_router
from .scanner import UNCATEGORIZED, Scanner, make_id
from .spools import Spools
from .spools import make_router as spools_router
from .slicing import SlicerClient, client_from_env
from .slicing import make_router as slicer_router
from .store import Store
from .tags import Tags, clean_tag

log = logging.getLogger("printpantry")
STATIC = Path(__file__).parent / "static"
MAX_RENDER_BYTES = 2 * 1024 * 1024


SLICERS = ("snapmaker", "bambu")


class Prefs(BaseModel):
    slicer: str | None = None


class TagsBody(BaseModel):
    tags: list[str]


class RenameBody(BaseModel):
    name: str


class CategoryChangeBody(BaseModel):
    category: str


class ImportBody(BaseModel):
    url: str
    category: str = ""
    name: str = ""
    tags: list[str] = []


class ImportSettings(BaseModel):
    makerworld_token: str = ""


class SignIn(BaseModel):
    email: str = ""
    password: str = ""
    code: str = ""
    tfa_key: str = ""
    tfa_code: str = ""


class CategoryPatch(BaseModel):
    icon: str | None = None
    color: str | None = None


def prune_thumbs(folder: Path, stls) -> int:
    """Delete stored STL previews whose STL was removed or changed."""
    keep = {f"{f.id}-{int(f.mtime)}-{f.size}.png" for f in stls}
    removed = 0
    for png in folder.glob("*.png"):
        if png.name not in keep:
            png.unlink(missing_ok=True)
            removed += 1
    if removed:
        log.info("removed %d stale STL previews", removed)
    return removed


def create_app(
    settings: Settings | None = None,
    background_scan: bool = True,
    printers: PrinterHub | None = None,
    slicer: SlicerClient | None = None,
    importer: Importer | None = None,
) -> FastAPI:
    settings = settings or Settings()
    settings.ensure_dirs()
    store = Store(settings.data / "printpantry.db")
    scanner = Scanner(settings, store)
    auth = Auth(settings.password, settings.data)
    tags = Tags(store)
    hub = printers or PrinterHub(settings.printers, data=settings.data)
    history = History(settings.data / "printpantry.db", settings.data / "history", hub,
                      lambda: scanner.library, store)
    icon_names = set()
    try:
        icon_names = set(json.loads((STATIC / "icons.json").read_text()))
    except (OSError, ValueError):
        pass
    stop = threading.Event()

    def scan_and_tidy():
        lib = scanner.scan()
        if not lib.error:
            prune_thumbs(settings.data / "thumbs", [f for f in lib.files.values() if f.kind == "stl"])

    def loop():
        scan_and_tidy()
        while settings.scan_interval > 0 and not stop.wait(settings.scan_interval):
            scan_and_tidy()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if background_scan:
            threading.Thread(target=loop, name="scanner", daemon=True).start()
            hub.start()
            history.start()
        else:
            scan_and_tidy()
        yield
        stop.set()
        hub.stop()
        history.stop()

    app = FastAPI(title="Print Pantry", version=__version__, lifespan=lifespan)
    app.state.scanner = scanner
    app.state.store = store
    app.state.settings = settings
    app.state.printers = hub
    app.state.history = history
    app.state.tags = tags

    # ------------------------------------------------------------ caching
    @app.middleware("http")
    async def revalidate_ui(request: Request, call_next):
        """Make browsers re-check our own JS/CSS on each load (a cheap 304 when
        unchanged), so a deploy shows up without a hard refresh. Vendored
        libraries keep normal caching."""
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/static/") and not path.startswith("/static/vendor/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    # ------------------------------------------------------------ auth
    limiter = LoginLimiter()
    app.state.login_limiter = limiter
    # Exact paths that work without a login, plus prefixes that check their own token.
    open_exact = {"/login", "/healthz", "/static/style.css", "/static/favicon.svg"}
    open_prefixes = ("/api/dl/",)  # slicers fetch these with a signed link, no cookie
    unsafe = {"POST", "PUT", "PATCH", "DELETE"}

    def peer(request: Request) -> str | None:
        return request.client.host if request.client else None

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = request.url.path
        # Refuse cross-site writes (CSRF): a browser always sends Origin on these,
        # and it must name this same host. Requests without Origin aren't from
        # another site's page.
        origin = request.headers.get("origin")
        if request.method in unsafe and origin:
            host = request.headers.get("host", "")
            if urlsplit(origin).netloc.lower() != host.lower():
                return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
        is_open = path in open_exact or path.startswith(open_prefixes)
        if auth.enabled and not is_open and not auth.valid(request.cookies.get(COOKIE)):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "login required"}, status_code=401)
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(error: str = ""):
        html = (STATIC / "login.html").read_text()
        msg = {
            "1": '<p class="login-error">Wrong password.</p>',
            "wait": '<p class="login-error">Too many wrong passwords. Try again in a few minutes.</p>',
        }.get(error, "")
        return html.replace("<!--ERROR-->", msg)

    @app.post("/login")
    async def login(request: Request):
        form = await request.body()
        from urllib.parse import parse_qs

        attempt = parse_qs(form.decode(errors="replace")).get("password", [""])[0]
        if not auth.enabled:
            return RedirectResponse("/", status_code=303)
        ip = client_ip(peer(request), request.headers)
        wait = limiter.retry_after(ip)
        if wait:
            log.warning("login locked out for %s (%ss left)", ip, wait)
            return RedirectResponse("/login?error=wait", status_code=303, headers={"Retry-After": str(wait)})
        if not auth.check_password(attempt):
            limiter.failed(ip)
            log.warning("wrong password from %s", ip)
            await asyncio.sleep(1)  # slow down guessing without stalling other requests
            return RedirectResponse("/login?error=1", status_code=303)
        limiter.succeeded(ip)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            COOKIE,
            auth.make_cookie(),
            max_age=MAX_AGE,
            expires=MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=is_https(request.url.scheme, peer(request), request.headers),
        )
        return resp

    @app.post("/logout")
    def logout():
        resp = RedirectResponse("/login" if auth.enabled else "/", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "version": __version__}

    # ------------------------------------------------------------ library
    def lib():
        return scanner.library

    @app.get("/api/library")
    def library():
        L = lib()
        cats = sorted(L.categories.values(), key=lambda c: (c.name == "Uncategorized", c.name.lower()))
        return {
            "version": __version__,
            "categories": [c.summary() for c in cats],
            "model_count": len(L.models),
            "file_count": len(L.files),
            "scanned_at": L.scanned_at,
            "scan_seconds": L.scan_seconds,
            "scanning": scanner.scanning,
            "error": L.error,
            "auth": auth.enabled,
            "printers": [
                {"id": p.id, "name": p.name, "color": p.color, "file_types": list(p.file_types)}
                for p in settings.printers
            ],
        }

    def summary(m) -> dict:
        return {**m.summary(), "tags": tags.get(m.id)}

    @app.get("/api/categories/{name}")
    def category(name: str):
        cat = lib().categories.get(name)
        if not cat:
            raise HTTPException(404, "No such category")
        return {**cat.summary(), "models": [summary(m) for m in cat.models]}

    @app.put("/api/categories/{name}")
    def update_category(name: str, patch: CategoryPatch):
        cat = lib().categories.get(name)
        if not cat:
            raise HTTPException(404, "No such category")
        overrides = store.get("category_overrides", {}) or {}
        ov = overrides.setdefault(name, {})
        if patch.icon is not None:
            if patch.icon not in icon_names:
                raise HTTPException(400, "Unknown icon")
            ov["icon"] = cat.icon = patch.icon
        if patch.color is not None:
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", patch.color):
                raise HTTPException(400, "Colour must look like #RRGGBB")
            ov["color"] = cat.color = patch.color
        store.set("category_overrides", overrides)
        return cat.summary()

    @app.get("/api/models")
    def models(q: str = "", printer: str = "", tag: str = "", category: str = "", sort: str = "", limit: int = 200):
        """Search the whole library.

        ``q`` matches model names, categories, tags, file names and the object
        names inside 3MFs; every word must match somewhere. ``tag:kitchen``
        inside ``q`` (or the ``tag`` parameter) keeps only models with that tag,
        ``cat:tools`` only that category. Best matches (name hits) come first.
        """
        all_tags = tags.all()
        items = list(lib().models.values())
        words, want_tags, want_cats = [], [clean_tag(tag)] if tag else [], [category.lower()] if category else []
        for w in q.lower().split():
            if w.startswith("tag:") and len(w) > 4:
                want_tags.append(clean_tag(w[4:].replace("_", " ")))
            elif w.startswith(("cat:", "category:")) and w.split(":", 1)[1]:
                want_cats.append(w.split(":", 1)[1])
            else:
                words.append(w)
        if want_tags:
            items = [m for m in items if all(t in all_tags.get(m.id, []) for t in want_tags)]
        if want_cats:
            items = [m for m in items if all(c in m.category.lower() for c in want_cats)]
        if printer:
            items = [m for m in items if any(f.printer == printer for f in m.files)]
        scores: dict[str, int] = {}
        if words:
            kept = []
            for m in items:
                name, cat = m.name.lower(), m.category.lower()
                mtags = " ".join(all_tags.get(m.id, []))
                files = " ".join(f.name.lower() for f in m.files)
                objects = " ".join(o.lower() for f in m.files if f.info for o in f.info.objects)
                score = 0
                for w in words:
                    if w in name:
                        score += 10 if name.startswith(w) or f" {w}" in name else 6
                    elif w in mtags:
                        score += 5
                    elif w in cat:
                        score += 3
                    elif w in files or w in objects:
                        score += 1
                    else:
                        break
                else:
                    kept.append(m)
                    scores[m.id] = score
            items = kept
        if sort == "recent":
            items.sort(key=lambda m: m.mtime, reverse=True)
        elif sort == "name" or not words:
            items.sort(key=lambda m: m.name.lower())
        else:
            items.sort(key=lambda m: (-scores.get(m.id, 0), m.name.lower()))
        return {"total": len(items), "models": [summary(m) for m in items[: max(1, min(limit, 1000))]]}

    @app.get("/api/models/{model_id}")
    def model(model_id: str):
        m = lib().models.get(model_id)
        if not m:
            raise HTTPException(404, "No such model")
        data = {**m.detail(), "tags": tags.get(m.id)}
        source = read_source(m)
        if source:
            data["source"] = source
        return data

    def read_source(m) -> dict | None:
        """Where an imported model came from (written by the importer)."""
        if "#" in m.relpath:
            return None
        meta = settings.library / m.relpath / SOURCE_FILE
        try:
            raw = json.loads(meta.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        keep = ("site", "url", "title", "author", "license", "summary", "imported_at")
        return {k: raw[k] for k in keep if raw.get(k)}

    @app.put("/api/models/{model_id}/tags")
    def put_tags(model_id: str, body: TagsBody):
        if model_id not in lib().models:
            raise HTTPException(404, "No such model")
        return {"tags": tags.set(model_id, body.tags)}

    def unique_dir(parent: Path, name: str) -> Path:
        path, n = parent / name, 2
        while path.exists():
            path, n = parent / f"{name} ({n})", n + 1
        return path

    def move_model(model_id: str, new_dir: Path) -> str:
        """Move a model's files into new_dir and carry its tags, history and
        STL previews over to the new ids. Loose files (sitting directly in a
        category folder) get their own model folder this way."""
        m = lib().models[model_id]
        root = settings.library.resolve()
        file_map: dict[str, str] = {}
        if "#" in m.relpath:
            cat_dir = root if m.category == UNCATEGORIZED else (root / m.category).resolve()
            # keep type subfolders (e.g. "stls/") the files sat in
            moves = [((root / f.relpath).resolve(), f.relpath) for f in m.files]
            moves = [(src, new_dir / src.relative_to(cat_dir), rel) for src, rel in moves]
        else:
            old_dir = (root / m.relpath).resolve()
            moves = [(old_dir, new_dir, m.relpath)]
        # Check first, so a folder the app can't write to fails cleanly instead
        # of leaving half-copied files behind.
        for src, _, _ in moves:
            if not os.access(src.parent, os.W_OK | os.X_OK):
                where = src.parent.relative_to(root).as_posix() or "the library folder"
                raise HTTPException(403, f"Print Pantry isn't allowed to move files out of '{where}' on the NAS. "
                                         "Run the deploy again to fix the folder permissions.")
        created = {d for _, dst, _ in moves for d in dst.parents
                   if root in d.parents and not d.exists()}
        done: list[tuple[Path, Path]] = []
        try:
            for src, dst, rel in moves:
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.rename(src, dst)  # same library volume: an atomic rename
                done.append((src, dst))
        except OSError as exc:
            for src, dst in reversed(done):
                try:
                    os.rename(dst, src)
                except OSError:
                    log.exception("could not move %s back to %s", dst, src)
            for d in sorted(created, key=lambda d: len(d.parts), reverse=True):
                try:
                    d.rmdir()  # only if empty
                except OSError:
                    pass
            log.exception("moving model %s to %s failed", m.relpath, new_dir)
            raise HTTPException(500, f"Couldn't move the files: {exc.strerror or exc}. Nothing was changed.") from exc
        if "#" in m.relpath:
            for src, dst, rel in moves:
                file_map[rel] = dst.relative_to(root).as_posix()
        else:
            new_rel = new_dir.relative_to(root).as_posix()
            for f in m.files:
                file_map[f.relpath] = new_rel + f.relpath[len(m.relpath):]
        new_id = make_id(new_dir.relative_to(root).as_posix())
        tags.migrate_id(model_id, new_id)
        ids = {make_id(old): make_id(new) for old, new in file_map.items()}
        history.migrate_ids({model_id: new_id}, ids)
        thumbs = settings.data / "thumbs"
        for old, new in ids.items():
            for t in thumbs.glob(f"{old}-*.png"):
                t.rename(t.with_name(new + t.name[len(old):]))
        scanner.scan()
        return new_id

    def folder_name(name: str) -> str:
        # Printables names files "Model-Part"; "Pot Ladle Clip-Pot Ladle Clip" -> "Pot Ladle Clip"
        head, sep, tail = name.partition("-")
        if sep and head.strip().lower() == tail.strip().lower():
            name = head
        return safe_name(name)

    def model_dir(m) -> Path:
        root = settings.library.resolve()
        path = (root / m.relpath).resolve()
        if root not in path.parents or not path.is_dir():
            raise HTTPException(404, "Model folder not found")
        return path

    def category_dir(name: str) -> Path:
        root = settings.library.resolve()
        if name.lower() == UNCATEGORIZED.lower():
            # a folder straight in the library would turn into a category
            raise HTTPException(400, "Pick a category for this model first")
        path = (root / name).resolve()
        if root not in path.parents:
            raise HTTPException(400, "Invalid category name")
        return path

    @app.put("/api/models/{model_id}/rename")
    def rename_model(model_id: str, body: RenameBody):
        m = lib().models.get(model_id)
        if not m:
            raise HTTPException(404, "No such model")
        new_name = safe_name(body.name, "")
        if not new_name:
            raise HTTPException(400, "Invalid model name")
        if "#" in m.relpath:
            # loose files: give them a model folder with the new name
            new_dir = unique_dir(category_dir(m.category), new_name)
        else:
            old_dir = model_dir(m)
            if new_name == old_dir.name:
                return {"ok": True, "id": model_id, "name": m.name}
            new_dir = old_dir.parent / new_name
            if new_dir.exists() and new_name.lower() != old_dir.name.lower():
                raise HTTPException(409, f"A model named '{new_name}' already exists in '{m.category}'")
        return {"ok": True, "id": move_model(model_id, new_dir), "name": new_dir.name}

    @app.put("/api/models/{model_id}/category")
    def change_model_category(model_id: str, body: CategoryChangeBody):
        m = lib().models.get(model_id)
        if not m:
            raise HTTPException(404, "No such model")
        new_category = safe_name(body.category, "")
        if not new_category:
            raise HTTPException(400, "Invalid category name")
        if new_category == m.category:
            return {"ok": True, "id": model_id, "category": m.category}
        target = category_dir(new_category)
        if "#" in m.relpath:
            new_dir = unique_dir(target, folder_name(m.name))
        else:
            old_dir = model_dir(m)
            new_dir = target / old_dir.name
            if new_dir.exists():
                raise HTTPException(409, f"A model named '{old_dir.name}' already exists in '{new_category}'")
        return {"ok": True, "id": move_model(model_id, new_dir), "category": new_category}

    @app.put("/api/files/{file_id}/rename")
    def rename_file(file_id: str, body: RenameBody):
        hit = resolve(file_id)
        f, old_path = hit
        new_name = safe_name(body.name)
        if not new_name:
            raise HTTPException(400, "Invalid file name")
        if not Path(new_name).suffix and old_path.suffix:
            new_name = new_name + old_path.suffix
        new_path = old_path.parent / new_name
        if new_path != old_path:
            if new_path.exists():
                raise HTTPException(409, f"A file named '{new_name}' already exists in this folder")
            old_path.rename(new_path)
            scanner.scan()
            root = settings.library.resolve()
            new_rel = new_path.relative_to(root).as_posix()
            new_f_id = make_id(new_rel)
            return {"ok": True, "id": new_f_id, "name": new_name}
        return {"ok": True, "id": file_id, "name": f.name}

    @app.get("/api/tags")
    def all_tags():
        return {"tags": tags.counts(set(lib().models))}

    @app.post("/api/rescan")
    def rescan():
        if scanner.scanning:
            return {"started": False, "scanning": True}
        threading.Thread(target=scanner.scan, daemon=True).start()
        return {"started": True}

    # ------------------------------------------------------------ files
    def resolve(file_id: str):
        hit = scanner.resolve(file_id)
        if not hit or not hit[1].is_file():
            raise HTTPException(404, "File not found; try a rescan")
        return hit

    @app.get("/api/files/{file_id}/raw")
    def raw(file_id: str, download: bool = False):
        f, path = resolve(file_id)
        media = {
            "stl": "model/stl",
            "project3mf": "model/3mf",
            "sliced3mf": "model/3mf",
            "gcode": "text/x-gcode",
        }.get(f.kind)
        return FileResponse(
            path,
            media_type=media,
            filename=f.name,
            content_disposition_type="attachment" if download else "inline",
        )

    @app.post("/api/files/{file_id}/link")
    def make_link(file_id: str):
        """A short-lived download URL a desktop slicer can fetch without logging in.

        The URL ends in a plain file name with no query string, because Snapmaker
        Orca names the downloaded file after the last part of the URL.
        """
        f, _ = resolve(file_id)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", f.name).strip("_") or "model.stl"
        return {"path": f"/api/dl/{auth.link_token(f.id)}/{safe}", "name": f.name}

    @app.get("/api/dl/{token}/{filename}")
    def signed_download(token: str, filename: str):
        file_id = auth.check_link(token)
        if not file_id:
            raise HTTPException(403, "This link has expired. Press the button again.")
        f, path = resolve(file_id)
        return FileResponse(path, media_type="application/octet-stream", filename=filename)

    # ------------------------------------------------------------ import from Printables / MakerWorld
    imp = importer or Importer(settings.library, store, scanner.scan, tags.add, make_id)
    imp.rescan, imp.add_tags, imp.make_model_id = scanner.scan, tags.add, make_id
    app.state.importer = imp

    def category_names() -> list[str]:
        return sorted((c for c in lib().categories if c != "Uncategorized"), key=str.lower)

    @app.get("/api/import/settings")
    def import_settings():
        return imp.makerworld_status()

    @app.put("/api/import/settings")
    def put_import_settings(body: ImportSettings):
        imp.set_makerworld_token(body.makerworld_token)
        return import_settings()

    @app.post("/api/import/makerworld-login")
    def makerworld_login(body: SignIn):
        try:
            return imp.makerworld_sign_in(body.email, body.password, body.code, body.tfa_key, body.tfa_code)
        except ImportError_ as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/import/preview")
    def import_preview(body: ImportBody):
        try:
            plan = imp.plan(body.url)
        except ImportError_ as exc:
            raise HTTPException(400, str(exc)) from None
        return {**plan.preview(), "suggested_category": suggest_category(plan.categories, category_names()),
                "categories": category_names(), "site_categories": plan.categories}

    @app.post("/api/import")
    def import_start(body: ImportBody):
        try:
            job = imp.start(body.url, body.category, body.name, body.tags)
        except ImportError_ as exc:
            raise HTTPException(400, str(exc)) from None
        return job.to_json()

    @app.get("/api/import")
    def import_jobs():
        return {"jobs": imp.recent()}

    @app.get("/api/import/{job_id}")
    def import_status(job_id: str):
        job = imp.jobs.get(job_id)
        if not job:
            raise HTTPException(404, "No such import")
        return job.to_json()

    # ------------------------------------------------------------ preferences
    @app.get("/api/prefs")
    def get_prefs():
        prefs = store.get("prefs", {}) or {}
        return {"slicer": prefs.get("slicer", "snapmaker"), "slicers": list(SLICERS)}

    @app.put("/api/prefs")
    def put_prefs(patch: Prefs):
        prefs = store.get("prefs", {}) or {}
        if patch.slicer is not None:
            if patch.slicer not in SLICERS:
                raise HTTPException(400, "Unknown slicer")
            prefs["slicer"] = patch.slicer
        store.set("prefs", prefs)
        return get_prefs()

    @app.get("/api/files/{file_id}/thumb")
    def thumb(file_id: str, plate: int = 1):
        f, path = resolve(file_id)
        headers = {"Cache-Control": "private, max-age=86400"}
        if f.kind in ("sliced3mf", "project3mf"):
            data = formats.read_3mf_thumbnail(path, plate)
            if data:
                return Response(data, media_type="image/png", headers=headers)
        elif f.kind == "gcode":
            hit = formats.read_gcode_thumbnail(path)
            if hit:
                return Response(hit[0], media_type=hit[1], headers=headers)
        elif f.kind == "image":
            return FileResponse(path, headers=headers)
        elif f.kind == "stl":
            cached = render_path(f)
            if cached.is_file():
                return FileResponse(cached, media_type="image/png", headers=headers)
        raise HTTPException(404, "No thumbnail")

    def render_path(f) -> Path:
        return settings.data / "thumbs" / f"{f.id}-{int(f.mtime)}-{f.size}.png"

    @app.put("/api/files/{file_id}/thumb")
    async def put_render(file_id: str, request: Request):
        """The browser renders STL previews once and stores them here."""
        f, _ = resolve(file_id)
        if f.kind != "stl":
            raise HTTPException(400, "Only STL previews can be stored")
        body = await request.body()
        if len(body) > MAX_RENDER_BYTES or not body.startswith(b"\x89PNG\r\n\x1a\n"):
            raise HTTPException(400, "Expected a PNG under 2 MB")
        for old in (settings.data / "thumbs").glob(f"{f.id}-*.png"):
            old.unlink(missing_ok=True)
        render_path(f).write_bytes(body)
        return {"ok": True}

    # ------------------------------------------------------------ printers
    app.include_router(printers_router(hub, resolve, on_start=history.record_start))
    app.include_router(history_router(history))

    # ------------------------------------------------------------ spools and cost
    from .printers import api as printers_api

    spools = Spools(settings.data / "printpantry.db", store, hub)
    app.state.spools = spools
    printers_api.EXTRA_CHECKS[:] = [spools.check_tools]
    app.include_router(spools_router(spools))
    history.on_finish(lambda rec: spools.on_print_finished(rec, history))

    # ------------------------------------------------------------ slicing and calibration
    slicer = slicer or client_from_env()
    calib = CalibrationStore(settings.data / "calibration.json")
    app.include_router(slicer_router(slicer, resolve, scanner.scan, calib.flow_ratios))
    app.include_router(calibration_router(calib, slicer, hub, settings.library, hub.starting))

    # ------------------------------------------------------------ UI
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", response_class=HTMLResponse)
    @app.get("/{rest:path}", response_class=HTMLResponse, include_in_schema=False)
    def index(rest: str = ""):
        if rest.startswith("api/"):
            raise HTTPException(404)
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    return app


def run() -> None:
    import os

    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=os.environ.get("PP_HOST", "0.0.0.0"),
        port=int(os.environ.get("PP_PORT", "8484")),
        proxy_headers=True,
    )


if __name__ == "__main__":
    run()
