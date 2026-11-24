"""Saved print orientation per STL, kept in the app's own database.

The STL file is never changed: the slicer turns a copy just before slicing
(slicer/orient.py). A rotation is 9 numbers, a 3x3 matrix row by row
(p' = R p, the STL's own Z-up coordinates). Stored by file id, which comes
from the file's path, so Replace STL (same name) keeps it; rename and move
carry it over.
"""

from __future__ import annotations

import math

from .store import Store

KEY = "stl_orientation"
IDENTITY = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)


def clean(value) -> list[float] | None:
    """A rotation as 9 rounded numbers, or None for "as the file is".
    Raises ValueError for anything that isn't a proper rotation."""
    if value is None:
        return None
    try:
        m = [float(v) for v in value]
    except (TypeError, ValueError):
        raise ValueError("A rotation is 9 numbers") from None
    if len(m) != 9 or not all(math.isfinite(v) for v in m):
        raise ValueError("A rotation is 9 numbers")
    rows = (m[0:3], m[3:6], m[6:9])
    for i in range(3):
        for j in range(3):
            if abs(sum(rows[i][k] * rows[j][k] for k in range(3)) - (i == j)) > 1e-3:
                raise ValueError("That isn't a rotation")
    det = (m[0] * (m[4] * m[8] - m[5] * m[7]) - m[1] * (m[3] * m[8] - m[5] * m[6])
           + m[2] * (m[3] * m[7] - m[4] * m[6]))
    if det < 0:
        raise ValueError("That rotation mirrors the model")
    m = [round(v, 9) + 0.0 for v in m]
    return None if all(abs(a - b) < 1e-9 for a, b in zip(m, IDENTITY)) else m


class Orientations:
    def __init__(self, store: Store):
        self.store = store

    def all(self) -> dict[str, list[float]]:
        return self.store.get(KEY, {}) or {}

    def get(self, file_id: str) -> list[float] | None:
        return self.all().get(file_id)

    def set(self, file_id: str, matrix) -> list[float] | None:
        m = clean(matrix)
        data = self.all()
        if m:
            data[file_id] = m
        else:
            data.pop(file_id, None)
        self.store.set(KEY, data)
        return m

    def migrate(self, ids: dict[str, str]) -> None:
        """Files renamed or moved: old id -> new id."""
        data = self.all()
        moved = {new: data.pop(old) for old, new in ids.items() if old in data and old != new}
        if moved:
            data.update(moved)
            self.store.set(KEY, data)
