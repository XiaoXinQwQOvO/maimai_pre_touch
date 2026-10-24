"""Proof that the GAME (maimai_dump_.exe) itself consumes our injected packet.

mice.dll does not only write to a serial port: it hooks the game's kernel32
ReadFile/WriteFile and serves the virtual COM3 from an internal buffer.  The
mock ReadFile lives at mice.dll+0x84B6:

    ReadFile(hFile, buf, nBytes, &read, overlapped)
      obj  = lookup(hFile);              // +0x1140(this=hFile)
      dev  = *(obj + 0x18)
      fn   = *(dev + 0x10)               // per-device read handler
      *read = 0
      if (!fn) log("ReadFile(%ls) unimplemented")
      else     return fn(dev, buf, nBytes, &read, overlapped)

So hooking +0x84B6 shows exactly what *the game* receives when it calls
ReadFile on the maitouch COM port.

This script: freeze build_status, write an "all 17 sensors pressed" packet and
then dump the bytes the game's ReadFile calls return.

Run:  python tools/frida_read_proof.py
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

READ_JS = r"""
(function () {
  let BASE = null;
  let st = null;
  function fresh() { return { reads: [], nreads: 0, pk14: {} }; }
  st = fresh();
  function hex(p, n) {
    if (p.isNull() || n <= 0) return '';
    n = Math.min(n, 128);
    const u8 = new Uint8Array(p.readByteArray(n));
    let s = '';
    for (let i = 0; i < u8.length; i++) s += u8[i].toString(16).padStart(2, '0');
    return s;
  }
  rpc.exports = {
    reset() { st = fresh(); return true; },
    take() { const s = JSON.parse(JSON.stringify(st)); st = fresh(); return s; },
    peek() { return JSON.parse(JSON.stringify(st)); }
  };
  function install(mod) {
    BASE = mod.base;
    // device -> game port write (same as the other agent)
    Interceptor.attach(BASE.add(0x11793), {
      onEnter(args) {
        const n = args[0].toInt32();
        if (n === 14) {
          const h = hex(this.context.edx, 14);
          st.pk14[h] = (st.pk14[h] || 0) + 1;
        }
      }
    });
    // the game's ReadFile, served by mice.dll
    Interceptor.attach(BASE.add(0x84b6), {
      onEnter(args) {
        this.buf = args[1];
        this.want = args[2].toInt32();
        this.pread = args[3];
        this.h = args[0].toString();
      },
      onLeave(ret) {
        let got = 0;
        try { got = this.pread.readU32(); } catch (e) { }
        st.nreads++;
        if (st.reads.length < 200) st.reads.push({ h: this.h, want: this.want, got: got,
                                                   ret: ret.toInt32(), hex: hex(this.buf, got) });
      }
    });
    send({ t: 'ready', base: BASE.toString() });
  }
  let tries = 600;
  (function wait() {
    let m = null;
    try { m = Process.getModuleByName('mice.dll'); } catch (e) { m = null; }
    if (m) { install(m); return; }
    if (--tries <= 0) { send({ t: 'error', msg: 'no module' }); return; }
    setTimeout(wait, 100);
  })();
})();
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ready-timeout", type=float, default=150.0)
    args = ap.parse_args()
    import frida
    rep = {}
    kill_all()
    print("[i] launching")
    launch()
    try:
        dev = frida.get_local_device()
        t0 = time.time()
        pid = None
        while time.time() - t0 < 60:
            for p in dev.enumerate_processes():
                if p.name.lower() in ("maimai_dump_.exe", "maimai.exe"):
                    pid = p.pid
                    break
            if pid:
                break
            time.sleep(0.05)
        session = frida.attach(pid)
        script = session.create_script(READ_JS)
        script.on("message", lambda m, d: print("  [agent]", m["payload"]))
        script.load()
        ex = script.exports_sync
        win = P.find_game_window(90)
        print(f"[i] pid={pid} hwnd={win['hwnd']:#x}" if win else f"[i] pid={pid}")

        deadline = time.time() + args.ready_timeout
        while time.time() < deadline:
            s = ex.peek()
            if s["pk14"]:
                break
            time.sleep(0.5)
        print("[i] active; baseline reads 3s")
        ex.reset()
        time.sleep(3.0)
        base = ex.take()
        rep["baseline"] = base
        print("[i] baseline: game ReadFile calls =", base["nreads"],
              "sample:", [r["hex"] for r in base["reads"][:4]])

        w = MS.MaitouchWriter(pid=pid)
        rep["probe"] = w.probe()
        w.freeze()
        pkt = MS.packet_from_zones(list(MS.ZONES))
        w.write_packet(pkt)
        print("[i] froze build_status and wrote", pkt.hex(" "))
        ex.reset()
        time.sleep(3.0)
        s = ex.take()
        rep["injected"] = {"packet": pkt.hex(), "wire_packets": s["pk14"],
                           "game_reads": s["reads"], "nreads": s["nreads"]}
        target = pkt.hex()
        delivered = [r for r in s["reads"] if target in r["hex"]]
        rep["match_count"] = len(delivered)
        print(f"[i] game ReadFile calls: {s['nreads']}, window writes: {list(s['pk14'])}")
        print(f"[i] READ calls whose payload contains our packet: {len(delivered)}")
        for r in s["reads"][:8]:
            print(f"    want={r['want']} got={r['got']} ret={r['ret']} {r['hex']}")

        w.restore()
        ex.reset()
        time.sleep(1.5)
        s2 = ex.take()
        rep["after_restore"] = {"nreads": s2["nreads"],
                                "still_matching": len([r for r in s2["reads"] if target in r["hex"]]),
                                "sample": [r["hex"] for r in s2["reads"][:4]]}
        print("[i] after restore:", rep["after_restore"])
        w.close()
        script.unload()
        session.detach()
    except BaseException as e:  # noqa: BLE001
        rep["error"] = f"{type(e).__name__}: {e}"
        print("[!]", rep["error"])
    finally:
        kill_all()
        path = os.path.join(OUT, "read_proof.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=1, ensure_ascii=False)
        print("[i] ->", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
