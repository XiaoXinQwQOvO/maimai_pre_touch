"""Dynamic probe of micetools' maitouch virtual COM device.

Attaches frida to the game as soon as it starts, installs hooks inside
mice.dll (see tools/maitouch_agent.js) and measures, for each injected input,
the exact bits that end up in the 14-byte status packet that mice writes to
the virtual COM3 port.

Phases: idle / real mouse button grid / synthetic touch grid (16 angles x 2
radii + centre) / 10 simultaneous synthetic contacts / release.

Run:  python tools/frida_maitouch_probe.py [--ready-timeout 150] [--keep]
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import math
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "pc"))

import probe_touch as P  # noqa: E402
from touch_inject import SyntheticTouch  # noqa: E402

GAME_DIR = P.GAME_DIR
OUT = os.path.join(HERE, "probe_out")
os.makedirs(OUT, exist_ok=True)
JS_PATH = os.path.join(HERE, "maitouch_agent.js")

KILL_LIST = ["maimai_dump_.exe", "maimai.exe", "mice.exe", "MovieProc.exe",
             "ALLNetProc.exe", "CameraUploader.exe", "GmSync.exe"]

user32 = ctypes.WinDLL("user32", use_last_error=True)
INPUT_MOUSE = 0
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class _I(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _I)]


def mouse_button(down: bool):
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.u.mi.dwFlags = MOUSEEVENTF_LEFTDOWN if down else MOUSEEVENTF_LEFTUP
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def kill_all():
    for name in KILL_LIST:
        subprocess.run(["taskkill", "/F", "/IM", name], capture_output=True)
    time.sleep(1.0)


def launch():
    return subprocess.Popen([os.path.join(GAME_DIR, "mice.exe")], cwd=GAME_DIR,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=0x00000008)


def attach_early(timeout=60.0):
    import frida
    dev = frida.get_local_device()
    deadline = time.time() + timeout
    pid = None
    while time.time() < deadline:
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
    with open(JS_PATH, "r", encoding="utf-8") as f:
        source = f.read()
    script = session.create_script(source)
    msgs = []

    def on_msg(m, d):
        if m.get("type") == "send":
            msgs.append(m["payload"])
            print("  [agent]", m["payload"])
        else:
            print("  [JS ERR]", m)

    script.on("message", on_msg)
    script.load()
    print(f"[i] attached to pid {pid}")
    return session, script, msgs, pid


def decode_zones(zones: dict):
    """{ret_hex: count} -> readable list of (byte_index, bit)."""
    out = []
    for k, v in zones.items():
        r = int(k)
        out.append({"raw": r, "byte": (r >> 8) & 0xFF, "bit": r & 0xFF, "hits": v})
    out.sort(key=lambda d: (d["byte"], d["bit"]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ready-timeout", type=float, default=150.0)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--skip-mouse", action="store_true")
    args = ap.parse_args()

    report = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "phases": {}}
    kill_all()
    logp = os.path.join(GAME_DIR, "log.txt")
    try:
        if os.path.exists(logp):
            open(logp, "w").close()
    except OSError as e:
        print("[!] log truncate:", e)

    print("[i] launching mice.exe")
    launch()
    try:
        session, script, msgs, pid = attach_early(60)
        ex = script.exports_sync

        win = P.find_game_window(90)
        if win is None:
            raise RuntimeError("no game window")
        cx0, cy0, cw, ch = P.client_rects(win["hwnd"])
        wr = win["rect"]
        winW, winH = wr[2], wr[3]
        print(f"[i] window class={win['class']} rect={wr} client={cx0},{cy0} {cw}x{ch}")
        report["window"] = {"class": win["class"], "rect": list(wr),
                            "client": [cx0, cy0, cw, ch], "exe": win["exe"]}
        user32.SetForegroundWindow(win["hwnd"])
        time.sleep(0.5)

        # ---------- wait for active mode (14-byte status packets) ----------
        deadline = time.time() + args.ready_timeout
        ready = False
        last_log = 0
        while time.time() < deadline:
            s = ex.take()
            if s["pk14"]:
                ready = True
                report["boot"] = {"packets": s["pk14"], "reads": s["reads"],
                                  "addpoint": s["addpoint"], "contacts_add": s["contacts_add"]}
                break
            if time.time() - last_log > 15:
                print(f"[i] waiting for active mode ... addpoint={s['addpoint']} "
                      f"reads={s['nreads']} builds={s['builds']}")
                last_log = time.time()
            time.sleep(0.5)
        if not ready:
            raise RuntimeError("never saw a 14-byte status packet")
        print("[i] ACTIVE: status packets flowing")
        print("[i] boot packets:", list(report["boot"]["packets"])[:6])
        print("[i] boot reads  :", report["boot"]["reads"][:40])

        # ---------------- geometry helpers --------------------------------
        def to_client(u, v):
            return (int(round((u + 1) * 0.5 * winW)), int(round((v + 1) * 0.5 * winH)))

        def to_screen(p):
            return (cx0 + p[0], cy0 + p[1])

        pts = [("center", 0.0, 0.0)]
        for ring, r in (("in", 0.40), ("out", 0.80)):
            for k in range(16):
                ang = k * math.pi / 8.0
                pts.append((f"{ring}_{k * 22.5:.1f}deg", r * math.cos(ang), r * math.sin(ang)))
        report["points"] = [{"name": n, "u": round(u, 4), "v": round(v, 4),
                             "client": to_client(u, v), "screen": to_screen(to_client(u, v))}
                            for n, u, v in pts]

        time.sleep(1.0)
        ex.reset()
        time.sleep(1.0)
        report["phases"]["idle"] = ex.take()
        print(f"[i] idle zones={report['phases']['idle']['zones']}")

        touch = SyntheticTouch(max_contacts=10)

        # ---------------- phase: real mouse button ------------------------
        if not args.skip_mouse:
            res = []
            for name, u, v in pts[:1] + pts[1::2]:      # centre + 8 angles
                cp = to_client(u, v)
                sp = to_screen(cp)
                ex.reset()
                user32.SetCursorPos(*sp)
                time.sleep(0.05)
                mouse_button(True)
                time.sleep(0.30)
                s = ex.take()
                res.append({"name": name, "client": cp, "zones": decode_zones(s["zones"]),
                            "packets": list(s["pk14"]), "addpoint": s["addpoint"]})
                mouse_button(False)
                time.sleep(0.06)
            report["phases"]["mouse"] = res
            user32.SetCursorPos(cx0 + 5, cy0 + 5)
            time.sleep(0.3)
            print("[i] mouse phase done")

        # ---------------- phase: synthetic touch grid ---------------------
        res = []
        for name, u, v in pts:
            cp = to_client(u, v)
            sp = to_screen(cp)
            ex.reset()
            try:
                touch.down(1, sp[0], sp[1])
            except OSError as e:
                res.append({"name": name, "error": str(e)})
                continue
            time.sleep(0.30)
            s = ex.take()
            ls = ex.list_state()
            res.append({"name": name, "u": round(u, 4), "v": round(v, 4),
                        "client": cp, "screen": sp,
                        "zones": decode_zones(s["zones"]), "packets": list(s["pk14"]),
                        "addpoint": s["addpoint"], "contacts_add": s["contacts_add"],
                        "list": ls})
            try:
                touch.up(1)
            except OSError:
                pass
            time.sleep(0.10)
        report["phases"]["synth"] = res
        print("[i] synth phase done")

        # ---------------- phase: 10 simultaneous contacts -----------------
        ex.reset()
        multi = []
        for i in range(10):
            ang = i * 2 * math.pi / 10
            u, v = 0.60 * math.cos(ang), 0.60 * math.sin(ang)
            cp = to_client(u, v)
            sp = to_screen(cp)
            try:
                touch.down(100 + i, sp[0], sp[1])
            except OSError as e:
                multi.append({"id": 100 + i, "error": str(e)})
                continue
            multi.append({"id": 100 + i, "u": round(u, 3), "v": round(v, 3),
                          "screen": sp, "client": cp})
            time.sleep(0.06)
        time.sleep(0.5)
        s = ex.take()
        report["phases"]["multi10"] = {"contacts": multi, "zones": decode_zones(s["zones"]),
                                       "packets": list(s["pk14"]), "writes": s["writes"],
                                       "addpoint": s["addpoint"], "list": ex.list_state()}
        print("[i] multi10 zones:", report["phases"]["multi10"]["zones"])
        print("[i] multi10 packets:", list(s["pk14"])[:8])
        print("[i] multi10 list:", report["phases"]["multi10"]["list"])
        for i in range(10):
            try:
                touch.up(100 + i)
            except OSError:
                pass
            time.sleep(0.04)
        time.sleep(0.4)
        ex.reset()
        time.sleep(0.1)
        report["phases"]["after_release"] = ex.take()
        touch.close()
        ex.reset()
        time.sleep(1.0)
        report["phases"]["final_idle"] = ex.take()
        print("[i] after release zones:", report["phases"]["after_release"]["zones"])

        try:
            script.unload()
            session.detach()
        except Exception:
            pass
    except BaseException as e:  # noqa: BLE001
        report["error"] = f"{type(e).__name__}: {e}"
        print("[!]", report["error"])
    finally:
        if not args.keep:
            kill_all()
        path = os.path.join(OUT, "maitouch_probe.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=1, ensure_ascii=False)
        print("[i] report ->", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
