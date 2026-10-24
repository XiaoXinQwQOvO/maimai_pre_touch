"""Dump the sensor grid computed by pc/sensors.py so the web client can be checked.

    python tools/test_web_grid.py dump     # writes tools/probe_out/web_grid.json
    node tools/test_web_grid.js            # compares web/app.js against it
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pc"))

import sensors  # noqa: E402

OUT = os.path.join(HERE, "probe_out", "web_grid.json")


def dump(steps: int = 101) -> str:
    grid = []
    for i in range(steps):
        for j in range(steps):
            x = i / (steps - 1)
            y = j / (steps - 1)
            grid.append([round(x, 6), round(y, 6), sensors.sensor_at(x, y)])
    payload = {"grid": grid, "labels": list(sensors.SENSOR_ORDER), "centre": sensors.CENTRE}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    print(f"wrote {OUT} ({len(grid)} points, {len(payload['labels'])} labels)")
    return OUT


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] != "dump":
        raise SystemExit(__doc__)
    dump()
