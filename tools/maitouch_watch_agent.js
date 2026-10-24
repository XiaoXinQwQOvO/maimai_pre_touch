/* frida agent: watch ONLY the maitouch port I/O of mice.dll.
 *
 * Deliberately does not touch mice.dll!build_status (RVA 0x43FD), because
 * frida's Interceptor rewrites a hooked function's prologue with a jump, which
 * would make the external `ret` patch in tools/maitouch_state.py impossible.
 */
(function () {
  let BASE = null;
  let st = null;

  function fresh() {
    return { pk14: {}, writes: {}, reads: [], nreads: 0, nwrites: 0 };
  }
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
    peek() { return JSON.parse(JSON.stringify(st)); },
    readPacket() { return hex(ptr(BASE).add(0x18b040), 14); },
    readByteAt(off) { return ptr(BASE).add(off).readU8(); }
  };

  function install(mod) {
    BASE = mod.base;
    try {
      // port_write(this, buf, n)
      Interceptor.attach(BASE.add(0x11793), {
        onEnter(args) {
          const n = args[0].toInt32();
          const h = hex(this.context.edx, n);
          st.nwrites++;
          if (n === 14) st.pk14[h] = (st.pk14[h] || 0) + 1;
          else if (Object.keys(st.writes).length < 200) st.writes[n + ':' + h] = (st.writes[n + ':' + h] || 0) + 1;
        }
      });
      // port_read(this, buf, n)
      Interceptor.attach(BASE.add(0x1175e), {
        onEnter(args) { this.n = args[0].toInt32(); this.buf = this.context.edx; },
        onLeave() {
          if (st.reads.length < 400) st.reads.push([this.n, hex(this.buf, this.n)]);
          st.nreads++;
        }
      });
      send({ t: 'ready', base: BASE.toString(), module: mod.path });
    } catch (e) {
      send({ t: 'error', msg: e.message });
    }
  }

  let tries = 600;
  (function wait() {
    let m = null;
    try { m = Process.getModuleByName('mice.dll'); } catch (e) { m = null; }
    if (m) { install(m); return; }
    if (--tries <= 0) { send({ t: 'error', msg: 'mice.dll never loaded' }); return; }
    setTimeout(wait, 100);
  })();
})();
