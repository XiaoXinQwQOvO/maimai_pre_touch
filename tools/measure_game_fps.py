"""How fast does the game really present?  Capture at 120 and look for duplicates."""
from __future__ import annotations

import sys
import time

sys.path.insert(0, "pc")

import numpy as np  # noqa: E402

import capture  # noqa: E402
import window  # noqa: E402


def main() -> int:
    win = window.find_game_window(("maimai_dump_.exe", "maimai.exe"), "teaGfx")
    if win is None:
        print("[!] game window not found")
        return 1
    region = tuple(win["rect"])
    print("[i] region:", region)

    for target in (60, 120, 144):
        cam = capture.CaptureManager({"backend": "dxcam", "fps": target, "output_idx": 0}, lambda: region)
        deadline = time.time() + 2.0
        frames = 0
        dupes = 0
        last = None
        times = []
        first = True
        while time.time() < deadline:
            frame = cam.get_frame()
            if frame is None:
                continue
            now = time.perf_counter()
            sample = frame[::8, ::8].astype(np.int16)
            if last is not None:
                if np.abs(sample - last).max() < 3:
                    dupes += 1
            last = sample
            frames += 1
            if not first:
                times.append(now)
            first = False
        cam.close()
        elapsed = 2.0
        gaps = np.diff(times) if len(times) > 2 else np.array([0.0])
        print(
            f"[i] target {target:3d}: {frames / elapsed:6.1f} frames/s, "
            f"{dupes:3d} duplicates, gap median {np.median(gaps) * 1000:5.2f} ms, "
            f"p90 {np.percentile(gaps, 90) * 1000:5.2f} ms"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
