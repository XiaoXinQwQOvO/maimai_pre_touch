"""Fit a circle to the upper arc of the game's disc (clean, unclipped boundary).

    python tools/arc_fit.py --image frame.png
"""
from __future__ import annotations

import argparse
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pc"))


def kasa(xs: np.ndarray, ys: np.ndarray) -> tuple[float, float, float, float]:
    a = np.column_stack([xs, ys, np.ones_like(xs)])
    b = xs * xs + ys * ys
    solution, *_ = np.linalg.lstsq(a, b, rcond=None)
    cx = solution[0] / 2.0
    cy = solution[1] / 2.0
    r = math.sqrt(max(1e-9, solution[2] + cx * cx + cy * cy))
    distances = np.hypot(xs - cx, ys - cy)
    return cx, cy, r, float(np.sqrt(np.mean((distances - r) ** 2)))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="")
    parser.add_argument("--roi", default="0.0,0.20,0.60,1.0")
    parser.add_argument("--threshold", type=float, default=0.0)
    parser.add_argument("--min-top", type=float, default=0.30)
    parser.add_argument("--max-top", type=float, default=0.80)
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
    height, width = frame.shape[:2]
    x0r, y0r, x1r, y1r = (float(v) for v in args.roi.split(","))
    x0, y0 = int(x0r * width), int(y0r * height)
    x1, y1 = int(x1r * width), int(y1r * height)
    gray = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    otsu, _ = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    threshold = args.threshold or max(float(otsu), 90.0)
    mask = (gray > threshold).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8)).astype(bool)

    xs: list[float] = []
    ys: list[float] = []
    for x in range(mask.shape[1]):
        rows = np.nonzero(mask[:, x])[0]
        if not rows.size:
            continue
        top = float(rows[0]) + y0
        if args.min_top * height <= top <= args.max_top * height:
            xs.append(float(x + x0))
            ys.append(top)
    if len(xs) < 20:
        print("[!] not enough arc points")
        return 1
    xs_array = np.array(xs)
    ys_array = np.array(ys)
    print(f"[i] arc points {len(xs)}  x {xs_array.min():.0f}..{xs_array.max():.0f}  y {ys_array.min():.0f}..{ys_array.max():.0f}")
    print(f"[i] threshold {threshold:.0f}")

    cx, cy, r, rms = kasa(xs_array, ys_array)
    for _ in range(4):
        keep = np.abs(np.hypot(xs_array - cx, ys_array - cy) - r) < max(1.5, 3.0 * rms)
        if keep.sum() < 12:
            break
        xs_array, ys_array = xs_array[keep], ys_array[keep]
        cx, cy, r, rms = kasa(xs_array, ys_array)
    span = math.degrees(2.0 * math.asin(min(1.0, (xs_array.max() - xs_array.min()) / (2.0 * r))))
    print(f"[i] inliers {len(xs_array)}  rms {rms:.2f}px  arc span {span:.0f} deg")
    print(
        f"[i] => centre ({cx:.1f}, {cy:.1f}) r={r:.1f}px  "
        f"frac cx={cx / width:.4f} cy={cy / height:.4f} rx=ry={r / width:.4f}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
