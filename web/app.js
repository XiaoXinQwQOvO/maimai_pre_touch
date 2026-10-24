/* maimai_pre_touch phone client
 *
 * Shows the PC's game screen over WebRTC and turns touches into the sensor
 * events the game accepts.  The panel is the 17-sensor layout that micetools'
 * maitouch device reports (see pc/sensors.py):
 *
 *    centre            r <  0.2241          -> sensor C
 *    inner ring        r <  0.5518          -> B1..B8
 *    outer ring        r <  1.0             -> A1..A8
 *    outside           r >= 1.0             -> ignored
 *
 * where r is measured in units of half the game client area, 0 deg points to
 * screen-right and grows clockwise (8 sectors of 45 deg).
 *
 * Coordinates sent to the PC are normalised to the video content box (the game
 * client area), so phone resolution, orientation and letterboxing do not matter.
 */
(() => {
  "use strict";

  const PANEL_DEFAULT = { r_centre: 0.2241, r_inner: 0.5518, r_max: 1.0, sectors: 8 };

  const stage = document.getElementById("stage");
  const video = document.getElementById("video");
  const canvas = document.getElementById("overlay");
  const ctx = canvas.getContext("2d");
  const banner = document.getElementById("banner");
  const hudNet = document.getElementById("hud-net");
  const hudZone = document.getElementById("hud-zone");
  const hudVideo = document.getElementById("hud-video");
  const hudRtt = document.getElementById("hud-rtt");
  const hudTouch = document.getElementById("hud-touch");
  const panel = document.getElementById("panel");

  const store = {
    get(key, fallback) {
      try {
        const raw = localStorage.getItem("mp." + key);
        return raw === null ? fallback : JSON.parse(raw);
      } catch (_) {
        return fallback;
      }
    },
    set(key, value) {
      try {
        localStorage.setItem("mp." + key, JSON.stringify(value));
      } catch (_) {}
    },
  };

  const state = {
    server: store.get("server", location.host),
    panel: Object.assign({}, PANEL_DEFAULT, store.get("panel", {})),
    snap: store.get("snap", true),
    showGrid: store.get("showGrid", true),
    showLabels: store.get("showLabels", true),
    contacts: new Map(),
    contactSeq: 0,
    ws: null,
    pc: null,
    rtt: null,
    frames: 0,
    fps: 0,
    retry: 0,
    lastMoveSent: 0,
    touchCounts: { down: 0, move: 0, up: 0 },
    touchBackend: "?",
  };

  const clamp01 = (v) => (v < 0 ? 0 : v > 1 ? 1 : v);

  // ------------------------------------------------------------- sensor math
  function sensorAt(nx, ny, model) {
    const u = 2 * nx - 1;
    const v = 2 * ny - 1;
    const r = Math.hypot(u, v);
    if (r >= model.r_max) return null;
    if (r < model.r_centre) return { name: "C", label: "C", radius: 0, sector: -1 };
    const ring = r >= model.r_inner ? "O" : "I";
    const deg = ((Math.atan2(v, u) * 180) / Math.PI + 360) % 360;
    const sector = Math.floor(deg / (360 / model.sectors)) % model.sectors;
    return {
      name: ring + sector * 45,
      label: (ring === "O" ? "A" : "B") + (sector + 1),
      radius: r,
      sector,
    };
  }

  function sensorPoint(sensor, model) {
    if (!sensor || sensor.name === "C") return [0.5, 0.5];
    const radius = sensor.name.startsWith("I")
      ? (model.r_centre + model.r_inner) / 2
      : (model.r_inner + model.r_max) / 2;
    const angle = ((sensor.sector + 0.5) * (360 / model.sectors) * Math.PI) / 180;
    return [0.5 + (radius * Math.cos(angle)) / 2, 0.5 + (radius * Math.sin(angle)) / 2];
  }

  // ---------------------------------------------------------------- geometry
  function contentBox() {
    const cw = stage.clientWidth;
    const ch = stage.clientHeight;
    const vw = video.videoWidth || cw;
    const vh = video.videoHeight || ch;
    const scale = Math.min(cw / vw, ch / vh);
    const w = vw * scale;
    const h = vh * scale;
    return { x: (cw - w) / 2, y: (ch - h) / 2, w, h };
  }

  function toNormalised(clientX, clientY) {
    const box = contentBox();
    const rect = stage.getBoundingClientRect();
    return {
      nx: clamp01((clientX - rect.left - box.x) / box.w),
      ny: clamp01((clientY - rect.top - box.y) / box.h),
    };
  }

  function setBanner(text) {
    if (!text) {
      banner.classList.add("hidden");
      return;
    }
    banner.textContent = text;
    banner.classList.remove("hidden");
  }

  // ------------------------------------------------------------------- WS
  function wsUrl() {
    const server = state.server || location.host;
    return "ws://" + server.replace(/^https?:\/\//, "") + "/ws";
  }

  function connect() {
    if (state.ws && (state.ws.readyState === WebSocket.OPEN || state.ws.readyState === WebSocket.CONNECTING)) return;
    setBanner("连接 " + wsUrl() + " …");
    let ws;
    try {
      ws = new WebSocket(wsUrl());
    } catch (err) {
      scheduleReconnect(err && err.message);
      return;
    }
    state.ws = ws;
    ws.onopen = () => {
      state.retry = 0;
      setBanner("已连接，正在建立视频…");
      send({ t: "hello", role: "phone", ua: navigator.userAgent });
      startPeer();
      startPing();
    };
    ws.onclose = () => {
      stopPeer();
      scheduleReconnect("连接已断开");
    };
    ws.onerror = () => {};
    ws.onmessage = (event) => {
      let msg;
      try {
        msg = JSON.parse(event.data);
      } catch (_) {
        return;
      }
      handleMessage(msg);
    };
  }

  function scheduleReconnect(reason) {
    state.retry = Math.min(state.retry + 1, 8);
    const delay = Math.min(500 * Math.pow(1.6, state.retry), 5000);
    setBanner((reason ? reason + "\n" : "") + "重试中…");
    setTimeout(connect, delay);
  }

  function send(obj) {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify(obj));
      return true;
    }
    return false;
  }

  async function handleMessage(msg) {
    switch (msg.t) {
      case "welcome":
        if (msg.panel) {
          state.panel = Object.assign({}, PANEL_DEFAULT, msg.panel);
          syncPanel();
        }
        if (msg.touch_backend) {
          state.touchBackend = msg.touch_backend;
          hudNet.textContent = "已连接 · " + state.server + " · " + state.touchBackend;
        }
        break;
      case "answer":
        if (state.pc && msg.sdp) {
          await state.pc.setRemoteDescription({ type: msg.type || "answer", sdp: msg.sdp });
          setBanner(null);
        }
        break;
      case "pong":
        if (typeof msg.ts === "number") {
          state.rtt = Math.round(performance.now() - msg.ts);
          hudRtt.textContent = "延迟：" + state.rtt + " ms";
        }
        break;
      default:
        break;
    }
  }

  function startPing() {
    clearInterval(state.pingTimer);
    state.pingTimer = setInterval(() => send({ t: "ping", ts: performance.now() }), 2000);
  }

  // --------------------------------------------------------------- WebRTC
  function waitIce(pc) {
    return new Promise((resolve) => {
      if (pc.iceGatheringState === "complete") return resolve();
      const timer = setTimeout(resolve, 2500);
      pc.addEventListener("icegatheringstatechange", () => {
        if (pc.iceGatheringState === "complete") {
          clearTimeout(timer);
          resolve();
        }
      });
    });
  }

  async function startPeer() {
    stopPeer();
    const pc = new RTCPeerConnection({ iceServers: [], bundlePolicy: "max-bundle" });
    state.pc = pc;
    const transceiver = pc.addTransceiver("video", { direction: "recvonly" });
    // Prefer H.264: the PC encodes with NVENC.
    try {
      const caps = RTCRtpSender.getCapabilities("video");
      if (caps && caps.codecs && transceiver.setCodecPreferences) {
        const isH264 = (c) => (c.mimeType || "").toLowerCase() === "video/h264";
        transceiver.setCodecPreferences(caps.codecs.filter(isH264).concat(caps.codecs.filter((c) => !isH264(c))));
      }
    } catch (_) {}
    pc.ontrack = (event) => {
      video.srcObject = event.streams[0];
      video.play().catch(() => {});
      video.onloadedmetadata = () => {
        video.play().catch(() => {});
        setBanner(null);
      };
    };
    pc.onconnectionstatechange = () => {
      if (["failed", "disconnected", "closed"].includes(pc.connectionState)) {
        setBanner("视频连接 " + pc.connectionState + "，重连中…");
        scheduleReconnect("视频连接中断");
      }
    };
    try {
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      await waitIce(pc);
      send({ t: "offer", type: "offer", sdp: pc.localDescription.sdp });
    } catch (err) {
      setBanner("WebRTC 失败：" + (err && err.message ? err.message : err));
    }
  }

  function stopPeer() {
    if (state.pc) {
      try {
        state.pc.close();
      } catch (_) {}
      state.pc = null;
    }
  }

  // ---------------------------------------------------------------- touch
  function emitTouch(contact, action) {
    let x = contact.nx;
    let y = contact.ny;
    if (state.snap && contact.sensor) {
      [x, y] = sensorPoint(contact.sensor, state.panel);
    }
    contact.nx = x;
    contact.ny = y;
    state.touchCounts[action] += 1;
    send({ t: "touch", id: contact.id, a: action, x: +x.toFixed(5), y: +y.toFixed(5) });
    hudTouch.textContent = "触摸：↓" + state.touchCounts.down + " ↔" + state.touchCounts.move + " ↑" + state.touchCounts.up;
  }

  function onPointerDown(event) {
    if (panel && !panel.classList.contains("hidden")) return;
    if (event.target.closest && event.target.closest("#btn-panel")) return;
    event.preventDefault();
    stage.setPointerCapture(event.pointerId);
    const { nx, ny } = toNormalised(event.clientX, event.clientY);
    const contact = {
      // ids must never be reused: the PC unions contacts by id
      id: (state.contactSeq = (state.contactSeq % 1e6) + 1),
      nx,
      ny,
      sensor: sensorAt(nx, ny, state.panel),
    };
    state.contacts.set(event.pointerId, contact);
    hudZone.textContent = contact.sensor ? contact.sensor.label : "圆外";
    emitTouch(contact, "down");
  }

  function onPointerMove(event) {
    const contact = state.contacts.get(event.pointerId);
    if (!contact) return;
    event.preventDefault();
    const now = performance.now();
    if (now - state.lastMoveSent < 12) return;
    state.lastMoveSent = now;
    const { nx, ny } = toNormalised(event.clientX, event.clientY);
    contact.nx = nx;
    contact.ny = ny;
    contact.sensor = sensorAt(nx, ny, state.panel);
    emitTouch(contact, "move");
  }

  function onPointerUp(event) {
    const contact = state.contacts.get(event.pointerId);
    if (!contact) return;
    event.preventDefault();
    emitTouch(contact, "up");
    state.contacts.delete(event.pointerId);
    hudZone.textContent = "—";
  }

  stage.addEventListener("pointerdown", onPointerDown, { passive: false });
  stage.addEventListener("pointermove", onPointerMove, { passive: false });
  stage.addEventListener("pointerup", onPointerUp, { passive: false });
  stage.addEventListener("pointercancel", onPointerUp, { passive: false });
  stage.addEventListener("contextmenu", (e) => e.preventDefault());
  document.addEventListener("gesturestart", (e) => e.preventDefault());

  // --------------------------------------------------------------- overlay
  function resizeCanvas() {
    const dpr = Math.min(window.devicePixelRatio || 1, 3);
    canvas.width = Math.round(stage.clientWidth * dpr);
    canvas.height = Math.round(stage.clientHeight * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  window.addEventListener("resize", resizeCanvas);

  function drawOverlay() {
    const w = stage.clientWidth;
    const h = stage.clientHeight;
    ctx.clearRect(0, 0, w, h);
    const box = contentBox();
    const cx = box.x + box.w / 2;
    const cy = box.y + box.h / 2;
    const unit = Math.min(box.w, box.h) / 2;
    const model = state.panel;

    if (state.showGrid) {
      ctx.strokeStyle = "rgba(79, 209, 255, 0.30)";
      ctx.lineWidth = 1;
      for (const radius of [model.r_centre, model.r_inner, model.r_max]) {
        ctx.beginPath();
        ctx.arc(cx, cy, radius * unit, 0, Math.PI * 2);
        ctx.stroke();
      }
      const sectors = model.sectors;
      for (let i = 0; i < sectors; i++) {
        const angle = (i * (360 / sectors) * Math.PI) / 180;
        ctx.beginPath();
        ctx.moveTo(cx + model.r_centre * unit * Math.cos(angle), cy + model.r_centre * unit * Math.sin(angle));
        ctx.lineTo(cx + model.r_max * unit * Math.cos(angle), cy + model.r_max * unit * Math.sin(angle));
        ctx.stroke();
      }
      if (state.showLabels) {
        ctx.fillStyle = "rgba(232, 241, 255, 0.45)";
        ctx.font = "11px sans-serif";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText("C", cx, cy);
        for (let s = 0; s < sectors; s++) {
          const angle = ((s + 0.5) * (360 / sectors) * Math.PI) / 180;
          const innerR = ((model.r_centre + model.r_inner) / 2) * unit;
          const outerR = ((model.r_inner + model.r_max) / 2) * unit;
          ctx.fillText("B" + (s + 1), cx + innerR * Math.cos(angle), cy + innerR * Math.sin(angle));
          ctx.fillText("A" + (s + 1), cx + outerR * Math.cos(angle), cy + outerR * Math.sin(angle));
        }
      }
    }

    for (const contact of state.contacts.values()) {
      const px = box.x + contact.nx * box.w;
      const py = box.y + contact.ny * box.h;
      const grad = ctx.createRadialGradient(px, py, 0, px, py, 34);
      grad.addColorStop(0, "rgba(79, 209, 255, 0.55)");
      grad.addColorStop(1, "rgba(79, 209, 255, 0)");
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.arc(px, py, 34, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = "rgba(79, 209, 255, 0.9)";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(px, py, 14, 0, Math.PI * 2);
      ctx.stroke();
      ctx.fillStyle = "#e8f1ff";
      ctx.font = "bold 12px sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(contact.sensor ? contact.sensor.label : "圆外", px, py - 24);
    }

    requestAnimationFrame(drawOverlay);
  }

  // ----------------------------------------------------------------- panel
  function syncPanel() {
    document.getElementById("chk-snap").checked = state.snap;
    document.getElementById("chk-grid").checked = state.showGrid;
    document.getElementById("chk-labels").checked = state.showLabels;
    document.getElementById("in-server").value = state.server;
    document.getElementById("hud-model").textContent =
      "面板：" + state.panel.sectors + " 扇区 × 2 环 + 中心（17 传感器）";
  }

  function bindPanel() {
    document.getElementById("chk-snap").addEventListener("change", (e) => {
      state.snap = e.target.checked;
      store.set("snap", state.snap);
    });
    document.getElementById("chk-grid").addEventListener("change", (e) => {
      state.showGrid = e.target.checked;
      store.set("showGrid", state.showGrid);
    });
    document.getElementById("chk-labels").addEventListener("change", (e) => {
      state.showLabels = e.target.checked;
      store.set("showLabels", state.showLabels);
    });
    document.getElementById("btn-reset").addEventListener("click", () => {
      state.panel = Object.assign({}, PANEL_DEFAULT);
      store.set("panel", state.panel);
      syncPanel();
    });
    document.getElementById("btn-reconnect").addEventListener("click", () => {
      state.server = document.getElementById("in-server").value.trim() || location.host;
      store.set("server", state.server);
      if (state.ws) {
        try {
          state.ws.close();
        } catch (_) {}
      }
      connect();
    });
    document.getElementById("btn-panel").addEventListener("click", () => {
      panel.classList.remove("hidden");
      syncPanel();
    });
    document.getElementById("btn-close").addEventListener("click", () => panel.classList.add("hidden"));
  }

  // ------------------------------------------------------------- fps meter
  function startFpsMeter() {
    setInterval(() => {
      state.fps = state.frames;
      state.frames = 0;
      const vw = video.videoWidth || 0;
      const vh = video.videoHeight || 0;
      hudVideo.textContent = "视频：" + (vw ? vw + "×" + vh : "—") + " @ " + state.fps + "fps";
      if (!state.rtt && state.ws && state.ws.readyState === WebSocket.OPEN) {
        hudNet.textContent = "已连接 · " + state.server + " · " + state.touchBackend;
      }
    }, 1000);
  }

  function countFrames() {
    if (video.requestVideoFrameCallback) {
      const step = () => {
        state.frames++;
        video.requestVideoFrameCallback(step);
      };
      video.requestVideoFrameCallback(step);
    } else {
      setInterval(() => {
        if (!video.paused && video.readyState >= 2) state.frames++;
      }, 16);
    }
  }

  // ------------------------------------------------------------------ boot
  async function boot() {
    resizeCanvas();
    bindPanel();
    syncPanel();
    countFrames();
    startFpsMeter();
    requestAnimationFrame(drawOverlay);
    try {
      if (navigator.wakeLock) await navigator.wakeLock.request("screen");
    } catch (_) {}
    connect();
  }

  boot();
})();
