"""Does the maitouch TX ring get drained at all - by real mouse input?

Holds the left mouse button on the touch panel centre for 3 s while recording

  * maitouch device TX ring: read ptr @dev+0x1024, write ptr @dev+0x1026
  * ring_write success/failure counters
  * pixel delta of the game window (numeric, no vision needed)

If even real mouse input leaves the read pointer pinned, maimai is simply not
reading the panel in this state (attract mode / touch disabled) and no injection
method could show an in-game reaction.  If the read pointer advances with the
mouse but not with our writes, our write point is wrong.

Run:  python tools/frida_panel_probe.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "pc"))

import probe_touch as P  # noqa: E402
from frida_maitouch_probe import kill_all, launch, mouse_button  # noqa: E402
from frida_verify_channel import attach_watch  # noqa: E402

OUT = os.path.join(HERE, "probe_out")
os.makedirs(OUT, exist_ok=True)


def stats(s):
    rds = [x[0] for x in s["fifo"]]
    wrs = [x[1] for x in s["fifo"]]
    return {"pushes": len(s["fifo"]),
            "distinct_rd": len(set(rds)), "rd_range": [min(rds), max(rds)] if rds else None,
            "distinct_wr": len(set(wrs)), "wr_range": [min(wrs), max(wrs)] if wrs else None,
            "ok": s.get("ok", 0), "bad": s.get("bad", 0),
            "packets": list(s["pk14"]), "nreads": s["nreads"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ready-timeout", type=float, default=200.0)
    ap.add_argument("--hold", type=float, default=3.0)
    args = ap.parse_args()
    rep = {}
    kill_all()
    print("[i] launching")
    launch()
    box = None
    try:
        session, script, pid = attach_watch(60)
        ex = script.exports_sync
        win = P.find_game_window(90)
        if win is None:
            raise RuntimeError("no window")
        cx, cy, cw, ch = P.client_rects(win["hwnd"])
        box = (cx, cy, cx + cw, cy + ch)
        print(f"[i] pid={pid} client {cx},{cy} {cw}x{ch}")
        t0 = time.time()
        while time.time() - t0 < args.ready_timeout:
            if ex.peek()["pk14"]:
                break
            time.sleep(0.5)
        print("[i] panel active")

        before = P.grab(box, os.path.join(OUT, "panel_before.png"))
        ex.reset()
        time.sleep(2.0)
        rep["idle"] = stats(ex.take())
        print("[i] idle:", rep["idle"])

        # real mouse on the panel centre, held
        P.user32.SetCursorPos(cx + cw // 2, cy + ch // 2)
        time.sleep(0.2)
        mouse_button(True)
        ex.reset()
        time.sleep(args.hold)
        mid = P.grab(box, os.path.join(OUT, "panel_mouse_hold.png"))
        s = ex.take()
        rep["mouse_hold"] = stats(s)
        print("[i] mouse hold:", rep["mouse_hold"])
        mouse_button(False)
        time.sleep(0.5)

        after = P.grab(box, os.path.join(OUT, "panel_after.png"))
        rep["pixel"] = {"before_vs_hold": P.diff_score(before, mid),
                        "before_vs_after": P.diff_score(before, after)}
        print("[i] pixel:", rep["pixel"])

        # where is the cursor now / did we read the same ring?
        rep["devs"] = s["devs"]
        try:
            script.unload()
            session.detach()
        except Exception:
            pass
    except BaseException as e:  # noqa: BLE001
        rep["error"] = f"{type(e).__name__}: {e}"
        print("[!]", rep["error"])
    finally:
        kill_all()
        path = os.path.join(OUT, "panel_probe.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=1, ensure_ascii=False)
        print("[i] ->", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
