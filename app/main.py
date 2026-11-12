"""Print Pantry web app: JSON API plus the static single-page UI."""

from __future__ import annotations

import logging
import re
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, formats
from .auth import COOKIE, MAX_AGE, Auth
from .config import Settings
from .printers import PrinterHub
from .printers.api import make_router as printers_router
from .scanner import Scanner
from .slicing import SlicerClient, client_from_env
from .slicing import make_router as slicer_router
from .store import Store

log = logging.getLogger("printpantry")
STATIC = Path(__file__).parent / "static"
MAX_RENDER_BYTES = 2 * 1024 * 1024


SLICERS = ("snapmaker", "bambu")


class Prefs(BaseModel):
    slicer: str | None = None


class CategoryPatch(BaseModel):
    icon: str | None = None
    color: str | None = None


def create_app(
    settings: Settings | None = None,
    background_scan: bool = True,
    printers: PrinterHub | None = None,
    slicer: SlicerClient | None = None,
) -> FastAPI:
    settings = settings or Settings()
    settings.ensure_dirs()
    store = Store(settings.data / "printpantry.db")
    scanner = Scanner(settings, store)
    auth = Auth(settings.password, settings.data)
    hub = printers or PrinterHub(settings.printers)
    icon_names = set()
    try:
        import json

        icon_names = set(json.loads((STATIC / "icons.json").read_text()))
    except (OSError, ValueError):
        pass
    stop = threading.Event()

    def loop():
        scanner.scan()
        while settings.scan_interval > 0 and not stop.wait(settings.scan_interval):
            scanner.scan()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if background_scan:
            threading.Thread(target=loop, name="scanner", daemon=True).start()
            hub.start()
        else:
            scanner.scan()
        yield
        stop.set()
        hub.stop()

    app = FastAPI(title="Print Pantry", version=__version__, lifespan=lifespan)
    app.state.scanner = scanner
    app.state.store = store
    app.state.settings = settings
    app.state.printers = hub

    # ------------------------------------------------------------ auth
    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = request.url.path
        # /api/dl/ checks its own signed token: slicers fetch it without a cookie.
        open_paths = ("/login", "/healthz", "/static/style.css", "/static/favicon.svg", "/api/dl/")
        if auth.enabled and not path.startswith(open_paths) and not auth.valid(request.cookies.get(COOKIE)):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "login required"}, status_code=401)
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(error: str = ""):
        html = (STATIC / "login.html").read_text()
        msg = '<p class="login-error">Wrong password.</p>' if error else ""
        return html.replace("<!--ERROR-->", msg)

    @app.post("/login")
    async def login(request: Request):
        form = await request.body()
        from urllib.parse import parse_qs

        attempt = parse_qs(form.decode()).get("password", [""])[0]
        if not auth.enabled:
            return RedirectResponse("/", status_code=303)
        if not auth.check_password(attempt):
            time.sleep(1)  # slow down guessing
            return RedirectResponse("/login?error=1", status_code=303)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            COOKIE, auth.make_cookie(), max_age=MAX_AGE, expires=MAX_AGE, httponly=True, samesite="lax"
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

    @app.get("/api/categories/{name}")
    def category(name: str):
        cat = lib().categories.get(name)
        if not cat:
            raise HTTPException(404, "No such category")
        return {**cat.summary(), "models": [m.summary() for m in cat.models]}

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
    def models(q: str = "", printer: str = "", sort: str = "name", limit: int = 200):
        items = list(lib().models.values())
        if q:
            words = q.lower().split()
            items = [
                m for m in items
                if all(
                    w in m.name.lower() or w in m.category.lower()
                    or any(w in f.name.lower() for f in m.files)
                    for w in words
                )
            ]
        if printer:
            items = [m for m in items if any(f.printer == printer for f in m.files)]
        if sort == "recent":
            items.sort(key=lambda m: m.mtime, reverse=True)
        else:
            items.sort(key=lambda m: m.name.lower())
        return {"total": len(items), "models": [m.summary() for m in items[: max(1, min(limit, 1000))]]}

    @app.get("/api/models/{model_id}")
    def model(model_id: str):
        m = lib().models.get(model_id)
        if not m:
            raise HTTPException(404, "No such model")
        return m.detail()

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
    app.include_router(printers_router(hub, resolve))

    # ------------------------------------------------------------ slicing
    app.include_router(slicer_router(slicer or client_from_env(), resolve, scanner.scan))

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
