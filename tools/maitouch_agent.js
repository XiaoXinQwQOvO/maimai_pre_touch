/* frida agent: maitouch internals of mice.dll (RVA based, base-agnostic). */
(function () {
  let BASE = null;
  let st = null;

  function fresh() {
    return { addpoint: 0, builds: 0, pk14: {}, reads: [], nreads: 0, writes: {},
             contacts_add: 0, contacts_del: 0, inputmap: {}, wh: {}, active: -1 };
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
    listState() {
      const out = {};
      try {
        const vec = ptr(BASE).add(0x1614e0);
        out.count = vec.add(4).readU32();
        out.stride = vec.add(8).readU32();
        const data = vec.add(0xc).readPointer();
        out.data = data.toString();
        if (!data.isNull() && out.count < 40) {
          out.entries = [];
          for (let i = 0; i < out.count; i++) {
            const e = data.add(i * out.stride);
            out.entries.push([e.add(0).readU32(), e.add(4).readS32(), e.add(8).readS32()]);
          }
        }
        out.packet = hex(ptr(BASE).add(0x18b040), 14);
        out.active = ptr(BASE).add(0x161248).readU32();
        out.hwnd = ptr(BASE).add(0x18aac0).readPointer().toString();
      } catch (e) { out.error = e.message; }
      return out;
    }
  };

  function install(mod) {
    const B = mod.base;
    BASE = B;
    try {
      // add_point(width, height, client_x, client_y)
      Interceptor.attach(B.add(0x431b), {
        onEnter(args) {
          this.w = this.context.ecx >>> 0;
          this.h = this.context.edx >>> 0;
          this.x = args[0].toInt32();
          this.y = args[1].toInt32();
        },
        onLeave(ret) {
          st.addpoint++;
          const idx = ret.toInt32() & 0xff;
          let byte = -1;
          try { byte = ptr(BASE).add(0x18b040 + idx).readU8(); } catch (e) { }
          if (byte >= 0) {
            const k = this.x + ',' + this.y + ',' + this.w + ',' + this.h;
            if (Object.keys(st.inputmap).length < 6000) st.inputmap[k] = idx + ':' + byte;
            st.wh[this.w + 'x' + this.h] = (st.wh[this.w + 'x' + this.h] || 0) + 1;
          }
        }
      });
      // build_status()
      Interceptor.attach(B.add(0x43fd), { onEnter() { st.builds++; } });
      // port_write(this, buf, n)   device -> game
      Interceptor.attach(B.add(0x11793), {
        onEnter(args) {
          const n = args[0].toInt32();
          const h = hex(this.context.edx, n);
          if (n === 14) st.pk14[h] = (st.pk14[h] || 0) + 1;
          else if (Object.keys(st.writes).length < 200) st.writes[n + ':' + h] = (st.writes[n + ':' + h] || 0) + 1;
        }
      });
      // port_read(this, buf, n)    game -> device
      Interceptor.attach(B.add(0x1175e), {
        onEnter(args) { this.n = args[0].toInt32(); this.buf = this.context.edx; },
        onLeave() {
          if (st.reads.length < 400) st.reads.push([this.n, hex(this.buf, this.n)]);
          st.nreads++;
        }
      });
      // contact_list_upsert(pointerId, x, y, incontact)
      Interceptor.attach(B.add(0x8dcd), {
        onEnter(args) {
          if (args[1].toInt32()) st.contacts_add++; else st.contacts_del++;
        }
      });
      // subclass WndProc: record message ids
      Interceptor.attach(B.add(0x8fc1), {
        onEnter(args) {
          if (!st.wndmsg) st.wndmsg = {};
          const m = args[1].toInt32() & 0xffff;
          const k = '0x' + m.toString(16);
          st.wndmsg[k] = (st.wndmsg[k] || 0) + 1;
        }
      });
      send({ t: 'ready', base: B.toString(), module: mod.path });
    } catch (e) {
      send({ t: 'error', msg: e.message, stack: e.stack });
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
