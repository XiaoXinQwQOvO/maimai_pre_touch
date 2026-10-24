"""Capture a temporal-median frame (removes animation) and run panel detection on it.

    python tools/capture_median.py --frames 24 --save tools/probe_out/median.png --preview tools/probe_out/median_preview.png
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pc"))
sys.path.insert(0, HERE)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames", type=int, default=18)
    parser.add_argument("--interval", type=float, default=0.15)
    parser.add_argument("--save", default=os.path.join(HERE, "probe_out", "median.png"))
    parser.add_argument("--preview", default=os.path.join(HERE, "probe_out", "median_preview.png"))
    parser.add_argument("--r-min", type=float, default=0.05)
    parser.add_argument("--r-max", type=float, default=0.60)
    parser.add_argument("--no-detect", action="store_true")
    args = parser.parse_args()

    import cv2
    import dxcam

    import panel_detect
    import imgio
    import window

    window.set_dpi_aware()
    game = window.find_game_window()
    if not game:
        print("[!] no game window")
        return 1
    window.bring_to_front(game)
    time.sleep(0.6)
    left, top, width, height = window.client_rect(game)
    print(f"[i] client rect {left},{top},{width},{height}")

    cam = dxcam.create(output_idx=0, output_color="BGR")
    frames = []
    deadline = time.time() + args.frames * args.interval * 6 + 15
    while len(frames) < args.frames and time.time() < deadline:
        frame = cam.grab(region=(left, top, left + width, top + height))
        if frame is None:
            time.sleep(0.03)
            continue
        frames.append(np.asarray(frame))
        time.sleep(args.interval)
    cam.release()
    if not frames:
        print("[!] no frames")
        return 1

    stack = np.stack(frames).astype(np.float32)
    median = np.median(stack, axis=0).astype(np.uint8)
    std = stack.std(axis=0).mean(axis=2)
    print(f"[i] {len(frames)} frames; median mean={median.mean():.1f}; temporal std mean={std.mean():.2f}")

    os.makedirs(os.path.dirname(args.save), exist_ok=True)
    imgio.imwrite(args.save, median)
    print(f"[i] median frame -> {args.save}")
    if args.preview:
        imgio.imwrite(args.preview.replace(".png", "_raw.png"), frames[0])

    # is the picture duplicated side by side? (maimai drives two player screens)
    half = width // 2
    left_half = median[:, :half].astype(np.float32)
    right_half = median[:, half:2 * half].astype(np.float32)
    diff = float(np.abs(left_half - right_half).mean())
    print(f"[i] left/right half mean abs difference: {diff:.2f} (0 = identical halves)")

    if args.no_detect:
        return 0
    candidates = panel_detect.detect_panel(median, roi=None, r_range=(args.r_min, args.r_max))
    colours = [(0, 0, 255), (0, 200, 0), (255, 0, 0), (0, 200, 255), (255, 0, 255), (255, 255, 0)]
    annotated = median.copy()
    for i, c in enumerate(candidates[:6]):
        px, py, pr = int(c.cx * width), int(c.cy * height), int(c.r * width)
        print(f"  {i}. cx={c.cx:.4f} cy={c.cy:.4f} r={c.r:.4f} px=({px},{py}) r={pr} cov={c.coverage:.2f} sharp={c.sharpness:.2f}")
        colour = colours[i % len(colours)]
        cv2.circle(annotated, (px, py), pr, colour, 4)
        cv2.drawMarker(annotated, (px, py), colour, cv2.MARKER_CROSS, 50, 2)
        cv2.putText(annotated, f"#{i}", (px - 12, py - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.9, colour, 2, cv2.LINE_AA)
    if args.preview:
        imgio.imwrite(args.preview, annotated)
        print(f"[i] annotated -> {args.preview}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
