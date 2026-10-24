"""Mouse grid scan: derive the exact sensor geometry of the maitouch status packet.

Holds the left mouse button down and sweeps the cursor over a grid of client
positions while frida records, for every call of mice.dll!add_point, the
(width, height, client_x, client_y) it was given and which packet byte/bit got
set.  Result: an empirical (client position -> sensor bit) map plus the real
normalisation constants W/H that mice uses.

Run:  python tools/frida_mouse_grid.py [--step 48] [--ready-timeout 150]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "pc"))

import probe_touch as P  # noqa: E402
from frida_maitouch_probe import attach_early, kill_all, launch, mouse_button  # noqa: E402

OUT = os.path.join(HERE, "probe_out")
os.makedirs(OUT, exist_ok=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", type=int, default=48)
    ap.add_argument("--margin", type=int, default=6)
    ap.add_argument("--ready-timeout", type=float, default=150.0)
    args = ap.parse_args()

    report = {}
    kill_all()
    print("[i] launching")
    launch()
    try:
        session, script, msgs, pid = attach_early(60)
        ex = script.exports_sync
        win = P.find_game_window(90)
        if win is None:
            raise RuntimeError("no window")
        cx0, cy0, cw, ch = P.client_rects(win["hwnd"])
        report["window"] = {"hwnd": hex(win["hwnd"]), "rect": list(win["rect"]),
                            "client": [cx0, cy0, cw, ch]}
        print(f"[i] window {win['hwnd']:#x} client {cx0},{cy0} {cw}x{ch}")
        P.user32.SetForegroundWindow(win["hwnd"])

        deadline = time.time() + args.ready_timeout
        while time.time() < deadline:
            s = ex.take()
            if s["pk14"]:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("never active")
        print("[i] active")
        time.sleep(0.8)
        ex.reset()

        # hold LMB so mice samples GetCursorPos continuously
        mouse_button(True)
        time.sleep(0.15)
        coords = []
        xs = list(range(args.margin, cw - args.margin, args.step))
        ys = list(range(args.margin, ch - args.margin, args.step))
        for y in ys:
            for x in xs:
                P.user32.SetCursorPos(cx0 + x, cy0 + y)
                coords.append([x, y])
                time.sleep(0.045)
        mouse_button(False)
        time.sleep(0.2)
        s = ex.take()
        report["inputmap"] = s["inputmap"]
        report["wh"] = s["wh"]
        report["addpoint"] = s["addpoint"]
        report["packets"] = s["pk14"]
        report["wndmsg"] = s.get("wndmsg", {})
        report["grid"] = {"xs": xs, "ys": ys}
        print(f"[i] addpoint calls={s['addpoint']} distinct WxH={s['wh']} keys={len(s['inputmap'])}")
        print(f"[i] wndproc messages seen by mice subclass proc: {s.get('wndmsg', {})}")
        print(f"[i] packets seen at grid time: {list(s['pk14'])[:6]}")

        # solution table: (idx, byte) as a function of normalised u,v
        rows = []
        wh = sorted(s["wh"].items(), key=lambda kv: -kv[1])
        if wh:
            W, H = [int(t) for t in wh[0][0].split("x")]
            report["W_H"] = [W, H]
            print(f"[i] dominant W,H = {W},{H}")
            for k, v in s["inputmap"].items():
                x, y, w, h = [int(t) for t in k.split(",")]
                if (w, h) != (W, H):
                    continue
                idx, byte = [int(t) for t in v.split(":")]
                u = 2.0 * x / W - 1.0
                vv = 2.0 * y / H - 1.0
                r = math.hypot(u, vv)
                ang = math.degrees(math.atan2(vv, u)) % 360.0
                rows.append({"x": x, "y": y, "u": round(u, 4), "v": round(vv, 4),
                             "r": round(r, 4), "deg": round(ang, 2),
                             "idx": idx, "pktbyte": byte, "bit": byte & 0x3F})
            rows.sort(key=lambda d: (d["deg"], d["r"]))
            report["rows"] = rows
            path = os.path.join(OUT, "mouse_grid.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=1, ensure_ascii=False)
            print("[i] intermediate dump ->", path)
            print(f"{'x':>4} {'y':>4} {'u':>7} {'v':>7} {'r':>6} {'deg':>7} "
                  f"{'idx':>3} {'byte':>5} {'bit':>5}")
            for d in rows:
                print(f"{d['x']:>4} {d['y']:>4} {d['u']:>7.3f} {d['v']:>7.3f} {d['r']:>6.3f} "
                      f"{d['deg']:>7.1f} {d['idx']:>3} {hex(d['pktbyte']):>5} {hex(d['bit']):>5}")
        try:
            script.unload()
            session.detach()
        except Exception:
            pass
    except BaseException as e:  # noqa: BLE001
        report["error"] = f"{type(e).__name__}: {e}"
        print("[!]", report["error"])
    finally:
        kill_all()
        path = os.path.join(OUT, "mouse_grid.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=1, ensure_ascii=False)
        print("[i] ->", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
