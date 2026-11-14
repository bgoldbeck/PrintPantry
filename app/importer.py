"""Import a model from a Printables or MakerWorld link into the library folder.

What each site allows (checked 2026-11-13 from the home network and the NAS):

* **Printables** has a public GraphQL API (the one its own website uses).
  Model details and file download links work without logging in, so an
  import fetches the STL/3MF files, pictures, tags and description.
* **MakerWorld** model pages sit behind a Cloudflare browser check, but its
  JSON API answers without one. Details, tags and pictures work without
  logging in; the files themselves need a logged-in MakerWorld account
  ("Please log in to download models."). With a MakerWorld token saved in
  Print Pantry the importer downloads the 3MF too; without one it imports the
  details and pictures and says the file still has to be added.

An import writes ``<library>/<Category>/<Model name>/`` with the files, an
``images/`` folder and ``pantry-source.json`` (where it came from), then
rescans. Tags from the site are added to the model's tags.
"""

from __future__ import annotations

import html
import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx

log = logging.getLogger("printpantry.import")

SOURCE_FILE = "pantry-source.json"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
PRINTABLES_API = "https://api.printables.com/graphql/"
PRINTABLES_MEDIA = "https://media.printables.com/"
MAKERWORLD_API = "https://makerworld.com/api/v1/design-service"
MODEL_EXTS = (".stl", ".3mf", ".obj", ".step", ".stp", ".scad", ".f3d", ".zip")
MAX_FILE_BYTES = 1024 * 1024 * 1024  # 1 GB per file
MAX_PICTURES = 8


class ImportError_(Exception):
    """A problem worth showing to the user as-is."""


# ------------------------------------------------------------------ links


def parse_link(url: str) -> tuple[str, str]:
    """'https://www.printables.com/model/3161-3d-benchy' -> ('printables', '3161')."""
    url = (url or "").strip()
    if not re.match(r"https?://", url):
        url = "https://" + url
    u = urlparse(url)
    host = (u.hostname or "").lower()
    if host.endswith("printables.com"):
        m = re.search(r"/model/(\d+)", u.path)
        if m:
            return "printables", m.group(1)
    if host.endswith(("makerworld.com", "makerworld.com.cn")):
        m = re.search(r"/models/(\d+)", u.path)
        if m:
            return "makerworld", m.group(1)
    raise ImportError_("Paste a Printables (printables.com/model/...) or MakerWorld (makerworld.com/.../models/...) link.")


def link_profile(url: str) -> str:
    """MakerWorld '...#profileId-4079808' -> '4079808' (the print profile picked on the page)."""
    m = re.search(r"profileId[-=_](\d+)", url or "")
    return m.group(1) if m else ""


def safe_name(name: str, fallback: str = "model") -> str:
    """A file or folder name that works on Windows and Linux."""
    name = html.unescape(str(name or ""))
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:100].strip(" .") or fallback


def html_to_text(s: str) -> str:
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>", "\n", s or "")
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


# ------------------------------------------------------------------ plans


@dataclass
class RemoteFile:
    name: str
    size: int = 0
    url: str = ""  # known up front, or fetched by ``resolve`` just before download
    resolve: object = None  # callable -> url


@dataclass
class Plan:
    site: str
    id: str
    url: str
    title: str
    author: str = ""
    license: str = ""
    summary: str = ""
    tags: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)  # site's own, broad to narrow
    files: list[RemoteFile] = field(default_factory=list)
    pictures: list[str] = field(default_factory=list)
    note: str = ""  # shown to the user, e.g. why no files
    profile: str = ""  # MakerWorld print profile being imported
    needs_login: bool = False

    def preview(self) -> dict:
        return {
            "site": self.site,
            "id": self.id,
            "url": self.url,
            "title": self.title,
            "author": self.author,
            "license": self.license,
            "summary": self.summary[:600],
            "tags": self.tags,
            "categories": self.categories,
            "files": [{"name": f.name, "size": f.size} for f in self.files],
            "pictures": self.pictures[:1],
            "picture_count": len(self.pictures),
            "note": self.note,
            "profile": self.profile,
            "needs_login": self.needs_login,
        }


# ------------------------------------------------------------------ Printables

_PRINT_QUERY = """query Print($id: ID!) { print(id: $id) {
  id name slug summary tags { name } category { id path { name } }
  stls { id name fileSize } otherFiles { id name fileSize } images { filePath } } }"""
# Asked for separately: if Printables renames these, the import still works.
_PRINT_EXTRA = """query PrintExtra($id: ID!) { print(id: $id) {
  user { publicUsername } license { name } } }"""
_DOWNLOAD = """mutation GetDownloadLink($id: ID!, $modelId: ID!, $fileType: DownloadFileTypeEnum!, $source: DownloadSourceEnum!) {
  getDownloadLink(id: $id, printId: $modelId, fileType: $fileType, source: $source) {
    ok errors { field messages } output { link } } }"""


class Printables:
    def __init__(self, http: httpx.Client):
        self.http = http

    def _gql(self, query: str, variables: dict) -> dict:
        r = self.http.post(PRINTABLES_API, json={"query": query, "variables": variables})
        try:
            body = r.json()
        except ValueError:
            raise ImportError_(f"Printables answered with HTTP {r.status_code}.") from None
        if body.get("errors") and not body.get("data"):
            raise ImportError_("Printables: " + "; ".join(e.get("message", "?") for e in body["errors"])[:300])
        return body.get("data") or {}

    def plan(self, model_id: str, url: str) -> Plan:
        p = self._gql(_PRINT_QUERY, {"id": model_id}).get("print")
        if not p:
            raise ImportError_("Printables has no model with that link (removed or private?).")
        author = lic = ""
        try:
            extra = self._gql(_PRINT_EXTRA, {"id": model_id}).get("print") or {}
            author = (extra.get("user") or {}).get("publicUsername") or ""
            lic = (extra.get("license") or {}).get("name") or ""
        except (ImportError_, httpx.HTTPError):
            pass
        plan = Plan(
            site="Printables",
            id=str(p["id"]),
            url=f"https://www.printables.com/model/{p['id']}-{p.get('slug') or ''}".rstrip("-"),
            title=p.get("name") or f"Printables {model_id}",
            author=author,
            license=lic,
            summary=html_to_text(p.get("summary") or ""),
            tags=[t["name"] for t in p.get("tags") or [] if t.get("name")],
            categories=[c["name"] for c in ((p.get("category") or {}).get("path") or []) if c.get("name")],
            pictures=[PRINTABLES_MEDIA + i["filePath"].lstrip("/") for i in p.get("images") or [] if i.get("filePath")],
        )
        for kind, items in (("stl", p.get("stls")), ("other", p.get("otherFiles"))):
            for f in items or []:
                name = f.get("name") or ""
                if kind == "other" and not name.lower().endswith(MODEL_EXTS):
                    continue
                plan.files.append(RemoteFile(
                    name=name, size=int(f.get("fileSize") or 0),
                    resolve=lambda fid=f["id"], k=kind: self._link(fid, plan.id, k),
                ))
        if not plan.files:
            plan.note = "This model has no STL or 3MF files on Printables."
        return plan

    def _link(self, file_id: str, model_id: str, kind: str) -> str:
        d = self._gql(_DOWNLOAD, {"id": file_id, "modelId": model_id, "fileType": kind, "source": "model_detail"})
        out = d.get("getDownloadLink") or {}
        link = (out.get("output") or {}).get("link")
        if not out.get("ok") or not link:
            raise ImportError_(f"Printables wouldn't give a download link: {out.get('errors')}")
        return link


# ------------------------------------------------------------------ MakerWorld


class MakerWorld:
    def __init__(self, http: httpx.Client, token: str = ""):
        self.http = http
        self.token = token.strip()

    def _get(self, path: str, auth: bool = False) -> httpx.Response:
        headers = {"Accept": "application/json", "Referer": "https://makerworld.com/"}
        if auth and self.token:
            # The website sends the token as a cookie; the apps send it as a bearer header.
            headers["Authorization"] = f"Bearer {self.token}"
            headers["Cookie"] = f"token={self.token}"
        return self.http.get(MAKERWORLD_API + path, headers=headers)

    def plan(self, design_id: str, url: str) -> Plan:
        r = self._get(f"/design/{design_id}")
        if r.status_code == 404:
            raise ImportError_("MakerWorld has no model with that link.")
        if r.status_code != 200:
            raise ImportError_(f"MakerWorld answered with HTTP {r.status_code}.")
        d = r.json()
        if not d.get("title"):
            raise ImportError_("MakerWorld has no model with that link (removed?).")
        ext = d.get("designExtension") or {}
        pics = [p["url"] for p in ext.get("design_pictures") or [] if p.get("url")]
        if not pics and d.get("coverUrl"):
            pics = [d["coverUrl"]]
        plan = Plan(
            site="MakerWorld",
            id=str(d.get("id") or design_id),
            url=f"https://makerworld.com/en/models/{d.get('id') or design_id}",
            title=d.get("title") or f"MakerWorld {design_id}",
            author=(d.get("designCreator") or {}).get("name") or "",
            license=d.get("license") or "",
            summary=html_to_text(d.get("summary") or ""),
            tags=[t for t in d.get("tags") or [] if isinstance(t, str)],
            categories=[c["name"] for c in d.get("categories") or [] if c.get("name")],
            pictures=pics,
        )
        instances = d.get("instances") or []
        want = link_profile(url)
        inst = None
        if want:
            # The page's "#profileId-N" is really the instance id; accept a profile id too.
            inst = next((i for i in instances if str(i.get("id")) == want), None) or next(
                (i for i in instances if str(i.get("profileId")) == want), None)
        inst = inst or next((i for i in instances if i.get("id") == d.get("defaultInstanceId")), None)
        inst = inst or (instances[0] if instances else None)
        if inst:
            plan.profile = inst.get("title") or ""
            plan.url += f"#profileId-{inst['id']}"
        if want and inst and str(inst.get("profileId")) != want and str(inst.get("id")) != want:
            plan.profile += " (the profile in the link wasn't found; using the default)"
        model_files = ext.get("model_files") or []
        name = safe_name(plan.title) + ".3mf"
        if len(instances) > 1 and plan.profile:
            name = safe_name(f"{plan.title} - {inst.get('title')}") + ".3mf"
        elif model_files and model_files[0].get("modelName", "").lower().endswith(".3mf"):
            name = safe_name(model_files[0]["modelName"])
        if inst and self.token:
            plan.files.append(RemoteFile(
                name=name, size=0,
                resolve=lambda iid=inst["id"]: self._link(iid),
            ))
        elif inst:
            plan.note = ("MakerWorld only gives files to signed-in accounts. Press \"Sign in to MakerWorld\" once "
                         "and the 3MF comes along. Importing now brings the details, tags and pictures only.")
            plan.needs_login = True
        else:
            plan.note = "This MakerWorld model has no print profile to download."
        return plan

    def _link(self, instance_id) -> str:
        r = self._get(f"/instance/{instance_id}/f3mf?type=download", auth=True)
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code in (401, 403):
            raise ImportError_("MakerWorld refused the download (" + (body.get("error") or f"HTTP {r.status_code}")
                               + "). The MakerWorld sign-in has probably expired: press Sign in to MakerWorld "
                               "in the Import dialog and try again.")
        link = body.get("url") or (body.get("data") or {}).get("url") if isinstance(body, dict) else None
        if r.status_code != 200 or not link:
            raise ImportError_(f"MakerWorld gave no download link (HTTP {r.status_code}).")
        return link


# ------------------------------------------------------------------ MakerWorld sign-in
#
# MakerWorld uses the Bambu Lab account. Signing in goes through the same login
# API that Bambu Studio and Bambu Handy use: email + password, and, when Bambu
# asks for it, a code they email (or a 2-factor code from an authenticator app).
# Only the resulting access token is stored (on the NAS); the password is not.

BAMBU_API = "https://api.bambulab.com/v1/user-service"


class SignInStep(Exception):
    """Bambu wants one more step; ``kind`` is 'code' (emailed) or 'tfa'."""

    def __init__(self, kind: str, message: str, tfa_key: str = ""):
        super().__init__(message)
        self.kind, self.tfa_key = kind, tfa_key


def bambu_sign_in(http: httpx.Client, email: str, password: str = "", code: str = "",
                  tfa_key: str = "", tfa_code: str = "") -> dict:
    """Returns {'token', 'refresh', 'expires'} or raises SignInStep / ImportError_."""
    if tfa_key and tfa_code:
        r = http.post("https://bambulab.com/api/sign-in/tfa", json={"tfaKey": tfa_key, "tfaCode": tfa_code.strip()})
        token = r.cookies.get("token") or (r.json().get("accessToken") if r.headers.get("content-type", "").startswith("application/json") else "")
        if r.status_code != 200 or not token:
            raise ImportError_("That authenticator code didn't work. Try a fresh one.")
        return {"token": token, "refresh": r.cookies.get("refreshToken", ""), "expires": 0}
    body = {"account": email.strip(), "code": code.strip()} if code else {"account": email.strip(), "password": password}
    r = http.post(f"{BAMBU_API}/user/login", json=body)
    try:
        d = r.json()
    except ValueError:
        raise ImportError_(f"Bambu's sign-in answered with HTTP {r.status_code} (maybe a bot check). Try again later.") from None
    if d.get("accessToken"):
        return {"token": d["accessToken"], "refresh": d.get("refreshToken", ""),
                "expires": time.time() + int(d.get("expiresIn") or 0) if d.get("expiresIn") else 0}
    kind = d.get("loginType") or ""
    if kind == "verifyCode":
        http.post(f"{BAMBU_API}/user/sendemail/code", json={"email": email.strip(), "type": "codeLogin"})
        raise SignInStep("code", f"Bambu emailed a code to {email.strip()}. Enter it to finish signing in.")
    if kind == "tfa":
        raise SignInStep("tfa", "Enter the code from your authenticator app.", d.get("tfaKey", ""))
    msg = d.get("error") or d.get("message") or f"HTTP {r.status_code}"
    if code:
        raise ImportError_(f"That code didn't work ({msg}). Codes expire after a few minutes; ask for a new one.")
    raise ImportError_(f"Bambu didn't accept that email and password ({msg}).")


# ------------------------------------------------------------------ jobs


@dataclass
class Job:
    id: str
    url: str
    state: str = "running"  # running | done | error
    step: str = "Reading the link"
    title: str = ""
    folder: str = ""
    model_id: str = ""
    done_files: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    note: str = ""
    error: str = ""
    started: float = field(default_factory=time.time)

    def to_json(self) -> dict:
        return {k: v for k, v in vars(self).items()}


def suggest_category(site_categories: list[str], existing: list[str]) -> str:
    """Pick one of the user's categories that matches the site's category path."""
    low = {c.lower(): c for c in existing}
    for name in reversed(site_categories):  # most specific first
        n = name.lower()
        if n in low:
            return low[n]
        for key, real in low.items():
            if key and (key in n.split() or n in key or key.rstrip("s") == n.rstrip("s")):
                return real
    return ""


class Importer:
    """Runs imports one at a time in a background thread."""

    def __init__(self, library: Path, store, rescan, add_tags, make_model_id, http: httpx.Client | None = None):
        self.library = library
        self.store = store
        self.rescan = rescan
        self.add_tags = add_tags
        self.make_model_id = make_model_id
        self.http = http or httpx.Client(
            headers={"User-Agent": UA}, timeout=httpx.Timeout(30, read=120), follow_redirects=True
        )
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()  # one import at a time

    # -- settings
    @property
    def makerworld_token(self) -> str:
        return (self.store.get("import_settings", {}) or {}).get("makerworld_token", "")

    def makerworld_status(self) -> dict:
        st = self.store.get("import_settings", {}) or {}
        return {"makerworld_token": bool(st.get("makerworld_token")), "makerworld_account": st.get("makerworld_account", "")}

    def makerworld_sign_in(self, email: str, password: str = "", code: str = "", tfa_key: str = "", tfa_code: str = "") -> dict:
        """Returns the status, or {'step': 'code'|'tfa', 'message', 'tfa_key'} when Bambu wants more."""
        if not email.strip():
            raise ImportError_("Enter the email of your MakerWorld / Bambu Lab account.")
        try:
            got = bambu_sign_in(self.http, email, password, code, tfa_key, tfa_code)
        except SignInStep as step:
            return {"step": step.kind, "message": str(step), "tfa_key": step.tfa_key}
        except httpx.HTTPError as exc:
            raise ImportError_(f"Couldn't reach Bambu's sign-in: {exc}") from None
        self.set_makerworld_token(got["token"])
        st = self.store.get("import_settings", {}) or {}
        st.update(makerworld_account=email.strip(), makerworld_refresh=got.get("refresh", ""))
        self.store.set("import_settings", st)
        return self.makerworld_status()

    def set_makerworld_token(self, token: str) -> None:
        s = self.store.get("import_settings", {}) or {}
        token = token.strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if token:
            s["makerworld_token"] = token
        else:
            for k in ("makerworld_token", "makerworld_refresh", "makerworld_account"):
                s.pop(k, None)
        self.store.set("import_settings", s)

    # -- planning
    def plan(self, url: str) -> Plan:
        site, model_id = parse_link(url)
        try:
            if site == "printables":
                return Printables(self.http).plan(model_id, url)
            return MakerWorld(self.http, self.makerworld_token).plan(model_id, url)
        except httpx.HTTPError as exc:
            raise ImportError_(f"Couldn't reach {site.title()}: {exc}") from None

    # -- running
    def start(self, url: str, category: str, name: str = "", tags: list[str] | None = None) -> Job:
        category = safe_name(category, "")
        if not category:
            raise ImportError_("Pick a category.")
        parse_link(url)  # fail fast on a bad link
        job = Job(id=uuid.uuid4().hex[:12], url=url)
        self.jobs[job.id] = job
        # forget old finished jobs
        for old in [j for j in self.jobs.values() if j.state != "running" and time.time() - j.started > 3600]:
            self.jobs.pop(old.id, None)
        threading.Thread(target=self._run, args=(job, category, name, tags or []), daemon=True).start()
        return job

    def run_sync(self, url: str, category: str, name: str = "", tags: list[str] | None = None) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], url=url)
        self.jobs[job.id] = job
        self._run(job, safe_name(category, "Uncategorized"), name, tags or [])
        return job

    def _run(self, job: Job, category: str, name: str, extra_tags: list[str]) -> None:
        with self._lock:
            try:
                self._do(job, category, name, extra_tags)
                job.state = "done"
                job.step = "Done"
                log.info("imported %s into %s (%d files)", job.url, job.folder, len(job.done_files))
            except ImportError_ as exc:
                job.state, job.error = "error", str(exc)
            except httpx.HTTPError as exc:
                job.state, job.error = "error", f"Download failed: {exc}"
            except OSError as exc:
                job.state, job.error = "error", f"Couldn't write to the library: {exc}"
            except Exception as exc:  # pragma: no cover - keep the job readable
                log.exception("import failed")
                job.state, job.error = "error", f"Import failed: {exc}"
            if job.state == "error":
                log.warning("import of %s failed: %s", job.url, job.error)
                self._remove_if_empty(job)

    def _remove_if_empty(self, job: Job) -> None:
        """Don't leave an empty model folder behind after a failed import."""
        if not job.folder:
            return
        folder = self.library / job.folder
        try:
            for sub in (folder / "images", folder):
                if sub.is_dir() and not any(sub.iterdir()):
                    sub.rmdir()
        except OSError:
            pass

    def _do(self, job: Job, category: str, name: str, extra_tags: list[str]) -> None:
        plan = self.plan(job.url)
        job.title = plan.title
        job.note = plan.note
        folder_name = safe_name(name or plan.title)
        folder = self._folder(category, folder_name, plan)
        rel = f"{category}/{folder.name}"
        job.folder = rel
        folder.mkdir(parents=True, exist_ok=True)

        for i, f in enumerate(plan.files, 1):
            fname = safe_name(Path(f.name).name, f"file{i}")
            dest = folder / fname
            if dest.exists() and f.size and dest.stat().st_size == f.size:
                job.skipped.append(fname)
                continue
            job.step = f"Downloading {fname} ({i} of {len(plan.files)})"
            link = f.url or f.resolve()
            self._download(link, dest)
            job.done_files.append(fname)

        pics = plan.pictures[:MAX_PICTURES]
        if pics:
            (folder / "images").mkdir(exist_ok=True)
        for i, url in enumerate(pics, 1):
            job.step = f"Saving pictures ({i} of {len(pics)})"
            ext = Path(urlparse(url).path).suffix.lower()
            ext = ext if ext in (".jpg", ".jpeg", ".png", ".webp", ".gif") else ".jpg"
            dest = folder / "images" / f"{plan.site.lower()}-{i:02d}{ext}"
            if dest.exists():
                continue
            try:
                self._download(url, dest, limit=50 * 1024 * 1024)
            except (httpx.HTTPError, ImportError_) as exc:
                log.warning("picture %s failed: %s", url, exc)

        meta = {
            "site": plan.site, "id": plan.id, "url": plan.url, "title": plan.title,
            "author": plan.author, "license": plan.license, "tags": plan.tags,
            "categories": plan.categories, "summary": plan.summary,
            "files": [safe_name(Path(f.name).name) for f in plan.files],
            "imported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        (folder / SOURCE_FILE).write_text(json.dumps(meta, indent=2, ensure_ascii=False), "utf-8")

        job.step = "Adding to the library"
        job.model_id = self.make_model_id(rel)
        self.add_tags(job.model_id, list(plan.tags) + list(extra_tags))
        self.rescan()

    def _folder(self, category: str, folder_name: str, plan: Plan) -> Path:
        """The model folder; a re-import of the same link reuses its folder."""
        base = self.library / category
        for n in range(1, 50):
            cand = base / (folder_name if n == 1 else f"{folder_name} ({n})")
            meta = cand / SOURCE_FILE
            if not cand.exists():
                return cand
            try:
                if json.loads(meta.read_text("utf-8")).get("url") == plan.url:
                    return cand
            except (OSError, ValueError):
                pass
        raise ImportError_("Too many folders with that name already.")

    def _download(self, url: str, dest: Path, limit: int = MAX_FILE_BYTES) -> None:
        if urlparse(url).scheme != "https":
            raise ImportError_("Refusing a non-https download link.")
        tmp = dest.with_name("." + dest.name + ".part")
        try:
            with self.http.stream("GET", url) as r:
                if r.status_code != 200:
                    raise ImportError_(f"Download of {dest.name} failed: HTTP {r.status_code}")
                got = 0
                with open(tmp, "wb") as out:
                    for chunk in r.iter_bytes(1024 * 256):
                        got += len(chunk)
                        if got > limit:
                            raise ImportError_(f"{dest.name} is bigger than {limit // 1048576} MB; skipped.")
                        out.write(chunk)
            tmp.replace(dest)
        finally:
            tmp.unlink(missing_ok=True)
