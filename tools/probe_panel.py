"""Look at the game window cleanly: hide overlapping consoles, raise the game, then
describe the picture numerically (blob stats, ASCII luminance map, circle fits).

    python tools/probe_panel.py --wait 60 --corner bl
    python tools/probe_panel.py --ascii-only --cols 80
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pc"))

RAMP = " .:-=+*#%@"
user32 = ctypes.WinDLL("user32", use_last_error=True)


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


def hide_overlapping_consoles() -> list[str]:
    """Minimise console windows of mice.exe / maimai so they stop covering the game."""
    hidden = []
    EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _l):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 2)
        user32.GetWindowTextW(hwnd, buf, length + 2)
        title = buf.value
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if "CASCADIA" in cls.value.upper() or "CONSOLE" in cls.value.upper():
            lowered = title.lower()
            if "mice.exe" in lowered or "maimai" in lowered:
                user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
                hidden.append(f"{cls.value}: {title}")
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return hidden


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait", type=float, default=0.0, help="wait up to N s for a non-black screen")
    parser.add_argument("--bright", type=float, default=25.0)
    parser.add_argument("--cols", type=int, default=70)
    parser.add_argument("--corner", default="bl", choices=["none", "tl", "tr", "bl", "br", "bottom"])
    parser.add_argument("--save", default=os.path.join(HERE, "probe_out", "panel.png"))
    parser.add_argument("--no-hide", action="store_true")
    args = parser.parse_args()

    import cv2
    import dxcam

    import imgio
    import window

    window.set_dpi_aware()
    game = window.find_game_window()
    if not game:
        print("[!] game window not found")
        return 1
    if not args.no_hide:
        hidden = hide_overlapping_consoles()
        if hidden:
            print("[i] minimised overlapping consoles: " + "; ".join(hidden))
    window.bring_to_front(game)
    time.sleep(0.8)
    region = window.client_rect(game)
    left, top, width, height = region
    print(f"[i] game window {game['title']!r} client rect {region}")

    cam = dxcam.create(output_idx=0, output_color="BGR")

    def grab():
        for _ in range(40):
            frame = cam.grab(region=(left, top, left + width, top + height))
            if frame is not None:
                return np.asarray(frame)
            time.sleep(0.05)
        return None

    frame = grab()
    if frame is None:
        print("[!] no frame")
        return 1
    if args.wait > 0:
        deadline = time.time() + args.wait
        while time.time() < deadline:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            print(f"[i] wait mean={gray.mean():.2f} std={gray.std():.2f}", end="\r")
            if gray.mean() > args.bright:
                break
            time.sleep(1.0)
            frame = grab()
        print()

    frame2 = grab()
    if frame2 is not None:
        same = float(np.mean(np.all(frame == frame2, axis=2)))
        print(f"[i] liveness: {same*100:.1f}% pixels identical 0.1s apart")

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    os.makedirs(os.path.dirname(args.save), exist_ok=True)
    imgio.imwrite(args.save, frame)
    print(f"[i] frame mean={gray.mean():.2f} std={gray.std():.2f} max={gray.max()} -> {args.save}")

    for threshold in (25, 50, 90):
        mask = (gray > threshold).astype(np.uint8)
        count, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
        blobs = []
        for i in range(1, count):
            x, y, w, h, area = stats[i]
            if area < 300:
                continue
            blob = frame[labels == i]
            mean_bgr = tuple(int(v) for v in blob.mean(axis=0))
            blobs.append((area, x, y, w, h, area / max(1, w * h), mean_bgr))
        blobs.sort(reverse=True)
        print(f"[i] threshold>{threshold}: {len(blobs)} blobs")
        for area, x, y, w, h, fill, bgr in blobs[:10]:
            cx, cy = x + w / 2, y + h / 2
            print(
                f"    area={area:6d} bbox=({x:3d},{y:3d}) {w:3d}x{h:3d} fill={fill:.2f} "
                f"centre=({cx:6.1f},{cy:6.1f}) rel=({cx/width:.3f},{cy/height:.3f}) BGR={bgr}"
            )

    print("[i] full luminance map:")
    print(ascii_map(gray, args.cols))

    if args.corner != "none":
        boxes = {
            "tl": (0, 0, width // 2, height // 2),
            "tr": (width // 2, 0, width, height // 2),
            "bl": (0, height // 2, width // 2, height),
            "br": (width // 2, height // 2, width, height),
            "bottom": (0, int(height * 0.6), width, height),
        }
        x0, y0, x1, y1 = boxes[args.corner]
        crop = frame[y0:y1, x0:x1]
        path = args.save.replace(".png", f"_{args.corner}.png")
        imgio.imwrite(path, crop)
        print(f"[i] corner {args.corner} [{x0},{y0}]-[{x1},{y1}] -> {path}")
        print(ascii_map(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), args.cols))
    cam.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
