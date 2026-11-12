"""Slicer presets: the slicer's built-in (system) profiles plus the user's own.

OrcaSlicer-family presets inherit from each other by name (``"inherits"``).
The command line wants complete settings files, so we resolve the chain here
and hand the slicer flat JSON: parent values first, child values on top.

System profiles live in ``<profiles>/<Vendor>/{machine,process,filament}/``.
User presets come from the config folder: loose ``.json`` files or exported
bundles (``.orca_printer``, ``.bbscfg``, ``.orca_filament``, ``.bbsflmt``,
``.zip``), which are zip files of the same JSON.

Pure Python, no slicer needed, so it is unit-tested directly.
"""

from __future__ import annotations

import json
import logging
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("slicer.presets")

TYPES = ("machine", "process", "filament")
BUNDLE_EXTS = {".orca_printer", ".bbscfg", ".orca_filament", ".bbsflmt", ".zip"}
# Shared filament library used by Orca-based slicers when a vendor folder lacks one.
SHARED_VENDORS = ("OrcaFilamentLibrary",)


@dataclass
class Preset:
    name: str
    type: str  # machine | process | filament
    data: dict
    source: str  # "system" or "user"
    vendor: str = ""  # vendor folder for system presets
    origin: str = ""  # file it came from, for messages

    @property
    def instantiable(self) -> bool:
        if self.source == "user":
            return True
        return str(self.data.get("instantiation", "true")).lower() == "true"


def infer_type(data: dict, hint: str = "") -> str:
    t = str(data.get("type", "")).lower()
    if t in TYPES:
        return t
    if t == "printer":
        return "machine"
    if "printer_settings_id" in data or "printer_model" in data:
        return "machine"
    if "filament_settings_id" in data or "filament_type" in data:
        return "filament"
    if "print_settings_id" in data or "layer_height" in data:
        return "process"
    hint = hint.lower()
    for key, value in (("printer", "machine"), ("machine", "machine"), ("process", "process"),
                       ("print", "process"), ("filament", "filament")):
        if key in hint:
            return value
    return ""


def _load_json(raw: bytes) -> dict | None:
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


@dataclass
class PresetLibrary:
    profiles_root: Path | None
    config_root: Path | None
    vendors: tuple[str, ...] = ("Snapmaker", "BBL")
    system: dict[tuple[str, str, str], Preset] = field(default_factory=dict)  # (vendor, type, name)
    user: dict[tuple[str, str], Preset] = field(default_factory=dict)  # (type, name)
    problems: list[str] = field(default_factory=list)

    # ------------------------------------------------------------ loading
    def load(self) -> "PresetLibrary":
        self.system.clear()
        self.user.clear()
        self.problems.clear()
        if self.profiles_root and self.profiles_root.is_dir():
            for vendor in (*self.vendors, *SHARED_VENDORS):
                self._load_vendor(vendor)
        if self.config_root and self.config_root.is_dir():
            self._load_user()
        return self

    def _load_vendor(self, vendor: str) -> None:
        folder = self.profiles_root / vendor
        if not folder.is_dir():
            return
        for path in folder.rglob("*.json"):
            data = _load_json(path.read_bytes())
            if not data or "name" not in data:
                continue
            t = infer_type(data, path.parent.name)
            if t not in TYPES:
                continue
            self.system[(vendor, t, data["name"])] = Preset(
                name=data["name"], type=t, data=data, source="system", vendor=vendor, origin=str(path)
            )

    def _load_user(self) -> None:
        for path in sorted(self.config_root.rglob("*")):
            if not path.is_file():
                continue
            ext = path.suffix.lower()
            try:
                if ext == ".json":
                    self._add_user(path.read_bytes(), path.parent.name, str(path))
                elif ext in BUNDLE_EXTS:
                    with zipfile.ZipFile(path) as zf:
                        for info in zf.infolist():
                            if info.filename.lower().endswith(".json") and not info.is_dir():
                                hint = "/".join(Path(info.filename).parts[:-1])
                                self._add_user(zf.read(info), hint, f"{path.name}:{info.filename}")
            except (OSError, zipfile.BadZipFile) as exc:
                self.problems.append(f"{path.name}: {exc}")

    def _add_user(self, raw: bytes, hint: str, origin: str) -> None:
        data = _load_json(raw)
        if not data or not data.get("name") or "bundle_structure" in origin:
            return
        t = infer_type(data, hint)
        if t not in TYPES:
            return
        self.user[(t, data["name"])] = Preset(name=data["name"], type=t, data=data, source="user", origin=origin)

    # ------------------------------------------------------------ lookup
    def find(self, t: str, name: str, vendor: str = "") -> Preset | None:
        """User presets win over system ones with the same name."""
        if (t, name) in self.user:
            return self.user[(t, name)]
        for v in ([vendor] if vendor else []) + list(self.vendors) + list(SHARED_VENDORS):
            hit = self.system.get((v, t, name))
            if hit:
                return hit
        return None

    def chain(self, preset: Preset, vendor: str = "") -> list[Preset]:
        """The preset and its ancestors, child first."""
        out = [preset]
        seen = {preset.name}
        cur = preset
        while True:
            parent_name = str(cur.data.get("inherits") or "")
            if not parent_name:
                return out
            vend = cur.vendor or vendor
            parent = None
            # A user preset may share its parent's name; look in the system set then.
            if cur.source == "user" and parent_name == cur.name:
                parent = self._system_only(cur.type, parent_name, vend)
            else:
                parent = self.find(cur.type, parent_name, vend)
                if parent is not None and parent.name in seen:
                    parent = self._system_only(cur.type, parent_name, vend)
            if parent is None:
                parent = self._similar(cur.type, parent_name, vend)
            if parent is None:
                raise PresetError(f'{cur.type} preset "{cur.name}" inherits "{parent_name}", which was not found')
            if parent.name in seen and parent.source == "user":
                raise PresetError(f'{cur.type} preset "{cur.name}" inherits in a loop')
            seen.add(parent.name)
            out.append(parent)
            cur = parent

    def _similar(self, t: str, name: str, vendor: str) -> Preset | None:
        """System preset whose name differs only in spelling.

        Snapmaker's Orca fork calls a process "0.20mm Standard @Snapmaker U1
        (0.4 nozzle)" where upstream OrcaSlicer says "0.20 Standard @...", so
        user presets made in one still find their parent in the other.
        """
        want = norm_name(name)
        for (v, tt, n), p in self.system.items():
            if tt == t and (not vendor or v == vendor) and norm_name(n) == want:
                return p
        return None

    def _system_only(self, t: str, name: str, vendor: str) -> Preset | None:
        for v in ([vendor] if vendor else []) + list(self.vendors) + list(SHARED_VENDORS):
            hit = self.system.get((v, t, name))
            if hit:
                return hit
        return None

    def flatten(self, t: str, name: str, vendor: str = "") -> dict:
        preset = self.find(t, name, vendor)
        if preset is None:
            raise PresetError(f'No {t} preset named "{name}"')
        merged: dict = {}
        for p in reversed(self.chain(preset, vendor)):
            merged.update(p.data)
        merged["name"] = preset.name
        merged["type"] = t
        # The CLI reads "inherits" of a user preset as its built-in printer or
        # process name (for compatibility checks); values are already merged.
        anc = self.system_ancestor(preset, vendor) if preset.source == "user" else None
        merged["inherits"] = anc.name if anc else ""
        merged["from"] = "User" if preset.source == "user" else "system"
        merged["instantiation"] = "true"
        return merged

    def system_ancestor(self, preset: Preset, vendor: str = "") -> Preset | None:
        for p in self.chain(preset, vendor):
            if p.source == "system":
                return p
        return None

    # ------------------------------------------------------------ choices
    def machines_for(self, printer_model: str, vendor: str) -> list[Preset]:
        out = []
        for p in self._candidates("machine", vendor):
            try:
                model = self.flatten("machine", p.name, vendor).get("printer_model", "")
            except PresetError:
                continue
            if str(model).lower() == printer_model.lower():
                out.append(p)
        return out

    def compatible(self, t: str, machine: str, vendor: str) -> list[Preset]:
        # A user printer preset is compatible with whatever its built-in parent is.
        machine_names = {machine}
        mp = self.find("machine", machine, vendor)
        if mp is not None:
            try:
                machine_names |= {p.name for p in self.chain(mp, vendor)}
            except PresetError:
                pass
        out = []
        for p in self._candidates(t, vendor):
            compat = p.data.get("compatible_printers")
            if p.source == "user" and not compat:
                anc = None
                try:
                    anc = self.system_ancestor(p, vendor)
                except PresetError:
                    pass
                if anc is None:
                    continue
                compat = anc.data.get("compatible_printers")
                if not compat and anc.vendor not in SHARED_VENDORS and anc.vendor != vendor:
                    continue
            if isinstance(compat, str):
                compat = [c for c in compat.split(";") if c]
            if compat and not machine_names.intersection(compat):
                continue
            if not compat and p.source == "system" and p.vendor in SHARED_VENDORS:
                continue  # the shared library lists printers explicitly; skip blanket ones
            out.append(p)
        return out

    def _candidates(self, t: str, vendor: str):
        for (v, tt, _), p in self.system.items():
            if tt == t and v == vendor and p.instantiable:
                yield p
        for (tt, _), p in self.user.items():
            if tt == t:
                yield p

    def filament_type(self, name: str, vendor: str) -> str:
        try:
            value = self.flatten("filament", name, vendor).get("filament_type", "")
        except PresetError:
            return ""
        if isinstance(value, list):
            value = value[0] if value else ""
        return str(value)

    def pick_filament(self, wanted_type: str, choices: list[Preset], vendor: str) -> str:
        """Best built-in filament for a material such as PLA or PETG."""
        if not choices:
            return ""
        wanted = (wanted_type or "PLA").upper().strip()
        names = [c.name for c in choices]
        w = re.escape(wanted)
        for pattern in (rf"^Snapmaker {w} @", rf"^Generic {w}$", rf"^Generic {w} @",
                        rf"^Bambu {w} Basic @", rf"^Bambu {w} @"):
            for n in names:
                if re.search(pattern, n, re.I):
                    return n
        if wanted.endswith("+"):
            return self.pick_filament(wanted[:-1], choices, vendor)
        typed = [c.name for c in choices if self.filament_type(c.name, vendor).upper() == wanted]
        for brand in ("Snapmaker", "Generic", "Bambu"):
            for n in typed:
                if n.startswith(brand):
                    return n
        if typed:
            return typed[0]
        if wanted != "PLA":
            return self.pick_filament("PLA", choices, vendor)
        return names[0]

    @staticmethod
    def pick_process(choices: list[Preset]) -> str:
        names = [c.name for c in choices]
        for pattern in (r"^0\.20\S* Standard", r"^0\.20", r"Standard"):
            for n in names:
                if re.search(pattern, n):
                    return n
        return names[0] if names else ""


def norm_name(name: str) -> str:
    name = re.sub(r"(\d)\s*mm\b", r"\1", name.lower())
    return re.sub(r"[^a-z0-9.@+]+", " ", name).strip()


class PresetError(Exception):
    pass
