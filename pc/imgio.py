"""Image file helpers that survive non-ASCII paths.

`cv2.imread` / `cv2.imwrite` silently fail on paths with CJK characters (they pass
the path through the ANSI API), which this project's own folder name has.  Going
through ``numpy.fromfile``/``tofile`` plus ``imdecode``/``imencode`` keeps the exact
bytes and works everywhere.
"""
from __future__ import annotations

import os

import numpy as np


def _cv2():
    import cv2

    return cv2


def imread(path: str, flags: int | None = None) -> np.ndarray | None:
    cv2 = _cv2()
    if not os.path.exists(path):
        return None
    data = np.fromfile(path, dtype=np.uint8)
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR if flags is None else flags)


def imwrite(path: str, image: np.ndarray) -> bool:
    cv2 = _cv2()
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    extension = os.path.splitext(path)[1] or ".png"
    ok, buffer = cv2.imencode(extension, image)
    if not ok:
        return False
    buffer.tofile(path)
    return True
