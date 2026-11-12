"""Walk the library folder and build categories -> models -> files.

Folder convention (documented in README):

    library/
      Tools/                      <- category (top-level folder)
        Wrench Holder/            <- model (any folder inside a category)
          wrench.stl              <- part(s) shown in the 3D viewer
          wrench.3mf              <- project 3MF, the editable source
          wrench_P1S.gcode.3mf    <- sliced for the P1S (plates inside)
          wrench_U1.gcode         <- sliced for the U1
          images/photo.jpg        <- extra pictures
        loose-part.stl            <- files directly in a category become their
        loose-part_U1.gcode          own model, grouped by file name

Everything under a model folder, at any depth, belongs to that model. Files at
the library root land in "Uncategorized".
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import formats, icons
from .config import Settings
from .store import Store

log = logging.getLogger("printpantry.scan")

UNCATEGORIZED = "Uncategorized"
SKIP_DIRS = {"@eadir", ".git", "__macosx", ".thumbnails", "#recycle", ".recycle", "$recycle.bin"}


def make_id(relpath: str) -> str:
    return hashlib.sha1(relpath.encode("utf-8")).hexdigest()[:12]


@dataclass
class LibFile:
    id: str
    relpath: str
    name: str
    size: int
    mtime: float
    kind: str  # stl | project3mf | sliced3mf | gcode | image | other
    info: formats.FileInfo | None = None
    printer: str = ""  # PrinterProfile.id, for sliced files

    def to_json(self) -> dict:
        data = {
            "id": self.id,
            "path": self.relpath,
            "name": self.name,
            "size": self.size,
            "mtime": self.mtime,
            "kind": self.kind,
            "printer": self.printer,
        }
        if self.info:
            data["printer_model"] = self.info.printer_model
            data["slicer"] = self.info.slicer
            data["plates"] = [
                {
                    "index": p.index,
                    "seconds": p.seconds,
                    "grams": p.grams,
                    "filaments": [vars(f) for f in p.filaments],
                    "objects": p.objects,
                    "has_thumbnail": p.has_thumbnail,
                }
                for p in self.info.plates
            ]
            if self.info.error:
                data["error"] = self.info.error
        return data


@dataclass
class Model:
    id: str
    name: str
    category: str
    relpath: str  # folder (or stem, for loose files) relative to library
    files: list[LibFile] = field(default_factory=list)

    @property
    def mtime(self) -> float:
        return max((f.mtime for f in self.files), default=0)

    def by_kind(self, *kinds: str) -> list[LibFile]:
        return [f for f in self.files if f.kind in kinds]

    def cover(self) -> dict | None:
        """Best picture for a card: photo, then plate thumbnail, then STL render."""
        for f in self.by_kind("image"):
            return {"type": "image", "file": f.id}
        for f in self.by_kind("sliced3mf", "project3mf", "gcode"):
            if f.info and any(p.has_thumbnail for p in f.info.plates):
                plate = next(p.index for p in f.info.plates if p.has_thumbnail)
                return {"type": "plate", "file": f.id, "plate": plate}
        for f in self.by_kind("stl"):
            return {"type": "stl", "file": f.id}
        return None

    def summary(self) -> dict:
        printers = sorted({f.printer for f in self.files if f.printer})
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "path": self.relpath,
            "mtime": self.mtime,
            "counts": {
                "stl": len(self.by_kind("stl")),
                "project": len(self.by_kind("project3mf")),
                "sliced": len(self.by_kind("sliced3mf", "gcode")),
            },
            "printers": printers,
            "cover": self.cover(),
        }

    def detail(self) -> dict:
        data = self.summary()
        order = {"stl": 0, "project3mf": 1, "sliced3mf": 2, "gcode": 3, "image": 4, "other": 5}
        data["files"] = [
            f.to_json() for f in sorted(self.files, key=lambda f: (order.get(f.kind, 9), f.relpath.lower()))
        ]
        return data


@dataclass
class Category:
    name: str
    icon: str
    color: str
    models: list[Model] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.name

    def summary(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "icon": self.icon,
            "color": self.color,
            "model_count": len(self.models),
            "sliced_count": sum(len(m.by_kind("sliced3mf", "gcode")) for m in self.models),
        }


@dataclass
class Library:
    categories: dict[str, Category] = field(default_factory=dict)
    models: dict[str, Model] = field(default_factory=dict)
    files: dict[str, LibFile] = field(default_factory=dict)
    scanned_at: float = 0
    scan_seconds: float = 0
    error: str = ""


# ------------------------------------------------------------------ grouping

_VARIANT_SUFFIX = re.compile(
    r"([ _.\-]+(p1s|p1p|x1c?|a1|u1|snapmaker|bambu|plate[ _-]?\d+|v\d+|pla\+?|petg|abs|asa|tpu|"
    r"\d+(\.\d+)?mm|\d+h\d*m?|\d+m|copy|final|\(\d+\)))+$",
    re.I,
)


def stem_of(name: str) -> str:
    low = name.lower()
    for ext in (".gcode.3mf", ".3mf", ".gcode", ".bgcode", ".stl"):
        if low.endswith(ext):
            name = name[: -len(ext)]
            break
    else:
        name = Path(name).stem
    return name


def group_key(name: str) -> str:
    base = stem_of(name)
    prev = None
    while prev != base:
        prev = base
        base = _VARIANT_SUFFIX.sub("", base)
    return re.sub(r"[\s_\-.]+", " ", base).strip().lower()


def pretty_name(name: str) -> str:
    base = stem_of(name)
    return re.sub(r"[_]+", " ", base).strip() or name


# ------------------------------------------------------------------ scanning


class Scanner:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.library = Library()
        self._lock = threading.Lock()
        self._scanning = threading.Lock()

    @property
    def scanning(self) -> bool:
        return self._scanning.locked()

    def scan(self) -> Library:
        with self._scanning:
            started = time.monotonic()
            try:
                lib = self._build()
            except Exception as exc:  # keep serving the old index
                log.exception("scan failed")
                with self._lock:
                    self.library.error = str(exc)
                return self.library
            lib.scanned_at = time.time()
            lib.scan_seconds = round(time.monotonic() - started, 2)
            with self._lock:
                self.library = lib
            log.info(
                "scanned %d models, %d files in %.2fs",
                len(lib.models), len(lib.files), lib.scan_seconds,
            )
            return lib

    # -- internals
    def _walk(self, root: Path):
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(
                d for d in dirnames if not d.startswith(".") and d.lower() not in SKIP_DIRS
            )
            for fn in sorted(filenames):
                if fn.startswith(".") or fn.startswith("~$"):
                    continue
                yield Path(dirpath) / fn

    def _load_file(self, path: Path, relpath: str) -> LibFile | None:
        kind = formats.classify_name(path.name)
        if kind == "other":
            return None
        try:
            st = path.stat()
        except OSError:
            return None
        f = LibFile(
            id=make_id(relpath),
            relpath=relpath,
            name=path.name,
            size=st.st_size,
            mtime=st.st_mtime,
            kind=kind,
        )
        if kind in ("3mf", "sliced3mf", "gcode"):
            cached = self.store.cached_info(relpath, st.st_mtime, st.st_size)
            if cached is not None:
                info = formats.FileInfo.from_json(cached)
            else:
                info = formats.read_info(path)
                self.store.put_info(relpath, st.st_mtime, st.st_size, info.to_json())
            f.info = info
            f.kind = info.kind if info.kind != "other" else kind
            if f.kind == "3mf":
                f.kind = "project3mf"
            if f.kind in ("sliced3mf", "gcode"):
                prof = self.settings.printer_for(info.printer_model, f.kind)
                f.printer = prof.id if prof else ""
        return f

    def _build(self) -> Library:
        root = self.settings.library
        lib = Library()
        if not root.is_dir():
            lib.error = f"Library folder {root} not found. Check the volume mount."
            return lib

        overrides = self.store.get("category_overrides", {}) or {}
        # model key -> Model, built in two passes: folders, then loose files
        folder_models: dict[str, Model] = {}
        loose: dict[str, list[LibFile]] = {}  # category -> files
        seen: set[str] = set()

        for path in self._walk(root):
            rel = path.relative_to(root)
            parts = rel.parts
            relpath = rel.as_posix()
            f = self._load_file(path, relpath)
            if f is None:
                continue
            seen.add(relpath)
            lib.files[f.id] = f
            if len(parts) == 1:
                loose.setdefault(UNCATEGORIZED, []).append(f)
            elif len(parts) == 2:
                loose.setdefault(parts[0], []).append(f)
            else:
                key = f"{parts[0]}/{parts[1]}"
                m = folder_models.get(key)
                if m is None:
                    m = folder_models[key] = Model(
                        id=make_id(key), name=parts[1], category=parts[0], relpath=key
                    )
                m.files.append(f)

        for m in folder_models.values():
            self._add_model(lib, m, root, overrides)
        for cat, files in loose.items():
            for m in self._group_loose(cat, files):
                self._add_model(lib, m, root, overrides)

        # categories with no models still show (empty folders)
        for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if entry.is_dir() and not entry.name.startswith(".") and entry.name.lower() not in SKIP_DIRS:
                self._category(lib, entry.name, root, overrides)

        for cat in lib.categories.values():
            cat.models.sort(key=lambda m: m.name.lower())
        self.store.prune_info(seen)
        return lib

    def _category(self, lib: Library, name: str, root: Path, overrides: dict) -> Category:
        cat = lib.categories.get(name)
        if cat:
            return cat
        icon, color = icons.default_icon(name), icons.default_color(name)
        meta_file = root / name / "_category.json"
        if meta_file.is_file():
            try:
                meta = json.loads(meta_file.read_text("utf-8"))
                icon = meta.get("icon", icon)
                color = meta.get("color", color)
            except (OSError, ValueError):
                pass
        ov = overrides.get(name, {})
        cat = Category(name=name, icon=ov.get("icon", icon), color=ov.get("color", color))
        lib.categories[name] = cat
        return cat

    def _add_model(self, lib: Library, m: Model, root: Path, overrides: dict) -> None:
        if not any(f.kind in ("stl", "project3mf", "sliced3mf", "gcode") for f in m.files):
            return  # a folder of only pictures is not a model
        lib.models[m.id] = m
        self._category(lib, m.category, root, overrides).models.append(m)

    def _group_loose(self, category: str, files: list[LibFile]) -> list[Model]:
        """Group files that sit directly in a category folder into models."""
        groups: dict[str, list[LibFile]] = {}
        names: dict[str, str] = {}
        # Anchor groups on STLs and project 3MFs, then attach the rest.
        anchors = sorted(
            (f for f in files if f.kind in ("stl", "project3mf")), key=lambda f: f.kind != "stl"
        )
        others = [f for f in files if f.kind not in ("stl", "project3mf")]
        for f in anchors:
            key = group_key(f.name)
            groups.setdefault(key, []).append(f)
            names.setdefault(key, pretty_name(f.name))
        stl_names = {
            stem_of(f.name).lower(): group_key(f.name) for f in anchors if f.kind == "stl"
        }
        for f in others:
            key = None
            if f.info:  # object names inside the sliced file point at the STL
                for obj in f.info.objects:
                    k = stl_names.get(stem_of(obj).lower())
                    if k:
                        key = k
                        break
            if key is None:
                gk = group_key(f.name)
                key = gk if gk in groups else None
                if key is None:  # prefix match: "wrench holder big" -> "wrench holder"
                    for k in sorted(groups, key=len, reverse=True):
                        if k and (gk.startswith(k + " ") or gk == k):
                            key = k
                            break
            if key is None:
                key = group_key(f.name)
                names.setdefault(key, pretty_name(f.name))
            groups.setdefault(key, []).append(f)
        models = []
        prefix = "" if category == UNCATEGORIZED else category + "/"
        for key, members in groups.items():
            rel = prefix + "#" + key
            models.append(
                Model(
                    id=make_id(rel),
                    name=names.get(key) or key,
                    category=category,
                    relpath=rel,
                    files=members,
                )
            )
        return models

    # -- queries
    def resolve(self, file_id: str) -> tuple[LibFile, Path] | None:
        f = self.library.files.get(file_id)
        if not f:
            return None
        root = self.settings.library.resolve()
        path = (root / f.relpath).resolve()
        if root not in path.parents:
            return None
        return f, path
