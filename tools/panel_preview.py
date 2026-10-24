"""Draw what the phone will draw, on top of the captured game picture.

    python tools/panel_preview.py                    # capture now, detect, save overlay
    python tools/panel_preview.py --image frame.png  # use a saved frame
    python tools/panel_preview.py --panel 0.2516,0.7165,0.275,0.2812   # explicit geometry

Writes <out> (annotated) and <out>_raw.png (the plain frame) so a human can check
that the overlay ring sits exactly on the circle the game drew.
"""
from __future__ import annotations

import argparse
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pc"))

import panel_detect  # noqa: E402
import sensors  # noqa: E402


def draw_overlay(frame, panel: dict, line_width: int = 3):
    import cv2

    height, width = frame.shape[:2]
    cx = panel["cx"] * width
    cy = panel["cy"] * height
    rx = panel["rx"] * width
    ry = panel["ry"] * height

    def ellipse(scale, colour, thickness):
        cv2.ellipse(
            frame,
            (int(round(cx)), int(round(cy))),
            (max(1, int(round(rx * scale))), max(1, int(round(ry * scale)))),
            0,
            0,
            360,
            colour,
            thickness,
            cv2.LINE_AA,
        )

    ellipse(1.0, (60, 220, 60), line_width + 1)  # panel rim (r = 1.0)
    ellipse(sensors.R_INNER, (255, 170, 60), line_width)
    ellipse(sensors.R_CENTRE, (60, 170, 255), line_width)
    for sector in range(sensors.SECTORS):
        angle = math.radians(sector * sensors.SECTOR_DEG)
        cv2.line(
            frame,
            (int(round(cx + sensors.R_CENTRE * rx * math.cos(angle))), int(round(cy + sensors.R_CENTRE * ry * math.sin(angle)))),
            (int(round(cx + rx * math.cos(angle))), int(round(cy + ry * math.sin(angle)))),
            (200, 200, 200),
            2,
            cv2.LINE_AA,
        )
    cv2.drawMarker(frame, (int(round(cx)), int(round(cy))), (0, 0, 255), cv2.MARKER_CROSS, 50, 3)
    font = cv2.FONT_HERSHEY_SIMPLEX
    cv2.putText(frame, "C", (int(cx) - 8, int(cy) + 6), font, 0.6, (255, 255, 255), 2, cv2.LINE_AA)
    for name in sensors.SENSOR_ORDER:
        if name == sensors.CENTRE:
            continue
        px, py = sensors.sensor_point(name, panel)
        cv2.putText(
            frame,
            sensors.LABELS[name],
            (int(px * width) - 12, int(py * height) + 6),
            font,
            0.55,
            (0, 255, 255),
            2,
            cv2.LINE_AA,
        )
    info = (
        f"cx={panel['cx']:.4f} cy={panel['cy']:.4f} rx={panel['rx']:.4f} ry={panel['ry']:.4f} "
        f"stretch={panel['rx']/panel['ry']:.3f}"
    )
    cv2.putText(frame, info, (12, height - 16), font, 0.62, (0, 255, 255), 2, cv2.LINE_AA)
    return frame


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="")
    parser.add_argument("--panel", default="", help="cx,cy,rx,ry as frame fractions")
    parser.add_argument("--out", default=os.path.join(HERE, "probe_out", "panel_overlay.png"))
    args = parser.parse_args()

    import imgio

    if args.image:
        frame = imgio.imread(args.image)
    else:
        frame = panel_detect.frame_from_window()
    if frame is None:
        print("[!] no frame")
        return 1
    if frame.ndim == 3 and frame.shape[2] == 4:
        frame = frame[:, :, :3]

    if args.panel:
        cx, cy, rx, ry = (float(v) for v in args.panel.split(","))
        panel = {"cx": cx, "cy": cy, "rx": rx, "ry": ry, "r": (rx + ry) / 2}
        source = "explicit"
    else:
        circle = panel_detect.detect_disc(frame)
        if circle is None:
            print("[!] no panel circle found in this frame")
            return 2
        panel = {"cx": circle.cx, "cy": circle.cy, "rx": circle.rx, "ry": circle.ry, "r": circle.r}
        source = f"detected (coverage {circle.coverage:.2f})"

    height, width = frame.shape[:2]
    print(f"[i] {source}: cx={panel['cx']:.4f} cy={panel['cy']:.4f} rx={panel['rx']:.4f} ry={panel['ry']:.4f}")
    print(
        f"    pixels: centre ({panel['cx']*width:.1f}, {panel['cy']*height:.1f}) "
        f"rx={panel['rx']*width:.1f} ry={panel['ry']*height:.1f}"
    )
    imgio.imwrite(args.out.replace(".png", "_raw.png"), frame)
    annotated = draw_overlay(frame.copy(), panel)
    imgio.imwrite(args.out, annotated)
    print(f"[i] wrote {args.out} and {args.out.replace('.png', '_raw.png')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
