"""Measure the game's panel circle rim precisely by scanning rays from its centre.

    python tools/panel_radial.py --image tools/probe_out/median.png
    python tools/panel_radial.py                 # capture now

Prints the rim radius per angle (so a clipped / occluded side is obvious) and the
ellipse (rx, ry) that best fits the unclipped rays.
"""
from __future__ import annotations

import argparse
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pc"))

import panel_detect  # noqa: E402


def rim_profile(gray, cx, cy, threshold=100.0, max_radius=None, step_deg=5.0):
    height, width = gray.shape
    max_radius = max_radius or int(min(width, height))
    rows = []
    for degrees in np.arange(0.0, 360.0, step_deg):
        angle = math.radians(degrees)
        dx, dy = math.cos(angle), math.sin(angle)
        last_inside = None
        clipped = False
        for radius in range(3, max_radius):
            x = int(round(cx + dx * radius))
            y = int(round(cy + dy * radius))
            if not (1 <= x < width - 1 and 1 <= y < height - 1):
                clipped = True
                break
            if gray[y, x] > threshold:
                last_inside = radius
            elif last_inside is not None and radius - last_inside > 3:
                break  # left the disc for good
        rows.append((degrees, float(last_inside) if last_inside else 0.0, clipped))
    return rows


def fit_axes(rows):
    points = [(math.radians(deg), r) for deg, r, clipped in rows if r > 5 and not clipped]
    if len(points) < 8:
        return None
    angles = np.array([p[0] for p in points])
    radii = np.array([p[1] for p in points])
    design = np.column_stack([np.cos(angles) ** 2, np.sin(angles) ** 2])
    target = 1.0 / (radii ** 2)
    solution, *_ = np.linalg.lstsq(design, target, rcond=None)
    if solution[0] <= 1e-9 or solution[1] <= 1e-9:
        return None
    return 1.0 / math.sqrt(solution[0]), 1.0 / math.sqrt(solution[1])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="")
    parser.add_argument("--threshold", type=float, default=100.0)
    parser.add_argument("--centre", default="", help="cx,cy as pixels (default: detect)")
    args = parser.parse_args()

    import cv2

    import imgio

    if args.image:
        frame = imgio.imread(args.image)
    else:
        frame = panel_detect.frame_from_window()
    if frame is None:
        print("[!] no frame")
        return 1
    if frame.ndim == 3 and frame.shape[2] == 4:
        frame = frame[:, :, :3]
    height, width = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(float)

    if args.centre:
        cx, cy = (float(v) for v in args.centre.split(","))
        print(f"[i] centre given: ({cx:.1f}, {cy:.1f})")
    else:
        circle = panel_detect.detect_disc(frame)
        if circle is None:
            print("[!] no circle detected; pass --centre")
            return 2
        cx, cy = circle.cx * width, circle.cy * height
        print(f"[i] detected centre ({cx:.1f}, {cy:.1f}) rx={circle.rx*width:.1f} ry={circle.ry*height:.1f}")

    rows = rim_profile(gray, cx, cy, threshold=args.threshold)
    print(f"     degrees | rim radius (px)   (c = ray left the frame)")
    for deg, radius, clipped in rows:
        bar = "#" * int(radius / 8)
        print(f"      {deg:6.1f} | {radius:6.1f} {'c' if clipped else ' '} {bar}")
    radii = [r for _, r, clipped in rows if r > 5 and not clipped]
    if radii:
        print(f"\n[i] unclipped rays: {len(radii)}  min={min(radii):.1f}  max={max(radii):.1f}  median={np.median(radii):.1f}")
    axes = fit_axes(rows)
    if axes:
        rx, ry = axes
        print(f"[i] ellipse fit: rx={rx:.1f} ry={ry:.1f} stretch={rx/ry:.4f} (effective r={(rx+ry)/2:.1f})")
    else:
        print("[!] not enough unclipped rays for an ellipse fit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
