"""Proof #3: who POPS the maitouch FIFO (i.e. the real game-side read).

Every mice device object owns a 2 KiB ring (read ptr dev+0x800, write ptr
dev+0x802).  port_write appends to it; something must pop it on behalf of the
game.  This script hooks the ring reader at mice.dll+0x18E05 and dumps

  * (this, buf, n) of every call
  * the bytes actually popped
  * a backtrace restricted to mice.dll (so we learn the caller chain)

while we feed a distinctive "all sensors" packet from outside.  If our packet
shows up in a popped buffer, the game received it.

Run:  python tools/frida_ring_proof.py
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

RING_JS = r"""
(function () {
  let BASE = null, END = null;
  let st = null;
  function fresh() { return { calls: [], n: 0, pk14: {} }; }
  st = fresh();
  function hex(p, n) {
    if (p.isNull() || n <= 0) return '';
    n = Math.min(n, 64);
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
    END = BASE.add(mod.size);
    // device -> game port_write (14 byte status packets)
    Interceptor.attach(BASE.add(0x11793), {
      onEnter(args) {
        const n = args[0].toInt32();
        if (n === 14) {
          const h = hex(this.context.edx, 14);
          st.pk14[h] = (st.pk14[h] || 0) + 1;
        }
      }
    });
    // ring pop: (this=ecx, buf=edx, n=[esp+4])
    Interceptor.attach(BASE.add(0x18e05), {
      onEnter(args) {
        this.self = this.context.ecx;
        this.buf = this.context.edx;
        this.n = args[0].toInt32();
        this.bt = '';
        try {
          const frames = Thread.backtrace(this.context, Backtracer.ACCURATE)
            .map(a => {
              const inMod = a.compare(BASE) >= 0 && a.compare(END) < 0;
              return (inMod ? 'mice+' : '0x') + (inMod ? a.sub(BASE).toString() : a.toString());
            }).slice(0, 6);
          this.bt = frames.join(' < ');
        } catch (e) { }
      },
      onLeave(ret) {
        st.n++;
        if (st.calls.length < 300) {
          st.calls.push({ self: this.self.toString(), n: this.n, buf: hex(this.buf, this.n),
                          ret: ret.toInt32(), bt: this.bt });
        }
      }
    });
    send({ t: 'ready', base: BASE.toString(), size: mod.size });
  }
  let tries = 600;
  (function wait() {
    let m = null;
    try { m = Process.getModuleByName('mice.dll'); } catch (e) { m = null; }
    if (m) { install(m); return; }
    if (--tries <= 0) { send({ t: 'error', msg: 'no module' }); return; }
    setTimeout(wait, 100);
  })();
})()
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
        script = session.create_script(RING_JS)
        script.on("message", lambda m, d: print("  [agent]", m["payload"]))
        script.load()
        ex = script.exports_sync
        print(f"[i] pid={pid}")
        deadline = time.time() + args.ready_timeout
        while time.time() < deadline:
            if ex.peek()["pk14"]:
                break
            time.sleep(0.5)
        print("[i] active")

        w = MS.MaitouchWriter(pid=pid)
        rep["probe_before"] = w.probe()
        w.freeze()
        pkt = MS.packet_from_zones(list(MS.ZONES))
        target = pkt.hex()
        w.write_packet(pkt)
        print("[i] frozen + wrote", pkt.hex(" "))

        ex.reset()
        t0 = time.time()
        while time.time() - t0 < 4.0:
            w.write_packet(pkt)
            time.sleep(0.02)
        s = ex.take()
        rep["wire"] = s["pk14"]
        rep["ring_calls"] = s["calls"]
        rep["ring_n"] = s["n"]
        rep["target"] = target
        hits = [c for c in s["calls"] if target in c["buf"]]
        rep["hits"] = len(hits)
        print(f"[i] ring pops: {s['n']}, wire packets: {list(s['pk14'])}")
        print(f"[i] pops containing our packet: {len(hits)}")
        seen = {}
        for c in s["calls"]:
            key = (c["bt"], c["n"])
            if key not in seen:
                seen[key] = c
        for c in list(seen.values())[:12]:
            print(f"    n={c['n']} buf={c['buf'][:64]} self={c['self']}")
            print(f"       bt: {c['bt']}")
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
        path = os.path.join(OUT, "ring_proof.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=1, ensure_ascii=False)
        print("[i] ->", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
