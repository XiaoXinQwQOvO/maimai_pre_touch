"""Print the top and bottom edge of the brightest region per column (geometry probe)."""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pc"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="")
    parser.add_argument("--step", type=int, default=20)
    parser.add_argument("--threshold", type=float, default=122.0)
    parser.add_argument("--x-max", type=int, default=480)
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
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    mask = gray > args.threshold
    mask = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8)).astype(bool)
    print(f"[i] {gray.shape[1]}x{gray.shape[0]} threshold {args.threshold:.0f}")
    print("     x |  top | bottom |  height")
    for x in range(0, min(args.x_max, gray.shape[1]), args.step):
        rows = np.nonzero(mask[:, x])[0]
        if not rows.size:
            print(f"  {x:4d} |    - |      - |       -")
            continue
        top, bottom = int(rows[0]), int(rows[-1])
        print(f"  {x:4d} | {top:4d} | {bottom:6d} | {bottom - top:6d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
