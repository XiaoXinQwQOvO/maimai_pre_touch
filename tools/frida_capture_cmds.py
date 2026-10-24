"""Capture the game -> maitouch command stream (mice.dll!port_read) at boot.

Attaches frida before the game initialises its IO and polls the agent state
without resetting it, so every byte the game writes to virtual COM3 is kept.
Also keeps the first status packets the device pushed back.

Run:  python tools/frida_capture_cmds.py [--wait 120]
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
from frida_maitouch_probe import attach_early, kill_all, launch  # noqa: E402

OUT = os.path.join(HERE, "probe_out")
os.makedirs(OUT, exist_ok=True)


def decode_stream(reads):
    """Rebuild the command byte stream.

    mice reads a command as: 1 byte loop until '{', then 5 more bytes.
    """
    cmds = []
    cur = b""
    for n, hx in reads:
        try:
            b = bytes.fromhex(hx)
        except ValueError:
            continue
        if n == 1:
            if cur:
                cmds.append(cur)
            cur = b
        else:
            cur += b
    if cur:
        cmds.append(cur)
    return [c.decode("latin1") for c in cmds]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=float, default=150.0)
    args = ap.parse_args()
    rep = {}
    kill_all()
    print("[i] launching")
    launch()
    try:
        session, script, msgs, pid = attach_early(60)
        ex = script.exports_sync
        t0 = time.time()
        first_active = None
        while time.time() - t0 < args.wait:
            s = ex.peek()
            if s["pk14"] and first_active is None:
                first_active = time.time() - t0
                print(f"[i] first status packet after {first_active:.1f}s: {list(s['pk14'])}")
                break
            time.sleep(0.25)
        # keep collecting the command stream for a few more seconds
        time.sleep(3.0)
        s = ex.peek()
        rep["reads"] = s["reads"]
        rep["nreads"] = s["nreads"]
        rep["packets"] = s["pk14"]
        rep["writes"] = s["writes"]
        rep["addpoint"] = s["addpoint"]
        rep["builds"] = s["builds"]
        rep["contacts_add"] = s["contacts_add"]
        rep["first_active_s"] = first_active
        cmds = decode_stream(s["reads"])
        rep["commands"] = cmds
        print(f"[i] total port_read calls: {s['nreads']}  recorded: {len(s['reads'])}")
        print("[i] reconstructed commands:")
        for c in cmds:
            print("    ", repr(c), c.encode("latin1").hex(" "))
        print("[i] packets:", list(s["pk14"])[:6])
        print("[i] other writes:", s["writes"])
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
        path = os.path.join(OUT, "cmd_capture.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=1, ensure_ascii=False)
        print("[i] ->", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
