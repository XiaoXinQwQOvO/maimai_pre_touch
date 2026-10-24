"""Proof #4: is the maitouch TX FIFO actually being drained (= read by the game)?

mice's device class keeps a 2 KiB ring per device object:
    read  pointer  = *(uint16*)(dev + 0x800)
    write pointer  = *(uint16*)(dev + 0x802)     (mask 0x7FF)
`port_write` (RVA 0x11793, this = device object) appends 14-byte status packets
to that ring.  Whoever serves the game's ReadFile pops from it.

So we log, for every 14-byte port_write, the device pointer and both ring
pointers.  Conclusion table:

    read pointer advances over time      -> something is consuming (game reads)
    read pointer pinned (write wraps)    -> nobody reads -> different path

Run:  python tools/frida_fifo_proof.py
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

import maitouch_state as MS  # noqa: E402
from frida_maitouch_probe import kill_all, launch  # noqa: E402

OUT = os.path.join(HERE, "probe_out")
os.makedirs(OUT, exist_ok=True)

FIFO_JS = r"""
(function () {
  let BASE = null;
  let st = null;
  function fresh() { return { samples: [], npush: 0, devs: {}, reads: [], nread: 0 }; }
  st = fresh();
  function hex(p, n) {
    try {
      const u8 = new Uint8Array(p.readByteArray(Math.min(n, 64)));
      let s = '';
      for (let i = 0; i < u8.length; i++) s += u8[i].toString(16).padStart(2, '0');
      return s;
    } catch (e) { return ''; }
  }
  rpc.exports = {
    reset() { st = fresh(); return true; },
    take() { const s = JSON.parse(JSON.stringify(st)); st = fresh(); return s; },
    peek() { return JSON.parse(JSON.stringify(st)); }
  };
  function install(mod) {
    BASE = mod.base;
    Interceptor.attach(BASE.add(0x11793), {
      onEnter(args) {
        const n = args[0].toInt32();
        if (n !== 14) return;
        st.npush++;
        const dev = this.context.ecx;
        try {
          const rd = dev.add(0x800).readU16();
          const wr = dev.add(0x802).readU16();
          const key = dev.toString();
          st.devs[key] = (st.devs[key] || 0) + 1;
          if (st.samples.length < 400) {
            st.samples.push({ dev: key, rd: rd, wr: wr, pkt: hex(this.context.edx, 14) });
          }
        } catch (e) { st.devs['err:' + e.message] = 1; }
      }
    });
    // game -> device direction: which device object, how many bytes, what data
    Interceptor.attach(BASE.add(0x1175e), {
      onEnter(args) {
        this.self = this.context.ecx;
        this.buf = this.context.edx;
        this.n = args[0].toInt32();
      },
      onLeave() {
        st.nread++;
        if (st.reads.length < 200) {
          let rd = -1, wr = -1;
          try { rd = this.self.add(0x800).readU16(); wr = this.self.add(0x802).readU16(); } catch (e) { }
          st.reads.push({ dev: this.self.toString(), n: this.n, hex: hex(this.buf, this.n),
                          rd: rd, wr: wr });
        }
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
  });
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
        script = session.create_script(FIFO_JS)
        script.on("message", lambda m, d: print("  [agent]", m["payload"]))
        script.load()
        ex = script.exports_sync
        print(f"[i] pid={pid}")
        deadline = time.time() + args.ready_timeout
        while time.time() < deadline:
            n = ex.peek()["npush"]
            if n > 100:
                break
            time.sleep(0.5)
        else:
            print(f"[!] no status packets within {args.ready_timeout}s "
                  f"(maitouch not in active mode)")
        print(f"[i] pushing (npush so far: {ex.peek()['npush']})")

        # phase 1: idle, 2 s
        ex.reset()
        time.sleep(2.0)
        s1 = ex.take()
        rep["idle"] = s1
        rds = [x["rd"] for x in s1["samples"]]
        wrs = [x["wr"] for x in s1["samples"]]
        print("[i] idle: %d pushes, devs=%s" % (s1["npush"], s1["devs"]))
        print(f"    read ptr  range {min(rds) if rds else None}..{max(rds) if rds else None} "
              f"distinct={len(set(rds))}")
        print(f"    write ptr range {min(wrs) if wrs else None}..{max(wrs) if wrs else None} "
              f"distinct={len(set(wrs))}")
        print(f"    game->device reads: {s1['nread']}")
        for r in s1["reads"][:8]:
            print(f"      dev={r['dev']} n={r['n']} hex={r['hex'][:40]} rd={r['rd']} wr={r['wr']}")
        rep["idle_distinct_rd"] = len(set(rds))
        rep["idle_distinct_wr"] = len(set(wrs))

        # phase 2: frozen + our packet, 2 s
        w = MS.MaitouchWriter(pid=pid)
        rep["probe"] = w.probe()
        w.freeze()
        pkt = MS.packet_from_zones(list(MS.ZONES))
        w.write_packet(pkt)
        ex.reset()
        t0 = time.time()
        while time.time() - t0 < 2.0:
            w.write_packet(pkt)
            time.sleep(0.02)
        s2 = ex.take()
        rep["injected"] = {"packet": pkt.hex(), "npush": s2["npush"], "devs": s2["devs"],
                           "distinct_rd": len(set(x["rd"] for x in s2["samples"])),
                           "distinct_wr": len(set(x["wr"] for x in s2["samples"])),
                           "pkts": list(set(x["pkt"] for x in s2["samples"]))[:5]}
        rds2 = [x["rd"] for x in s2["samples"]]
        print(f"[i] injected: {s2['npush']} pushes, distinct rd={len(set(rds2))} "
              f"wr={len(set(x['wr'] for x in s2['samples']))}")
        print(f"    packets on the wire: {list(set(x['pkt'] for x in s2['samples']))[:4]}")
        if rds2:
            print(f"    read ptr {min(rds2)}..{max(rds2)}, write ptr "
                  f"{min(x['wr'] for x in s2['samples'])}..{max(x['wr'] for x in s2['samples'])}")
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
        path = os.path.join(OUT, "fifo_proof.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=1, ensure_ascii=False)
        print("[i] ->", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
