"""Print a region of the game screen as contrast-stretched ASCII art."""
from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pc"))

RAMP = " .:-=+*#%@"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rect", default="0,650,960,300", help="x,y,w,h inside the game client area")
    parser.add_argument("--cols", type=int, default=120)
    parser.add_argument("--gain", type=float, default=1.0)
    parser.add_argument("--save", default="")
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
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
    low, high = float(gray.min()), float(gray.max())
    print(f"[i] region {args.rect} mean={gray.mean():.2f} min={low:.0f} max={high:.0f}")
    scaled = (gray - low) / max(1.0, high - low) * 255.0
    scaled = np.clip(scaled * args.gain, 0, 255)
    if args.save:
        imgio.imwrite(args.save, frame)
        print(f"[i] saved {args.save}")

    rows = max(1, int(args.cols * h / w / 2))
    ys = np.linspace(0, h, rows + 1).astype(int)
    xs = np.linspace(0, w, args.cols + 1).astype(int)
    for r in range(rows):
        line = []
        for c in range(args.cols):
            block = scaled[ys[r]: ys[r + 1], xs[c]: xs[c + 1]]
            value = float(block.mean()) if block.size else 0.0
            line.append(RAMP[min(len(RAMP) - 1, int(value / 256 * len(RAMP)))])
        print("".join(line))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
