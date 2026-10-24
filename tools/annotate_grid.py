"""Annotate a captured game frame with a percentage grid so a human can read off geometry."""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pc"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default=os.path.join(HERE, "probe_out", "median.png"))
    parser.add_argument("--out", default=os.path.join(HERE, "probe_out", "median_grid.png"))
    parser.add_argument("--step", type=float, default=0.05)
    args = parser.parse_args()

    import cv2

    import imgio

    frame = imgio.imread(args.image)
    if frame is None:
        print(f"[!] cannot read {args.image}")
        return 1
    height, width = frame.shape[:2]
    out = frame.copy()
    steps = int(round(1.0 / args.step))
    for i in range(steps + 1):
        frac = i * args.step
        x = min(width - 1, int(frac * width))
        y = min(height - 1, int(frac * height))
        thick = 2 if i % 2 == 0 else 1
        colour = (0, 0, 255) if i % 2 == 0 else (120, 120, 120)
        cv2.line(out, (x, 0), (x, height - 1), colour, thick)
        cv2.line(out, (0, y), (width - 1, y), colour, thick)
        if i % 2 == 0:
            cv2.putText(out, f"{int(frac*100)}", (x + 3, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1, cv2.LINE_AA)
            cv2.putText(out, f"{int(frac*100)}", (3, min(height - 4, y + 16)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1, cv2.LINE_AA)
    imgio.imwrite(args.out, out)
    print(f"[i] {args.image} -> {args.out} ({width}x{height}, grid every {args.step*100:.0f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
