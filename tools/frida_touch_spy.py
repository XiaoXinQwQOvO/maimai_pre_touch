"""Spy on the maitouch virtual COM port traffic with frida.

Launches the game, attaches frida to maimai*.exe, hooks CreateFileW / ReadFile /
WriteFile, and reports only *changed* read payloads (plus every write) across
four phases: idle / mouse hover / synthetic multi-touch / after.

That answers definitively what feeds the touch panel reply bytes.
"""
import json
import os
import subprocess
import sys
import time

GAME_DIR = r"G:\Original\maimai"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "probe_out")
os.makedirs(OUT, exist_ok=True)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "pc"))

import probe_touch as P  # noqa: E402
from touch_inject import SyntheticTouch  # noqa: E402

SPY_JS = r"""
const k32 = Process.getModuleByName('kernel32.dll');
const handles = new Map();
const lastPayload = new Map();
const interesting = /COM\d|touch|maitouch/i;

function hkey(v) { return (v >>> 0).toString(16); }

Interceptor.attach(k32.getExportByName('CreateFileW'), {
  onEnter(args) { this.name = args[0].isNull() ? '' : args[0].readUtf16String(); },
  onLeave(retval) {
    if (retval.isNull() || (retval.toInt32() >>> 0) === 0xffffffff) return;
    const h = hkey(retval.toInt32());
    handles.set(h, this.name);
    if (interesting.test(this.name)) send({ t: 'open', h: h, name: this.name });
  }
});

function hexstr(ptr, len) {
  let out = '';
  const n = Math.min(len, 512);
  for (let i = 0; i < n; i++) {
    const b = ptr.add(i).readU8();
    out += (b < 16 ? '0' : '') + b.toString(16) + ' ';
  }
  return out.trim();
}

Interceptor.attach(k32.getExportByName('ReadFile'), {
  onEnter(args) {
    this.h = hkey(args[0].toInt32());
    this.buf = args[1];
    this.len = args[2].toInt32();
    this.out = args[3];
    this.name = handles.get(this.h) || '';
  },
  onLeave(retval) {
    if (!this.name || !interesting.test(this.name)) return;
    let n = 0;
    try { n = this.out.readU32(); } catch (e) { }
    if (!n) return;
    const hex = hexstr(this.buf, n);
    if (lastPayload.get(this.h) !== hex) {
      lastPayload.set(this.h, hex);
      send({ t: 'read', h: this.h, name: this.name, n: n, hex: hex });
    }
  }
});

Interceptor.attach(k32.getExportByName('WriteFile'), {
  onEnter(args) {
    const h = hkey(args[0].toInt32());
    const name = handles.get(h) || '';
    if (!name || !interesting.test(name)) return;
    const len = args[2].toInt32();
    send({ t: 'write', h: h, name: name, n: len, hex: hexstr(args[1], len) });
  }
});

send({ t: 'ready' });
"""


def launch():
    return subprocess.Popen(
        [os.path.join(GAME_DIR, "mice.exe")],
        cwd=GAME_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=0x00000008,
    )


def main():
    import frida

    wait = int(sys.argv[1]) if len(sys.argv) > 1 else 55
    win = P.find_game_window(1)
    if win is None:
        print("[i] launching game ...")
        launch()
    win = P.find_game_window(180)
    if win is None:
        print("no game window")
        return 1
    print("[i] window:", win["exe"], "pid", win["pid"])
    print(f"[i] waiting {wait}s for boot ...")
    time.sleep(wait)

    events = []
    session = frida.attach(win["pid"])
    script = session.create_script(SPY_JS)
    script.on("message", lambda msg, data: events.append(msg["payload"]) if msg.get("type") == "send" else print("JSError", msg))
    script.load()
    print("[i] frida attached, baseline 4s")
    time.sleep(4.0)
    mark_idle = len(events)

    cx0, cy0, cw, ch = P.client_rects(win["hwnd"])
    cx, cy = cx0 + cw // 2, cy0 + ch // 2
    bbox = (cx0, cy0, cx0 + cw, cy0 + ch)
    P.user32.SetForegroundWindow(win["hwnd"])

    print("[i] phase: mouse hover in window 3s")
    P.user32.SetCursorPos(cx, cy - 320)
    time.sleep(3.0)
    mark_hover = len(events)

    print("[i] phase: synthetic touch (2 contacts) 3s")
    touch = SyntheticTouch(max_contacts=2)
    touch.down(1, cx - 300, cy)
    touch.down(2, cx, cy + 300)
    time.sleep(3.0)
    P.grab(bbox, os.path.join(OUT, "spy_touch.png"))
    touch.up(2)
    touch.up(1)
    touch.close()
    mark_touch = len(events)

    print("[i] phase: cursor parked outside 3s")
    P.user32.SetCursorPos(cx0 + 2, cy0 + 2)
    time.sleep(3.0)
    mark_after = len(events)

    script.unload()
    session.detach()

    def dump(a, b, label):
        print(f"\n===== {label}  ({b - a} events) =====")
        reads = {}
        for p in events[a:b]:
            if p["t"] == "open":
                print(f"  OPEN   h={p['h']} {p['name']}")
            elif p["t"] == "read":
                reads.setdefault((p["name"], p["hex"]), 0)
                reads[(p["name"], p["hex"])] += 1
            else:
                print(f"  WRITE  h={p['h']} {p['name']} n={p['n']}: {p['hex']}")
        for (name, hx), count in list(reads.items())[:60]:
            print(f"  READ   {name} x{count}: {hx}")

    dump(0, mark_idle, "IDLE")
    dump(mark_idle, mark_hover, "MOUSE HOVER")
    dump(mark_hover, mark_touch, "SYNTHETIC TOUCH")
    dump(mark_touch, mark_after, "AFTER")

    with open(os.path.join(OUT, "frida_events.json"), "w", encoding="utf-8") as f:
        json.dump(events, f, indent=1, ensure_ascii=False)

    subprocess.run(["taskkill", "/F", "/IM", "maimai_dump_.exe"], capture_output=True)
    subprocess.run(["taskkill", "/F", "/IM", "maimai.exe"], capture_output=True)
    subprocess.run(["taskkill", "/F", "/IM", "mice.exe"], capture_output=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
