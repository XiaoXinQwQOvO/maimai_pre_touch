"""Sample the game screen over time and report state changes (brightness + main blobs)."""
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
    parser.add_argument("--seconds", type=float, default=40.0)
    parser.add_argument("--interval", type=float, default=2.0)
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
    print(f"[i] client rect {left},{top},{width},{height}")
    cam = dxcam.create(output_idx=0, output_color="BGR")
    deadline = time.time() + args.seconds
    last = None
    while time.time() < deadline:
        frame = None
        for _ in range(20):
            frame = cam.grab(region=(left, top, left + width, top + height))
            if frame is not None:
                break
            time.sleep(0.05)
        if frame is None:
            print("[!] no frame")
            continue
        frame = np.asarray(frame)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        mask = (gray > 60).astype(np.uint8)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        big = sorted((tuple(int(v) for v in stats[i]) for i in range(1, count)), key=lambda s: -s[4])[:4]
        desc = " | ".join(f"{w}x{h}@{x},{y} a={a}" for x, y, w, h, a in big)
        state = f"mean={gray.mean():6.2f} blobs={len(big)} {desc}"
        if state[:14] != (last or "")[:14]:
            print(f"[{time.strftime('%H:%M:%S')}] {state}", flush=True)
        last = state
        time.sleep(args.interval)
    cam.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
