"""User tags on models, kept in the app's own database (the library stays as-is).

Tags are stored per model id. A model id comes from the model's folder path,
so tags survive rescans and restarts, and only get lost if the folder is
renamed. Tags are short lowercase words or phrases ("kitchen", "gift idea").
"""

from __future__ import annotations

import re

from .store import Store

KEY = "model_tags"
MAX_TAGS = 30
MAX_LEN = 40


def clean_tag(tag: str) -> str:
    """'  Gift  Idea! ' -> 'gift idea'. Returns '' for nothing usable."""
    tag = re.sub(r"[^\w +&.'-]+", " ", str(tag).lower(), flags=re.UNICODE)
    tag = re.sub(r"\s+", " ", tag).strip(" .-'")
    return tag[:MAX_LEN].strip()


def clean_tags(tags) -> list[str]:
    out: list[str] = []
    for t in tags or []:
        c = clean_tag(t)
        if c and c not in out:
            out.append(c)
    return out[:MAX_TAGS]


class Tags:
    def __init__(self, store: Store):
        self.store = store

    def all(self) -> dict[str, list[str]]:
        return self.store.get(KEY, {}) or {}

    def get(self, model_id: str) -> list[str]:
        return list(self.all().get(model_id, []))

    def set(self, model_id: str, tags) -> list[str]:
        data = self.all()
        clean = clean_tags(tags)
        if clean:
            data[model_id] = clean
        else:
            data.pop(model_id, None)
        self.store.set(KEY, data)
        return clean

    def add(self, model_id: str, tags) -> list[str]:
        return self.set(model_id, self.get(model_id) + list(tags or []))

    def migrate_id(self, old_id: str, new_id: str) -> None:
        data = self.all()
        if old_id in data:
            data[new_id] = data.pop(old_id)
            self.store.set(KEY, data)

    def counts(self, live_ids=None) -> list[dict]:
        """Every tag with how many models carry it, most used first."""
        counts: dict[str, int] = {}
        for mid, tags in self.all().items():
            if live_ids is not None and mid not in live_ids:
                continue
            for t in tags:
                counts[t] = counts.get(t, 0) + 1
        return [{"tag": t, "count": n} for t, n in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]
