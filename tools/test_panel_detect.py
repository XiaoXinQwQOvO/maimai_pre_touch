"""Unit test for the panel-circle detection (no game needed, synthetic frames).

    python tools/test_panel_detect.py
    python tools/test_panel_detect.py --image tools/probe_out/median.png   # also try a real frame
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pc"))

import panel_detect  # noqa: E402
import sensors  # noqa: E402

SIZE = 960
CHECKS: list[tuple[bool, str]] = []


def check(ok: bool, message: str) -> None:
    CHECKS.append((bool(ok), message))
    print(f"  [{'ok' if ok else 'FAIL'}] {message}")


def dark_frame() -> np.ndarray:
    frame = np.full((SIZE, SIZE, 3), 6, dtype=np.uint8)
    return frame


def picture_frame(cx: float, cy: float, rx: float, ry: float, header: bool = True) -> np.ndarray:
    """A stand-in for the game's waiting screen: dark background, bright band, panel disc."""
    import cv2

    frame = np.full((SIZE, SIZE, 3), 10, dtype=np.uint8)
    if header:
        cv2.rectangle(frame, (0, 0), (SIZE - 1, 225), (230, 244, 253), -1)
    cv2.ellipse(frame, (int(cx), int(cy)), (int(rx), int(ry)), 0, 0, 360, (235, 240, 248), -1)
    # a couple of dark graphics inside the disc, like the game's panel drawing
    cv2.circle(frame, (int(cx), int(cy)), int(min(rx, ry) * 0.35), (120, 80, 40), -1)
    cv2.line(frame, (int(cx - rx * 0.8), int(cy)), (int(cx + rx * 0.8), int(cy)), (60, 60, 200), 6)
    return frame


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="", help="also analyse a real saved frame")
    args = parser.parse_args()

    print("[i] disc detection on a synthetic waiting screen")
    target = (250.0, 690.0, 258.0, 258.0)
    frame = picture_frame(*target)
    circle = panel_detect.detect_disc(frame)
    check(circle is not None, "a circle is found")
    if circle is not None:
        dx = abs(circle.cx * SIZE - target[0])
        dy = abs(circle.cy * SIZE - target[1])
        drx = abs(circle.rx * SIZE - target[2]) / target[2]
        dry = abs(circle.ry * SIZE - target[3]) / target[3]
        print(
            f"      found cx={circle.cx * SIZE:.1f} cy={circle.cy * SIZE:.1f} "
            f"rx={circle.rx * SIZE:.1f} ry={circle.ry * SIZE:.1f} "
            f"(wanted {target[0]}, {target[1]}, {target[2]}, {target[3]})"
        )
        check(dx <= 6 and dy <= 6, f"centre within 6 px (dx={dx:.1f}, dy={dy:.1f})")
        check(drx <= 0.05 and dry <= 0.05, f"radii within 5 % (rx {drx:.1%}, ry {dry:.1%})")
        check(abs(circle.stretch - 1.0) <= 0.05, f"round circle detected as unstretched ({circle.stretch:.3f})")

    print("\n[i] stretched picture (the '拉缩' case)")
    stretched = (250.0, 690.0, 262.0, 214.0)
    circle = panel_detect.detect_disc(picture_frame(*stretched))
    check(circle is not None, "a circle is found on a stretched picture")
    if circle is not None:
        print(f"      rx={circle.rx * SIZE:.1f} ry={circle.ry * SIZE:.1f} stretch={circle.stretch:.3f}")
        check(
            abs(circle.rx * SIZE - stretched[2]) / stretched[2] <= 0.05
            and abs(circle.ry * SIZE - stretched[3]) / stretched[3] <= 0.05,
            f"both radii recovered (rx {circle.rx * SIZE:.1f} vs {stretched[2]}, ry {circle.ry * SIZE:.1f} vs {stretched[3]})",
        )
        check(circle.stretch > 1.15, f"stretch is reported ({circle.stretch:.3f})")

    print("\n[i] tracker state machine")
    tracker = panel_detect.PanelTracker(lock_frames=3, verify_interval=0.0)
    status = tracker.update(dark_frame(), now=0.0)
    check(status["state"] == "dark" and status["circle"] is None, "boot black screen -> state dark")
    status = tracker.update(picture_frame(*target), now=1.0)
    check(status["state"] == "searching", "first bright frame -> searching (not locked yet)")
    for i in range(2, 6):
        status = tracker.update(picture_frame(*target), now=float(i))
        if status["state"] == "locked":
            break
    check(status["state"] == "locked", "consecutive detections -> locked")
    locked = tracker.panel()
    check(locked is not None and abs(locked["cx"] - target[0] / SIZE) < 0.02, "locked panel exposes the centre")
    steady = tracker.update(picture_frame(*target), now=10.0)
    check(steady["state"] == "locked", "stays locked on further frames")
    status = tracker.update(dark_frame(), now=20.0)
    check(status["state"] == "dark" and status["circle"] is None, "black screen again -> unlocked, waits")
    status = tracker.update(picture_frame(*target), now=21.0)
    check(status["state"] in ("searching", "locked"), f"picture returns -> detecting again ({status['state']})")

    print("\n[i] touch mapping follows the detected circle")
    panel = {"cx": 250 / SIZE, "cy": 690 / SIZE, "rx": 258 / SIZE, "ry": 258 / SIZE}
    check(sensors.sensor_at(250 / SIZE, 690 / SIZE, panel) == "C", "panel centre maps to C")
    check(sensors.sensor_at((250 + 250) / SIZE, 690 / SIZE, panel) == "O0", "0 deg at the rim maps to A3 (O0)")
    check(sensors.sensor_at((250 - 250) / SIZE, 690 / SIZE, panel) == "O180", "180 deg at the rim maps to A7")
    check(sensors.sensor_at(250 / SIZE, (690 + 250) / SIZE, panel) == "O90", "90 deg (down) at the rim maps to A5")
    check(sensors.sensor_at(0.95, 0.05, panel) is None, "a point far outside the circle is ignored")
    snapped = sensors.snap_to_sensor((250 + 250) / SIZE, 690 / SIZE, panel)
    mid = (sensors.R_INNER + sensors.R_MAX) / 2
    angle = np.radians(22.5)
    want = (250 / SIZE + mid * np.cos(angle) * 258 / SIZE, 690 / SIZE + mid * np.sin(angle) * 258 / SIZE)
    check(
        abs(snapped[0] - want[0]) < 1e-6 and abs(snapped[1] - want[1]) < 1e-6,
        "snapping lands on the sensor's mid radius inside the circle",
    )

    if args.image:
        import imgio

        real = imgio.imread(args.image)
        if real is None:
            print(f"\n[!] cannot read {args.image}")
        else:
            print(f"\n[i] real frame {args.image}: {real.shape}")
            found = panel_detect.detect_disc(real)
            if found is None:
                check(False, "no circle in the real frame")
            else:
                print(
                    f"      cx={found.cx * real.shape[1]:.1f} cy={found.cy * real.shape[0]:.1f} "
                    f"rx={found.rx * real.shape[1]:.1f} ry={found.ry * real.shape[0]:.1f} "
                    f"stretch={found.stretch:.3f} coverage={found.coverage:.2f}"
                )
                check(True, "a circle is found in the real frame")

    failures = sum(1 for ok, _ in CHECKS if not ok)
    print(f"\n{len(CHECKS) - failures}/{len(CHECKS)} checks passed")
    print("PANELTEST:", "PASS" if failures == 0 else f"FAIL ({failures})")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
