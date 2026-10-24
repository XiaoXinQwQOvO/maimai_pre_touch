"""Grab the game window and describe it numerically (brightness, colour, shapes)."""
from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pc"))

RAMP = " .:-=+*#%@"


def ascii_map(gray: np.ndarray, cols: int = 60) -> str:
    h, w = gray.shape
    rows = max(1, int(cols * h / w / 2))
    ys = np.linspace(0, h, rows + 1).astype(int)
    xs = np.linspace(0, w, cols + 1).astype(int)
    out = []
    for r in range(rows):
        line = []
        for c in range(cols):
            block = gray[ys[r]: ys[r + 1], xs[c]: xs[c + 1]]
            value = float(block.mean()) if block.size else 0.0
            line.append(RAMP[min(len(RAMP) - 1, int(value / 256 * len(RAMP)))])
        out.append("".join(line))
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="800,240,960,960")
    parser.add_argument("--cols", type=int, default=76)
    parser.add_argument("--out", default=os.path.join(HERE, "probe_out", "shot.png"))
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args()

    import cv2

    import imgio
    import dxcam

    cam = dxcam.create(output_idx=0, output_color="BGR")
    left, top, width, height = (int(v) for v in args.region.split(","))
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    for i in range(args.samples):
        frame = None
        for _ in range(40):
            frame = cam.grab(region=(left, top, left + width, top + height))
            if frame is not None:
                break
            time.sleep(0.1)
        if frame is None:
            print("[!] no frame")
            return 1
        frame = np.asarray(frame)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        out = args.out.replace(".png", f"_{i}.png")
        imgio.imwrite(out, frame)
        print(f"\n### sample {i}: mean={gray.mean():.2f} std={gray.std():.2f} p99={np.percentile(gray,99):.0f} -> {out}")
        # bright pixel bounding box (content area)
        mask = gray > 40
        if mask.any():
            ys, xs = np.nonzero(mask)
            print(f"    content bbox: x {xs.min()}..{xs.max()}  y {ys.min()}..{ys.max()}  ({mask.mean()*100:.1f}% bright)")
        print(ascii_map(gray, args.cols))
        if i + 1 < args.samples:
            time.sleep(args.interval)
    cam.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
