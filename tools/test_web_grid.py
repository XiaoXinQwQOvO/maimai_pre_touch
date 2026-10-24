"""Dump the sensor grid computed by pc/sensors.py so the web client can be checked.

    python tools/test_web_grid.py dump     # writes tools/probe_out/web_grid.json
    node tools/test_web_grid.js            # compares web/app.js against it

Two panels are dumped: the whole frame (the legacy assumption) and a detected,
off-centre, slightly stretched circle - the phone has to agree with the PC on both.
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pc"))

import sensors  # noqa: E402

OUT = os.path.join(HERE, "probe_out", "web_grid.json")

# a real detection from the game's waiting screen (see pc/panel_detect.py)
DETECTED_PANEL = {"cx": 0.25157, "cy": 0.71648, "rx": 0.275, "ry": 0.28125}


def _grid(panel, steps: int) -> list:
    points = []
    for i in range(steps):
        for j in range(steps):
            x = i / (steps - 1)
            y = j / (steps - 1)
            points.append([round(x, 6), round(y, 6), sensors.sensor_at(x, y, panel)])
    return points


def _sensor_points(panel) -> list:
    return [[name, [round(v, 6) for v in sensors.sensor_point(name, panel)]] for name in sensors.SENSOR_ORDER]


def dump(steps: int = 101) -> str:
    payload = {
        "full": {"panel": None, "grid": _grid(None, steps), "sensor_points": _sensor_points(None)},
        "detected": {
            "panel": DETECTED_PANEL,
            "grid": _grid(DETECTED_PANEL, steps),
            "sensor_points": _sensor_points(DETECTED_PANEL),
        },
        "labels": list(sensors.SENSOR_ORDER),
        "centre": sensors.CENTRE,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    print(
        f"wrote {OUT} ({len(payload['full']['grid'])} points x2 panels, "
        f"{len(payload['labels'])} labels)"
    )
    return OUT


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] != "dump":
        raise SystemExit(__doc__)
    dump()
