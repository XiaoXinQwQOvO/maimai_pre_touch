/* maimai_pre_touch phone client
 *
 * Shows the PC's game screen over WebRTC, turns touches into zone-mapped
 * pointer events and streams them back over the same WebSocket connection.
 */
(() => {
  "use strict";

  const RING_LETTERS = ["A", "B", "C", "D", "E"]; // A = outermost
  const DEFAULTS = {
    center: [0.5, 0.5],
    ring_edges: [0.34, 0.5, 0.66, 0.82, 1.0],
    sectors: 8,
    sector_offset_deg: 0,
  };

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
    zoneMap: Object.assign({}, DEFAULTS, store.get("zoneMap", {})),
    snap: store.get("snap", false),
    showGrid: store.get("showGrid", true),
    showLabels: store.get("showLabels", false),
    contacts: new Map(),
    ws: null,
    pc: null,
    rtt: null,
    frames: 0,
    fps: 0,
    retry: 0,
    lastMoveSent: 0,
    touchCounts: { down: 0, move: 0, up: 0 },
  };

  // ---------------------------------------------------------------- helpers
  const clamp01 = (v) => (v < 0 ? 0 : v > 1 ? 1 : v);

  function zoneOf(nx, ny, zm) {
    const [cx, cy] = zm.center;
    const r = Math.hypot((nx - cx) * 2, (ny - cy) * 2);
    if (r > zm.ring_edges[4]) return null;
    let ring = 0;
    for (let i = 0; i < zm.ring_edges.length; i++) {
      if (r <= zm.ring_edges[i]) {
        ring = i;
        break;
      }
    }
    const ang = ((Math.atan2(nx - cx, -(ny - cy)) * 180) / Math.PI + 360) % 360;
    const shifted = ((ang - zm.sector_offset_deg) % 360 + 360) % 360;
    const sector = Math.floor(shifted / (360 / zm.sectors)) % zm.sectors;
    return { ring, sector, label: RING_LETTERS[RING_LETTERS.length - 1 - ring] + (sector + 1) };
  }

  function pointOf(ring, sector, zm) {
    const edges = zm.ring_edges;
    ring = Math.max(0, Math.min(edges.length - 1, ring | 0));
    const inner = ring > 0 ? edges[ring - 1] : 0;
    const radius = (inner + edges[ring]) / 2;
    const angle = (((sector | 0) + 0.5) * (360 / zm.sectors) + zm.sector_offset_deg) * (Math.PI / 180);
    return [zm.center[0] + (radius * Math.sin(angle)) / 2, zm.center[1] - (radius * Math.cos(angle)) / 2];
  }

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

    ws.onopen = async () => {
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
        if (msg.zone_map) {
          state.zoneMap = Object.assign({}, DEFAULTS, msg.zone_map);
          syncPanel();
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
    // Prefer H.264 (the PC encodes with NVENC); fall back to whatever is left.
    try {
      const caps = RTCRtpSender.getCapabilities("video");
      if (caps && caps.codecs) {
        const h264 = caps.codecs.filter((c) => (c.mimeType || "").toLowerCase() === "video/h264");
        const rest = caps.codecs.filter((c) => (c.mimeType || "").toLowerCase() !== "video/h264");
        if (h264.length && transceiver.setCodecPreferences) {
          transceiver.setCodecPreferences(h264.concat(rest));
        }
      }
    } catch (_) {}
    pc.ontrack = (event) => {
      video.srcObject = event.streams[0];
      const play = () => video.play().catch(() => {});
      play();
      video.onloadedmetadata = () => {
        play();
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
  function contactIdFor(pointerId) {
    if (!state.contacts.has(pointerId)) {
      state.contacts.set(pointerId, { id: state.contacts.size + 1, nx: 0.5, ny: 0.5, zone: null });
    }
    return state.contacts.get(pointerId);
  }

  function emitTouch(contact, action) {
    const [sx, sy] = state.snap && contact.zone ? pointOf(contact.zone.ring, contact.zone.sector, state.zoneMap) : [contact.nx, contact.ny];
    contact.nx = sx;
    contact.ny = sy;
    state.touchCounts[action] += 1;
    send({ t: "touch", id: contact.id, a: action, x: +sx.toFixed(5), y: +sy.toFixed(5), snap: false });
    hudTouch.textContent = "触摸：↓" + state.touchCounts.down + " ↔" + state.touchCounts.move + " ↑" + state.touchCounts.up;
  }

  function onPointerDown(event) {
    if (panel && !panel.classList.contains("hidden")) return;
    if (event.target.closest && event.target.closest("#btn-panel")) return;
    event.preventDefault();
    stage.setPointerCapture(event.pointerId);
    const { nx, ny } = toNormalised(event.clientX, event.clientY);
    const contact = contactIdFor(event.pointerId);
    contact.nx = nx;
    contact.ny = ny;
    contact.zone = zoneOf(nx, ny, state.zoneMap);
    hudZone.textContent = contact.zone ? contact.zone.label : "—";
    emitTouch(contact, "down");
  }

  function onPointerMove(event) {
    if (!state.contacts.has(event.pointerId)) return;
    event.preventDefault();
    const now = performance.now();
    if (now - state.lastMoveSent < 12) return;
    state.lastMoveSent = now;
    const { nx, ny } = toNormalised(event.clientX, event.clientY);
    const contact = state.contacts.get(event.pointerId);
    contact.nx = nx;
    contact.ny = ny;
    contact.zone = zoneOf(nx, ny, state.zoneMap);
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
  stage.addEventListener("pointerleave", onPointerUp, { passive: false });
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
    const cx = box.x + state.zoneMap.center[0] * box.w;
    const cy = box.y + state.zoneMap.center[1] * box.h;
    const unit = Math.min(box.w, box.h) / 2;

    if (state.showGrid) {
      ctx.strokeStyle = "rgba(79, 209, 255, 0.28)";
      ctx.lineWidth = 1;
      for (const edge of state.zoneMap.ring_edges) {
        ctx.beginPath();
        ctx.arc(cx, cy, Math.min(edge * unit, unit), 0, Math.PI * 2);
        ctx.stroke();
      }
      const sectors = state.zoneMap.sectors;
      for (let i = 0; i < sectors; i++) {
        const angle = ((i * (360 / sectors) + state.zoneMap.sector_offset_deg) * Math.PI) / 180;
        const inner = state.zoneMap.ring_edges[0] * unit;
        const outer = state.zoneMap.ring_edges[state.zoneMap.ring_edges.length - 1] * unit;
        ctx.beginPath();
        ctx.moveTo(cx + inner * Math.sin(angle), cy - inner * Math.cos(angle));
        ctx.lineTo(cx + outer * Math.sin(angle), cy - outer * Math.cos(angle));
        ctx.stroke();
      }
      if (state.showLabels) {
        ctx.fillStyle = "rgba(232, 241, 255, 0.5)";
        ctx.font = "10px sans-serif";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        const edges = state.zoneMap.ring_edges;
        for (let ring = 0; ring < edges.length; ring++) {
          const inner = ring > 0 ? edges[ring - 1] : 0;
          const radius = ((inner + edges[ring]) / 2) * unit;
          for (let s = 0; s < sectors; s++) {
            const angle = ((s + 0.5) * (360 / sectors) + state.zoneMap.sector_offset_deg) * (Math.PI / 180);
            const label = RING_LETTERS[RING_LETTERS.length - 1 - ring] + (s + 1);
            ctx.fillText(label, cx + radius * Math.sin(angle), cy - radius * Math.cos(angle));
          }
        }
      }
    }

    const box2 = contentBox();
    for (const contact of state.contacts.values()) {
      const px = box2.x + contact.nx * box2.w;
      const py = box2.y + contact.ny * box2.h;
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
      if (contact.zone) {
        ctx.fillStyle = "#e8f1ff";
        ctx.font = "bold 11px sans-serif";
        ctx.textAlign = "center";
        ctx.fillText(contact.zone.label, px, py - 22);
      }
    }

    requestAnimationFrame(drawOverlay);
  }

  // ----------------------------------------------------------------- panel
  function syncPanel() {
    const zm = state.zoneMap;
    document.getElementById("in-cx").value = zm.center[0];
    document.getElementById("in-cy").value = zm.center[1];
    document.getElementById("in-r0").value = zm.ring_edges[0];
    document.getElementById("in-r4").value = zm.ring_edges[zm.ring_edges.length - 1];
    document.getElementById("in-off").value = zm.sector_offset_deg;
    document.getElementById("chk-snap").checked = state.snap;
    document.getElementById("chk-grid").checked = state.showGrid;
    document.getElementById("chk-labels").checked = state.showLabels;
    document.getElementById("in-server").value = state.server;
  }

  function ringsFromEnds(inner, outer) {
    const steps = 4;
    const edges = [];
    for (let i = 0; i <= steps; i++) edges.push(inner + ((outer - inner) * i) / steps);
    return edges;
  }

  function bindPanel() {
    const onSlider = () => {
      const cx = parseFloat(document.getElementById("in-cx").value);
      const cy = parseFloat(document.getElementById("in-cy").value);
      const r0 = parseFloat(document.getElementById("in-r0").value);
      const r4 = Math.max(r0 + 0.02, parseFloat(document.getElementById("in-r4").value));
      const off = parseFloat(document.getElementById("in-off").value);
      state.zoneMap = { center: [cx, cy], ring_edges: ringsFromEnds(r0, r4), sectors: state.zoneMap.sectors, sector_offset_deg: off };
      store.set("zoneMap", state.zoneMap);
    };
    ["in-cx", "in-cy", "in-r0", "in-r4", "in-off"].forEach((id) => {
      document.getElementById(id).type = "range";
      document.getElementById(id).addEventListener("input", onSlider);
    });
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
    document.getElementById("btn-apply").addEventListener("click", () => {
      send({ t: "zone_map", zone_map: state.zoneMap });
      setBanner("分区已同步到 PC");
      setTimeout(() => setBanner(null), 900);
    });
    document.getElementById("btn-reset").addEventListener("click", () => {
      state.zoneMap = Object.assign({}, DEFAULTS);
      store.set("zoneMap", state.zoneMap);
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
      hudNet.textContent = (state.ws && state.ws.readyState === WebSocket.OPEN ? "已连接" : "未连接") + " · " + state.server;
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
    document.getElementById("in-server").value = state.server;
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
