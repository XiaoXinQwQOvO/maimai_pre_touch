"""Analyse tools/probe_out/mouse_grid.json -> sensor bit map of maitouch."""
import collections
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
path = os.path.join(HERE, "probe_out", "mouse_grid.json")
d = json.load(open(path, encoding="utf-8"))
rows = [r for r in d["rows"] if 0 <= r["x"] < 2000 and 0 <= r["y"] < 2000]
print("W,H =", d.get("W_H"), "rows:", len(rows), "addpoint calls:", d.get("addpoint"))
print("wndproc msgs:", d.get("wndmsg"))


def ring(r):
    if r < 0.224:
        return "centre"
    if r < 0.552:
        return "inner"
    return "outer"


tab = collections.defaultdict(collections.Counter)
for r in rows:
    if r["r"] >= 1.0:
        continue
    tab[(ring(r["r"]), int((r["deg"] + 22.5) // 45) % 8)][(r["idx"], r["bit"])] += 1

print("\n(ring, sector) -> (packet byte index, bit) votes")
print("sector k covers azimuth [k*45-22.5, k*45+22.5), 0 deg = screen right, CW (screen y down)")
for ring_name in ("centre", "inner", "outer"):
    print(f"\n-- {ring_name}")
    for sec in range(8):
        c = tab.get((ring_name, sec))
        if not c:
            print(f"   sector {sec:>2}: (no samples)")
            continue
        tot = sum(c.values())
        best = ", ".join(f"byte{i}=0x{b:02x}({n})" for (i, b), n in c.most_common())
        print(f"   sector {sec:>2} (deg {sec*45:>3}): {best}  [{tot} samples]")

# ring boundary detection: scan along +x axis rows (v ~ 0)
print("\n-- radial profile along several azimuths (x axis and diagonal)")
for label, pred, key in (
    ("azimuth   0 deg", lambda r: abs(r["v"]) < 0.05 and r["u"] > 0, lambda r: r["u"]),
    ("azimuth  90 deg", lambda r: abs(r["u"]) < 0.05 and r["v"] > 0, lambda r: r["v"]),
    ("azimuth  45 deg", lambda r: abs(r["u"] - r["v"]) < 0.05 and r["u"] > 0, lambda r: r["u"]),
):
    sel = sorted([r for r in rows if pred(r)], key=key)
    if not sel:
        continue
    print(f"   {label}:")
    prev = None
    for r in sel:
        cur = (r["idx"], r["bit"])
        if cur != prev:
            print(f"      at |xy|={r['u'] if 'x' in label else r['u']:.3f} (r={r['r']:.3f}) "
                  f"-> byte{r['idx']}=0x{r['pktbyte']:02x}")
            prev = cur
