"""Pick a default icon and colour for a category from its name.

Users can override both in the UI; the override is stored in the data volume,
or in a ``_category.json`` file inside the category folder.
"""

from __future__ import annotations

import hashlib
import re

# keyword -> lucide icon name. First match wins, so put specific words first.
KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (("tool", "wrench", "workshop", "garage"), "wrench"),
    (("organiz", "storage", "gridfinity", "bin", "drawer", "box"), "boxes"),
    (("kitchen", "cook", "food"), "utensils"),
    (("coffee", "mug", "cup"), "coffee"),
    (("garden", "plant", "pot", "planter"), "sprout"),
    (("toy", "game", "kid", "fidget"), "puzzle"),
    (("dice", "tabletop", "board"), "dices"),
    (("mini", "figur", "warhammer", "dnd", "d&d"), "swords"),
    (("cosplay", "costume", "prop", "helmet"), "wand-sparkles"),
    (("home", "house", "decor", "furniture"), "house"),
    (("lamp", "light", "led"), "lamp-desk"),
    (("bath", "shower"), "bath"),
    (("pet", "cat", "dog"), "paw-print"),
    (("electr", "pcb", "arduino", "raspberry", "pi case", "enclosure"), "cpu"),
    (("phone", "tablet", "stand"), "smartphone"),
    (("computer", "desk", "pc", "keyboard"), "monitor"),
    (("cable", "wire", "clip"), "cable"),
    (("camera", "photo", "tripod"), "camera"),
    (("audio", "music", "speaker", "guitar"), "music"),
    (("printer", "3d print", "upgrade", "spool", "filament", "calibrat", "test"), "printer"),
    (("car", "auto", "vehicle"), "car"),
    (("bike", "bicycle", "cycling"), "bike"),
    (("sport", "fitness", "gym"), "dumbbell"),
    (("outdoor", "camp", "hike"), "tent"),
    (("gift", "christmas", "holiday", "halloween"), "gift"),
    (("art", "sculpt", "statue", "bust"), "palette"),
    (("jewel", "ring", "earring"), "gem"),
    (("cloth", "wear", "fashion"), "shirt"),
    (("school", "edu", "learn"), "graduation-cap"),
    (("science", "lab"), "flask-conical"),
    (("book",), "book-open"),
    (("medical", "health"), "heart"),
    (("space", "rocket"), "rocket"),
    (("map", "terrain", "topo"), "map"),
    (("vase", "flower"), "flower"),
    (("part", "replacement", "repair", "fix", "spare"), "cog"),
    (("hardware", "screw", "bolt", "nut"), "nut"),
    (("uncategor", "misc", "other", "inbox"), "folder"),
]

PALETTE = [
    "#E4572E", "#F2A900", "#4CB944", "#2E86AB", "#7B61FF",
    "#E84A9B", "#17BEBB", "#F17105", "#6A994E", "#3D5A80",
]


def default_icon(name: str) -> str:
    low = name.lower()
    for words, icon in KEYWORDS:
        if any(re.search(r"\b" + re.escape(w), low) for w in words):
            return icon
    return "shapes"


def default_color(name: str) -> str:
    digest = hashlib.sha1(name.lower().encode()).digest()
    return PALETTE[digest[0] % len(PALETTE)]
