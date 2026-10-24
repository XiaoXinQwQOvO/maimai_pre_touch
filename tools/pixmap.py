"""Print the game screen as a colour-class map, so layout can be read as text.

Legend:  ' ' very dark   '.' dark   'k' grey   'w' white
         'R' red/orange  'Y' yellow  'G' green  'C' cyan  'B' blue  'M' magenta
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pc"))


def classify(bgr: np.ndarray) -> str:
    b, g, r = float(bgr[0]), float(bgr[1]), float(bgr[2])
    top = max(b, g, r)
    low = min(b, g, r)
    value = top / 255.0
    sat = 0.0 if top <= 0 else (top - low) / top
    if value < 0.10:
        return " "
    if value < 0.30:
        return "."
    if sat < 0.18:
        return "w" if value > 0.72 else ("k" if value > 0.34 else ":")
    # hue in degrees (BGR -> standard)
    maximum, minimum = top, low
    delta = maximum - minimum or 1.0
    if maximum == r:
        hue = (60 * ((g - b) / delta) + 360) % 360
    elif maximum == g:
        hue = 60 * ((b - r) / delta) + 120
    else:
        hue = 60 * ((r - g) / delta) + 240
    if hue < 20 or hue >= 345:
        return "R"
    if hue < 45:
        return "R" if sat > 0.45 else "Y"
    if hue < 70:
        return "Y"
    if hue < 160:
        return "G"
    if hue < 200:
        return "C"
    if hue < 265:
        return "B"
    return "M"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rect", default="0,0,960,960")
    parser.add_argument("--cols", type=int, default=76)
    parser.add_argument("--save", default="")
    parser.add_argument("--delay", type=float, default=0.0)
    args = parser.parse_args()

    import cv2
    import dxcam

    import imgio
    import window

    window.set_dpi_aware()
    game = window.find_game_window()
    if not game:
        print("[!] no game window")
        return 1
    left, top, width, height = window.client_rect(game)
    x, y, w, h = (int(v) for v in args.rect.split(","))
    cam = dxcam.create(output_idx=0, output_color="BGR")
    frame = None
    for _ in range(40):
        frame = cam.grab(region=(left + x, top + y, left + x + w, top + y + h))
        if frame is not None:
            break
        time.sleep(0.05)
    cam.release()
    if frame is None:
        print("[!] no frame")
        return 1
    frame = np.asarray(frame)
    if args.save:
        imgio.imwrite(args.save, frame)
        print(f"[i] saved {args.save}")
    rows = max(1, int(args.cols * h / w / 2))
    ys = np.linspace(0, h, rows + 1).astype(int)
    xs = np.linspace(0, w, args.cols + 1).astype(int)
    print(f"[i] rect {args.rect} mean={frame.mean():.1f}  cols={args.cols} rows={rows}")
    for r in range(rows):
        line = []
        for c in range(args.cols):
            block = frame[ys[r]: ys[r + 1], xs[c]: xs[c + 1]]
            line.append(classify(block.reshape(-1, 3).mean(axis=0)) if block.size else " ")
        print(f"{ys[r]:4d}|" + "".join(line))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
