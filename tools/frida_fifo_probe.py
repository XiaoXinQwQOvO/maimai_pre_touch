"""Is the maitouch TX FIFO drained?  (i.e. does the game actually read it)

Uses the proven attach path of tools/frida_verify_channel.py and the watch
agent's new ring-pointer fields:

  every 14-byte port_write logs (read_ptr, write_ptr) of the device object
  every port_read logs (this, n, bytes, read_ptr, write_ptr)

Interpretation:
  read_ptr  advancing  -> the FIFO is being popped  -> the game consumes
  read_ptr  pinned     -> nobody consumes           -> wrong path
Also tells us whether the small 1-byte reads belong to the maitouch device
object or to another virtual device in the same process.

Run:  python tools/frida_fifo_probe.py [--ready-timeout 200]
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
import maitouch_state as MS  # noqa: E402
from frida_maitouch_probe import kill_all, launch  # noqa: E402
from frida_verify_channel import attach_watch  # noqa: E402

OUT = os.path.join(HERE, "probe_out")
os.makedirs(OUT, exist_ok=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ready-timeout", type=float, default=200.0)
    args = ap.parse_args()
    rep = {}
    kill_all()
    print("[i] launching")
    launch()
    try:
        session, script, pid = attach_watch(60)
        ex = script.exports_sync
        win = P.find_game_window(90)
        print(f"[i] pid={pid} hwnd={win['hwnd']:#x}" if win else f"[i] pid={pid}")
        t0 = time.time()
        while time.time() - t0 < args.ready_timeout:
            if ex.peek()["pk14"]:
                break
            time.sleep(0.5)
        s = ex.peek()
        print(f"[i] active={bool(s['pk14'])} device objects={s['devs']}")

        ex.reset()
        time.sleep(2.0)
        s1 = ex.take()
        rds = [x[0] for x in s1["fifo"]]
        wrs = [x[1] for x in s1["fifo"]]
        rep["idle"] = {"npush": len(s1["fifo"]), "devs": s1["devs"],
                       "distinct_rd": len(set(rds)), "distinct_wr": len(set(wrs)),
                       "rd_range": [min(rds), max(rds)] if rds else None,
                       "wr_range": [min(wrs), max(wrs)] if wrs else None,
                       "nreads": s1["nreads"], "reads": s1["reads"][:10]}
        print(f"[i] 2s idle: {len(s1['fifo'])} status pushes, distinct read_ptr="
              f"{len(set(rds))}, write_ptr={len(set(wrs))}")
        if rds:
            print(f"    read_ptr {min(rds)}..{max(rds)}  write_ptr {min(wrs)}..{max(wrs)}")
        print(f"    port_read calls={s1['nreads']} (1st 5: {s1['reads'][:5]})")
        print(f"    ring_write ok={s1.get('ok')} failed-ring-full={s1.get('bad')}")

        w = MS.MaitouchWriter(pid=pid)
        rep["probe"] = w.probe()
        w.freeze()
        pkt = MS.packet_from_zones(list(MS.ZONES))
        ex.reset()
        t0 = time.time()
        while time.time() - t0 < 2.0:
            w.write_packet(pkt)
            time.sleep(0.02)
        s2 = ex.take()
        rds2 = [x[0] for x in s2["fifo"]]
        rep["injected"] = {"packet": pkt.hex(), "npush": len(s2["fifo"]),
                           "distinct_rd": len(set(rds2)),
                           "rd_range": [min(rds2), max(rds2)] if rds2 else None,
                           "pkts": list(s2["pk14"]), "nreads": s2["nreads"],
                           "reads": s2["reads"][:10]}
        print(f"[i] injected: {len(s2['fifo'])} pushes, distinct read_ptr={len(set(rds2))}, "
              f"wire={list(s2['pk14'])}")
        if rds2:
            print(f"    read_ptr {min(rds2)}..{max(rds2)}")
        print(f"    port_read calls={s2['nreads']} (1st 5: {s2['reads'][:5]})")
        print(f"    ring_write ok={s2.get('ok')} failed-ring-full={s2.get('bad')}")
        w.restore()
        w.close()
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
        path = os.path.join(OUT, "fifo_probe.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=1, ensure_ascii=False)
        print("[i] ->", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
