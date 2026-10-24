"""Print bright/dark runs along a row and a column (geometry probe).

    python tools/profile_runs.py --image frame.png --row 690 --col 241 --threshold 100
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pc"))


def runs(values: np.ndarray, threshold: float) -> list[tuple[bool, int, int, float]]:
    out: list[tuple[bool, int, int, float]] = []
    bright = values > threshold
    start = 0
    for index in range(1, len(values) + 1):
        if index == len(values) or bright[index] != bright[start]:
            out.append((bool(bright[start]), start, index - 1, float(values[start:index].mean())))
            start = index
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="")
    parser.add_argument("--row", type=int, default=-1)
    parser.add_argument("--col", type=int, default=-1)
    parser.add_argument("--threshold", type=float, default=100.0)
    parser.add_argument("--min-run", type=int, default=4)
    args = parser.parse_args()

    import cv2

    import imgio
    import panel_detect

    frame = imgio.imread(args.image) if args.image else panel_detect.frame_from_window()
    if frame is None:
        print("[!] no frame")
        return 1
    if frame.ndim == 3 and frame.shape[2] == 4:
        frame = frame[:, :, :3]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape
    row = args.row if args.row >= 0 else height // 2
    col = args.col if args.col >= 0 else width // 4

    for name, values in (("row", gray[row, :]), ("col", gray[:, col])):
        print(f"[{name} {'y' if name == 'row' else 'x'}={row if name == 'row' else col}] runs (thr>{args.threshold:.0f}):")
        for bright, start, end, mean in runs(values, args.threshold):
            if end - start + 1 < args.min_run:
                continue
            print(f"    {'BRIGHT' if bright else 'dark  '} {start:4d}..{end:4d} len={end - start + 1:4d} mean={mean:6.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
