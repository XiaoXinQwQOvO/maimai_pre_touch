"""Look at what the game draws in a screen corner (numeric + ASCII, no eyes needed).

    python tools/probe_corner.py --region 800,240,960,960 --corner bl --out tools/probe_out/corner.png

Prints luminance statistics, an ASCII luminance map, and Hough circle candidates
for the requested corner, and saves PNG crops for reference.
"""
from __future__ import annotations

import argparse
import os
import sys

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
    parser.add_argument("--corner", default="bl", choices=["tl", "tr", "bl", "br", "all"])
    parser.add_argument("--out", default=os.path.join(HERE, "probe_out", "corner.png"))
    parser.add_argument("--warmup", type=int, default=3, help="frames to throw away")
    parser.add_argument("--wait", type=float, default=0.0, help="wait up to N s for the black screen to end")
    parser.add_argument("--bright", type=float, default=12.0, help="mean luminance that counts as 'not black'")
    args = parser.parse_args()

    import cv2

    import imgio
    import dxcam

    camera = dxcam.create(output_idx=0, output_color="BGR")
    left, top, width, height = (int(v) for v in args.region.split(","))
    if args.wait > 0:
        import time

        deadline = time.time() + args.wait
        while time.time() < deadline:
            probe = camera.grab(region=(left, top, left + width, top + height))
            if probe is not None:
                gray = cv2.cvtColor(np.asarray(probe), cv2.COLOR_BGR2GRAY)
                mean = float(gray.mean())
                print(f"[i] wait: mean={mean:.2f} std={gray.std():.2f}", end="\r")
                if mean > args.bright:
                    print(f"\n[i] screen is up (mean={mean:.2f})")
                    break
            time.sleep(1.0)
    frame = None
    for attempt in range(60):
        frame = camera.grab(region=(left, top, left + width, top + height))
        if frame is not None:
            break
        import time as _t

        _t.sleep(0.1)
    if frame is None:
        print("[!] capture returned nothing")
        return 1
    frame = np.asarray(frame)
    print(f"[i] frame {frame.shape} dtype={frame.dtype} mean={frame.mean():.1f}")
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    imgio.imwrite(args.out, frame)
    print(f"[i] full frame -> {args.out}")
    print("[i] full frame luminance map (60 cols):")
    print(ascii_map(gray, 60))

    corners = {
        "tl": (0, 0),
        "tr": (w // 2, 0),
        "bl": (0, h // 2),
        "br": (w // 2, h // 2),
    }
    wanted = list(corners) if args.corner == "all" else [args.corner]
    for name in wanted:
        x0, y0 = corners[name]
        crop = frame[y0: y0 + h // 2, x0: x0 + w // 2]
        crop_gray = gray[y0: y0 + h // 2, x0: x0 + w // 2]
        out = args.out.replace(".png", f"_{name}.png")
        imgio.imwrite(out, crop)
        print(f"\n=== corner {name} (offset {x0},{y0}) -> {out}")
        print(f"    mean={crop_gray.mean():.1f} min={crop_gray.min()} max={crop_gray.max()} std={crop_gray.std():.1f}")
        print(ascii_map(crop_gray, 48))

        blurred = cv2.medianBlur(crop_gray, 5)
        for p1, p2 in ((120, 40), (80, 30), (160, 60)):
            circles = cv2.HoughCircles(
                blurred, cv2.HOUGH_GRADIENT, dp=1.2, minDist=crop_gray.shape[0] // 8,
                param1=p1, param2=p2, minRadius=crop_gray.shape[0] // 40, maxRadius=crop_gray.shape[0] // 2,
            )
            if circles is None:
                print(f"    hough p1={p1} p2={p2}: none")
                continue
            found = np.round(circles[0], 1)[:6]
            desc = ", ".join(f"(x={cx + x0:.0f}, y={cy + y0:.0f}, r={r:.0f})" for cx, cy, r in found)
            print(f"    hough p1={p1} p2={p2}: {len(circles[0])} -> {desc}")
    camera.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
