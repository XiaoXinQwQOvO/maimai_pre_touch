"""Compare panel-circle estimators on one frame (debug aid).

    python tools/panel_fit.py --image tools/probe_out/panel_overlay_raw.png
    python tools/panel_fit.py                  # capture now
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pc"))

import panel_detect  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="")
    parser.add_argument("--roi", default="0.0,0.30,0.55,1.0")
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
    x0, y0, x1, y1 = (float(v) for v in args.roi.split(","))
    crop = frame[int(y0 * height): int(y1 * height), int(x0 * width): int(x1 * width)]
    ox, oy = int(x0 * width), int(y0 * height)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)

    print(f"[i] frame {width}x{height} crop {crop.shape[1]}x{crop.shape[0]} mean={gray.mean():.1f}")
    for threshold in (80, 100, 120, 140, 160):
        mask = (gray > threshold).astype(np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        if not contours:
            continue
        contour = max(contours, key=cv2.contourArea)
        area = cv2.contourArea(contour)
        if area < 3000:
            continue
        (ex, ey), (d1, d2), angle = cv2.fitEllipse(contour)
        # refit without the points far from the first ellipse (clips / glue)
        points = contour.reshape(-1, 2).astype(np.float64)
        best = None
        for _ in range(3):
            angle_rad = np.radians(angle)
            cos_a, sin_a = np.cos(angle_rad), np.sin(angle_rad)
            dx = points[:, 0] - ex
            dy = points[:, 1] - ey
            u = dx * cos_a + dy * sin_a
            v = -dx * sin_a + dy * cos_a
            r = (u / (d1 / 2)) ** 2 + (v / (d2 / 2)) ** 2
            keep = np.abs(r - 1.0) < 0.35
            if keep.sum() < 30:
                break
            best = points[keep]
            (ex, ey), (d1, d2), angle = cv2.fitEllipse(best.reshape(-1, 1, 2).astype(np.float32))
        print(
            f"  thr>{threshold:3d}: area={area:8.0f} centre=({ex + ox:6.1f},{ey + oy:6.1f}) "
            f"axes=({d1:6.1f},{d2:6.1f}) angle={angle:6.1f} stretch={max(d1,d2)/min(d1,d2):.3f} "
            f"inliers={0 if best is None else len(best)}"
        )

    detected = panel_detect.detect_disc(frame)
    print(f"  detect_disc: {detected}")
    if detected:
        print(
            f"    -> centre ({detected.cx*width:.1f},{detected.cy*height:.1f}) "
            f"rx={detected.rx*width:.1f} ry={detected.ry*height:.1f} stretch={detected.stretch:.3f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
