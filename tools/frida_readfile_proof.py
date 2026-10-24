"""Proof #2: our bytes are what the game's own ReadFile returns.

mice.dll serves the game's `ReadFile(hCom3, ...)` through the maitouch device
object; the device object's read handler is mice.dll+0x1175E (port_read).
Everything else (build_status -> packet -> port_write) only fills the 2 KiB
per-device FIFO at dev+0x800/0x802.

So: freeze build_status, write a distinctive "all sensors" packet, then log the
buffers that the game's ReadFile pops out of mice.dll+0x1175E.  If the packet
shows up there, the game received it.

Run:  python tools/frida_readfile_proof.py
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

OUT = os.path.join(HERE, "probe_out")
os.makedirs(OUT, exist_ok=True)
WATCH_JS = os.path.join(HERE, "maitouch_watch_agent.js")


def attach(pid_path_timeout=60.0):
    import frida
    dev = frida.get_local_device()
    t0 = time.time()
    pid = None
    while time.time() - t0 < pid_path_timeout:
        for p in dev.enumerate_processes():
            if p.name.lower() in ("maimai_dump_.exe", "maimai.exe"):
                pid = p.pid
                break
        if pid:
            break
        time.sleep(0.05)
    if not pid:
        raise RuntimeError("game process not found")
    session = frida.attach(pid)
    with open(WATCH_JS, "r", encoding="utf-8") as f:
        script = session.create_script(f.read())
    script.on("message", lambda m, d: print("  [agent]", m["payload"]))
    script.load()
    return session, script, pid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ready-timeout", type=float, default=150.0)
    args = ap.parse_args()
    rep = {}
    kill_all()
    print("[i] launching")
    launch()
    try:
        session, script, pid = attach()
        ex = script.exports_sync
        win = P.find_game_window(90)
        if win:
            cx, cy, cw, ch = P.client_rects(win["hwnd"])
            rep["client"] = [cx, cy, cw, ch]
        print(f"[i] pid={pid}")
        deadline = time.time() + args.ready_timeout
        while time.time() < deadline:
            if ex.peek()["pk14"]:
                break
            time.sleep(0.5)
        print("[i] active")

        ex.reset()
        time.sleep(2.0)
        base = ex.take()
        rep["baseline"] = {"nreads": base["nreads"], "reads": base["reads"][:20],
                          "pk14": list(base["pk14"])[:3]}
        print(f"[i] baseline: game port reads={base['nreads']} "
              f"samples={[r[1] for r in base['reads'][:5]]}")

        w = MS.MaitouchWriter(pid=pid)
        rep["probe_before"] = w.probe()
        w.freeze()
        pkt = MS.packet_from_zones(list(MS.ZONES))
        w.write_packet(pkt)
        target = pkt.hex()
        print("[i] frozen + wrote", pkt.hex(" "))

        # write it repeatedly so the FIFO always holds it while the game polls
        ex.reset()
        t0 = time.time()
        while time.time() - t0 < 4.0:
            w.write_packet(pkt)
            time.sleep(0.02)
        s = ex.take()
        rep["injected"] = {"packet": target, "wire": s["pk14"], "nreads": s["nreads"],
                           "reads": s["reads"]}
        hits = [r for r in s["reads"] if target in r[1]]
        rep["hits"] = len(hits)
        print(f"[i] game-side port_read calls: {s['nreads']}, wire packets: {list(s['pk14'])}")
        print(f"[i] reads containing our packet: {len(hits)}")
        for r in s["reads"][:10]:
            print(f"    n={r[0]} {r[1]}")

        w.restore()
        ex.reset()
        time.sleep(1.5)
        s2 = ex.take()
        rep["after_restore"] = {"nreads": s2["nreads"],
                                "still_hits": len([r for r in s2["reads"] if target in r[1]]),
                                "reads": s2["reads"][:10]}
        print("[i] after restore:", rep["after_restore"]["nreads"], "reads,",
              rep["after_restore"]["still_hits"], "still matching")
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
        path = os.path.join(OUT, "readfile_proof.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=1, ensure_ascii=False)
        print("[i] ->", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
