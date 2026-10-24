"""Find persistent circles/rings in the game screen and report their geometry.

    python tools/find_circles.py --seconds 20 --roi 0,480,480,480
"""
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
    parser.add_argument("--seconds", type=float, default=15.0)
    parser.add_argument("--fps", type=float, default=3.0)
    parser.add_argument("--roi", default="0,0,960,960", help="x,y,w,h inside the game client area")
    parser.add_argument("--scale", type=float, default=0.5)
    parser.add_argument("--min-r", type=float, default=12.0)
    parser.add_argument("--max-r", type=float, default=460.0)
    parser.add_argument("--param2", type=float, default=30.0)
    args = parser.parse_args()

    import cv2
    import dxcam

    import window

    window.set_dpi_aware()
    game = window.find_game_window()
    if not game:
        print("[!] no game window")
        return 1
    left, top, width, height = window.client_rect(game)
    x, y, w, h = (int(v) for v in args.roi.split(","))
    cam = dxcam.create(output_idx=0, output_color="BGR")
    deadline = time.time() + args.seconds
    hits: dict[tuple[int, int, int], int] = {}
    frames = 0
    while time.time() < deadline:
        frame = None
        for _ in range(20):
            frame = cam.grab(region=(left + x, top + y, left + x + w, top + y + h))
            if frame is not None:
                break
            time.sleep(0.03)
        if frame is None:
            continue
        frames += 1
        gray = cv2.cvtColor(np.asarray(frame), cv2.COLOR_BGR2GRAY)
        if args.scale != 1.0:
            gray = cv2.resize(gray, None, fx=args.scale, fy=args.scale, interpolation=cv2.INTER_AREA)
        gray = cv2.medianBlur(gray, 5)
        circles = cv2.HoughCircles(
            gray, cv2.HOUGH_GRADIENT, dp=1.2, minDist=30,
            param1=100, param2=args.param2,
            minRadius=int(args.min_r * args.scale), maxRadius=int(args.max_r * args.scale),
        )
        if circles is None:
            time.sleep(1.0 / args.fps)
            continue
        for cx, cy, r in np.round(circles[0]).astype(int):
            key = (round(cx / 8) * 8 * 0 + int(round(cx / 12) * 12), int(round(cy / 12) * 12), int(round(r / 8) * 8))
            hits[key] = hits.get(key, 0) + 1
        time.sleep(1.0 / args.fps)
    cam.release()

    print(f"[i] analysed {frames} frames of roi {args.roi}")
    if not hits:
        print("[i] no circles found")
        return 0
    print("[i] circles (screen px, client coords), sorted by how often they appear:")
    for (cx, cy, r), count in sorted(hits.items(), key=lambda kv: -kv[1])[:15]:
        share = count / max(1, frames)
        gx, gy = cx / args.scale + x, cy / args.scale + y
        print(
            f"    centre=({gx:6.1f},{gy:6.1f}) r={r/args.scale:6.1f} "
            f"seen={count:3d}/{frames} ({share*100:4.0f}%)  relative=({gx/width:.3f},{gy/height:.3f}) r_rel={r/args.scale/width:.3f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
