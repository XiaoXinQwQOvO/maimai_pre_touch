"""End-to-end verification of the external maitouch write channel.

Launches the game, waits until mice.dll pushes 14-byte status packets to the
virtual COM3, then:
  1. freezes mice.dll!build_status with a one byte `ret` patch (WriteProcessMemory)
  2. writes arbitrary 14-byte packets from *this* process
  3. reads back, through a frida hook on mice.dll!port_write, the packets the
     device actually handed to the game
  4. restores everything and checks the idle packets come back

Run:  python tools/frida_verify_channel.py [--ready-timeout 150]
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


def attach_watch(timeout=60.0):
    """Attach with the port-I/O-only agent (never hooks build_status)."""
    import frida
    dev = frida.get_local_device()
    t0 = time.time()
    pid = None
    while time.time() - t0 < timeout:
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
        src = f.read()
    script = session.create_script(src)
    script.on("message", lambda m, d: print("  [agent]", m["payload"]) if m.get("type") == "send" else print("  [JS ERR]", m))
    script.load()
    print(f"[i] attached to pid {pid} (port-io watch only)")
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
        session, script, pid = attach_watch(60)
        ex = script.exports_sync
        win = P.find_game_window(90)
        if win is None:
            raise RuntimeError("no window")
        print(f"[i] game hwnd={win['hwnd']:#x} pid={pid}")
        deadline = time.time() + args.ready_timeout
        while time.time() < deadline:
            s = ex.take()
            if s["pk14"]:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("no status packets")
        print("[i] active, idle packets:", list(s["pk14"])[:3])
        rep["idle_packets"] = list(s["pk14"])
        time.sleep(1.0)
        ex.reset()

        w = MS.MaitouchWriter(pid=pid)
        rep["probe_before"] = w.probe()
        print("[i] probe:", json.dumps(rep["probe_before"]))

        print("[i] freeze:", w.freeze())
        rep["probe_frozen"] = w.probe()
        print("[i] after freeze 6 bytes:", rep["probe_frozen"]["build_status"],
              "packet:", rep["probe_frozen"]["packet"])

        # ---- single zone ----
        ex.reset()
        pkt = MS.packet_from_zones(["O0"])
        w.write_packet(pkt)
        time.sleep(1.0)
        s = ex.take()
        rep["single"] = {"written": pkt.hex(), "seen_by_port_write": s["pk14"],
                         "builds": s.get("builds", 0)}
        print(f"[i] single zone O0 written {pkt.hex()} -> wire {list(s['pk14'])} "
              f"(build_status calls: {s.get("builds", 0)})")

        # ---- many zones at once ----
        ex.reset()
        want = ["C", "O0", "I90", "O180", "I270", "O45"]
        pkt2 = MS.packet_from_zones(want)
        w.write_packet(pkt2)
        time.sleep(1.0)
        s2 = ex.take()
        rep["multi"] = {"zones": want, "written": pkt2.hex(),
                        "decoded": MS.describe(pkt2),
                        "seen_by_port_write": s2["pk14"]}
        print(f"[i] multi {want} -> {pkt2.hex()} | wire {list(s2['pk14'])}")

        # ---- all 17 zones ----
        ex.reset()
        pkt3 = MS.packet_from_zones(list(MS.ZONES))
        w.write_packet(pkt3)
        time.sleep(1.0)
        s3 = ex.take()
        rep["all"] = {"written": pkt3.hex(), "seen_by_port_write": s3["pk14"]}
        print(f"[i] all zones -> {pkt3.hex()} | wire {list(s3['pk14'])}")

        # ---- clear ----
        ex.reset()
        w.clear()
        time.sleep(0.8)
        s4 = ex.take()
        rep["cleared"] = {"seen_by_port_write": s4["pk14"]}
        print(f"[i] cleared -> wire {list(s4['pk14'])}")

        # ---- restore ----
        print("[i] restore:", w.restore())
        ex.reset()
        time.sleep(1.2)
        s5 = ex.take()
        rep["after_restore"] = {"seen_by_port_write": s5["pk14"], "builds": s5.get("builds", 0)}
        print(f"[i] after restore -> wire {list(s5['pk14'])} builds={s5.get("builds", 0)}")
        rep["probe_after"] = w.probe()
        w.close()

        # ---- did the game react?  pixel change of a held multi-zone touch ----
        try:
            bbox = (P.client_rects(win["hwnd"])[0], P.client_rects(win["hwnd"])[1],
                    P.client_rects(win["hwnd"])[0] + P.client_rects(win["hwnd"])[2],
                    P.client_rects(win["hwnd"])[1] + P.client_rects(win["hwnd"])[3])
            base = P.grab(bbox, os.path.join(OUT, "chan_before.png"))
            w2 = MS.MaitouchWriter(pid=pid)
            w2.freeze()
            w2.write_packet(MS.packet_from_zones(["C"]))
            time.sleep(2.5)
            shot = P.grab(bbox, os.path.join(OUT, "chan_hold_center.png"))
            w2.clear()
            time.sleep(1.0)
            after = P.grab(bbox, os.path.join(OUT, "chan_after.png"))
            rep["pixel"] = {"before_vs_hold": P.diff_score(base, shot),
                            "before_vs_after": P.diff_score(base, after)}
            w2.restore()
            w2.close()
            print("[i] pixel delta:", rep["pixel"])
        except Exception as e:  # noqa: BLE001
            rep["pixel_error"] = str(e)

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
        path = os.path.join(OUT, "verify_channel.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=1, ensure_ascii=False)
        print("[i] ->", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
