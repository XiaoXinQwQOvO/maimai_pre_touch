"""Measure the big bright disc in a screen region (the game's panel circle)."""
from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pc"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="")
    parser.add_argument("--roi", default="0.0,0.5,0.55,1.0")
    parser.add_argument("--threshold", type=float, default=110.0)
    args = parser.parse_args()

    import cv2

    import imgio

    if args.image:
        frame = imgio.imread(args.image)
    else:
        import panel_detect

        frame = panel_detect.frame_from_window()
    if frame is None:
        print("[!] no frame")
        return 1
    height, width = frame.shape[:2]
    x0, y0, x1, y1 = (float(v) for v in args.roi.split(","))
    crop = frame[int(y0 * height): int(y1 * height), int(x0 * width): int(x1 * width)]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    print(f"[i] frame {width}x{height} roi={args.roi} crop={crop.shape[1]}x{crop.shape[0]} mean={gray.mean():.1f}")

    for threshold in (args.threshold, 80, 140, 180):
        mask = (gray > threshold).astype(np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
        count, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
        print(f"  threshold>{threshold:.0f}:")
        rows = []
        for i in range(1, count):
            x, y, w, h, area = stats[i]
            if area < 2000:
                continue
            fill = area / (w * h)
            circularity = 4 * area / (np.pi * max(1, w * h))
            rows.append((area, x, y, w, h, fill, circularity))
        rows.sort(reverse=True)
        for area, x, y, w, h, fill, circ in rows[:5]:
            gx = x + w / 2 + int(x0 * width)
            gy = y + h / 2 + int(y0 * height)
            print(
                f"    area={area:7d} bbox=({x:3d},{y:3d}) {w:3d}x{h:3d} fill={fill:.2f} "
                f"circ~{circ:.2f} centre_px=({gx:6.1f},{gy:6.1f}) centre_rel=({gx/width:.4f},{gy/height:.4f}) "
                f"r_from_bbox={(w+h)/4/width:.4f} r_from_area={np.sqrt(area/np.pi)/width:.4f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
