"""Describe what is on the game screen: bright blobs with colour + shape metrics."""
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


def analyse(frame: np.ndarray, label: str) -> None:
    import cv2

    import imgio

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    print(f"\n### {label}: mean={gray.mean():.2f} std={gray.std():.2f} p99={np.percentile(gray, 99):.0f}")
    for threshold in (30, 60, 120):
        mask = (gray > threshold).astype(np.uint8)
        count, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
        blobs = []
        for i in range(1, count):
            x, y, w, h, area = stats[i]
            if area < 200:
                continue
            blob = frame[labels == i]
            mean_bgr = blob.mean(axis=0).astype(int)
            fill = area / max(1, w * h)
            blobs.append((area, x, y, w, h, fill, tuple(int(v) for v in mean_bgr)))
        blobs.sort(reverse=True)
        print(f"  threshold>{threshold}: {len(blobs)} blobs")
        for area, x, y, w, h, fill, bgr in blobs[:8]:
            print(
                f"    area={area:6d} bbox=x{x:4d} y{y:4d} {w:4d}x{h:4d} "
                f"fill={fill:.2f} centre=({x + w // 2},{y + h // 2}) BGR={bgr}"
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="800,240,960,960")
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--interval", type=float, default=1.5)
    parser.add_argument("--crop", default="bl", choices=["none", "tl", "tr", "bl", "br", "bottom"])
    parser.add_argument("--cols", type=int, default=70)
    parser.add_argument("--out", default=os.path.join(HERE, "probe_out", "blob.png"))
    args = parser.parse_args()

    import cv2
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
        analyse(frame, f"sample {i} (full)")
        if args.crop != "none":
            h, w = frame.shape[:2]
            boxes = {
                "tl": (0, 0, w // 2, h // 2),
                "tr": (w // 2, 0, w, h // 2),
                "bl": (0, h // 2, w // 2, h),
                "br": (w // 2, h // 2, w, h),
                "bottom": (0, int(h * 0.65), w, h),
            }
            x0, y0, x1, y1 = boxes[args.crop]
            crop = frame[y0:y1, x0:x1]
            path = args.out.replace(".png", f"_{i}_{args.crop}.png")
            imgio.imwrite(path, crop)
            print(f"  crop {args.crop} [{x0},{y0}]-[{x1},{y1}] -> {path}")
            print(ascii_map(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), args.cols))
        if i + 1 < args.samples:
            time.sleep(args.interval)
    cam.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
