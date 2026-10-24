"""Verify a region capture against a full-screen capture (catches duplication bugs)."""
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
    parser.add_argument("--save", default=os.path.join(HERE, "probe_out", "fullscreen.png"))
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
    window.bring_to_front(game)
    time.sleep(0.6)
    left, top, width, height = window.client_rect(game)
    print(f"[i] client rect {left},{top},{width},{height}")

    cam = dxcam.create(output_idx=0, output_color="BGR")
    full = None
    for _ in range(50):
        full = cam.grab()
        if full is not None:
            break
        time.sleep(0.05)
    if full is None:
        print("[!] full-screen grab returned nothing")
        return 1
    full = np.asarray(full)
    print(f"[i] full screen {full.shape}")
    os.makedirs(os.path.dirname(args.save), exist_ok=True)
    imgio.imwrite(args.save, full)

    crop = full[top: top + height, left: left + width]
    imgio.imwrite(args.save.replace(".png", "_crop.png"), crop)

    region = None
    for _ in range(50):
        region = cam.grab(region=(left, top, left + width, top + height))
        if region is not None:
            break
        time.sleep(0.05)
    cam.release()
    if region is None:
        print("[!] region grab returned nothing")
        return 1
    region = np.asarray(region)

    if crop.shape == region.shape:
        diff = float(np.abs(crop.astype(np.int16) - region.astype(np.int16)).mean())
        print(f"[i] full-screen crop vs region grab: mean abs diff {diff:.2f}")
    half = width // 2
    left_half = crop[:, :half].astype(np.float32)
    right_half = crop[:, half:2 * half].astype(np.float32)
    print(f"[i] crop halves mean abs diff: {float(np.abs(left_half - right_half).mean()):.2f}")

    # where else on the desktop does this content appear? (cheap check with a coarse hash)
    small = cv2.resize(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), (32, 32)).astype(np.float32)
    screen_small = cv2.resize(cv2.cvtColor(full, cv2.COLOR_BGR2GRAY), (160, 90)).astype(np.float32)
    crop_small = cv2.resize(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), (16, 9)).astype(np.float32)
    result = cv2.matchTemplate(screen_small, crop_small, cv2.TM_CCOEFF_NORMED)
    _, best, _, best_loc = cv2.minMaxLoc(result)
    scale = full.shape[1] / 160.0
    print(f"[i] game content best match on desktop: score={best:.3f} at ({best_loc[0]*scale:.0f},{best_loc[1]*scale:.0f})")
    print(f"    (expected near ({left},{top}) - a second match elsewhere means the picture is duplicated)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
