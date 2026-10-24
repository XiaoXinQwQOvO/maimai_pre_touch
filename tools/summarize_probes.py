"""Summarise every probe_out/*.json into a compact evidence digest."""
import glob
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "probe_out")

for path in sorted(glob.glob(os.path.join(OUT, "*.json"))):
    d = json.load(open(path, encoding="utf-8"))
    print("=" * 78)
    print(os.path.basename(path), "keys:", list(d))
    if "window" in d:
        print("  window:", d["window"])
    if "phases" in d:
        for name, ph in d["phases"].items():
            z = ph.get("zones") or {}
            top = sorted(z.items(), key=lambda kv: -kv[1])[:6] if isinstance(z, dict) else []
            print(f"  phase {name}: dur={ph.get('dur')} addpoint={ph.get('addpoint')} "
                  f"builds={ph.get('builds')} nreads={ph.get('nreads')} "
                  f"contacts_add={ph.get('contacts_add')} contacts_del={ph.get('contacts_del')}")
            print(f"      packets({len(ph.get('pkts') or [])})={list(ph.get('pkts') or [])[:6]}")
            print(f"      zones top={top}")
    for k in ("baseline", "injected", "after_restore", "hits", "match_count"):
        if k in d:
            v = d[k]
            if isinstance(v, dict):
                v = {kk: (vv if not isinstance(vv, list) else f"[{len(vv)} items]") for kk, vv in v.items()}
            print(f"  {k}: {v}")
    for k in ("nreads", "reads", "packets", "writes", "commands", "first_active_s",
              "addpoint", "builds", "contacts_add", "wh", "W_H", "pixel", "error"):
        if k in d:
            v = d[k]
            if isinstance(v, list):
                print(f"  {k}: [{len(v)} items] {v[:5]}")
            else:
                print(f"  {k}: {v}")
