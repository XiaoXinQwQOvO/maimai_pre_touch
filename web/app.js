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
 * where r is measured in units of the panel radius, 0 deg points to screen-right
 * and grows clockwise (8 sectors of 45 deg).
 *
 * The panel is *not* the whole picture: the game draws it as a circle on its
 * waiting screen (lower-left corner), the PC detects that circle and sends its
 * centre and radii (cx, cy, rx, ry - fractions of the video frame).  Using the
 * detected circle keeps this overlay exactly on top of the game's own circle even
 * when the picture is stretched or letterboxed.
 *
 * Coordinates sent to the PC are normalised to the video content box (the game
 * client area), so phone resolution, orientation and letterboxing do not matter.
 */
(() => {
  "use strict";

  const PANEL_DEFAULT = {
    cx: 0.5,
    cy: 0.5,
    rx: 0.5,
    ry: 0.5,
    r_centre: 0.2241,
    r_inner: 0.5518,
    r_max: 1.0,
    sectors: 8,
    detected: false,
    state: "full",
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
    panel: Object.assign({}, PANEL_DEFAULT, store.get("panel", {})),
    manual: store.get("manual", false),
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
  // Frame fractions -> panel units (0,0 = panel centre, 1 = rim).
  function toPanel(nx, ny, model) {
    return [(nx - model.cx) / model.rx, (ny - model.cy) / model.ry];
  }

  function fromPanel(u, v, model) {
    return [model.cx + u * model.rx, model.cy + v * model.ry];
  }

  function sensorAt(nx, ny, model) {
    const [u, v] = toPanel(nx, ny, model);
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
    if (!sensor || sensor.name === "C") return [model.cx, model.cy];
    const radius = sensor.name.startsWith("I")
      ? (model.r_centre + model.r_inner) / 2
      : (model.r_inner + model.r_max) / 2;
    const angle = ((sensor.sector + 0.5) * (360 / model.sectors) * Math.PI) / 180;
    return fromPanel(radius * Math.cos(angle), radius * Math.sin(angle), model);
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
        if (msg.panel) applyPanel(msg.panel);
        if (msg.touch_backend) {
          state.touchBackend = msg.touch_backend;
          hudNet.textContent = "已连接 · " + state.server + " · " + state.touchBackend;
        }
        break;
      case "panel":
        applyPanel(msg);
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

  // The receiver's jitter buffer is what makes the picture lag behind reality: Chrome
  // starts at ~200 ms and only grows when packets are late.  Ask for the minimum.
  function tuneReceiver(pc) {
    if (!pc || !pc.getReceivers) return;
    try {
      pc.getReceivers().forEach((rx) => {
        if (!rx) return;
        if ("jitterBufferTarget" in rx) rx.jitterBufferTarget = 0;
        if ("playoutDelayHint" in rx) rx.playoutDelayHint = 0;
      });
    } catch (_) {}
  }

  // Live numbers from the receive side: what the phone actually decodes, plus the
  // bitrate/loss/RTT of the link - so "卡" can be told apart from "网络差".
  let statsTimer = null;
  let lastStats = null;
  function startVideoStats() {
    stopVideoStats();
    lastStats = null;
    statsTimer = setInterval(async () => {
      const pc = state.pc;
      if (!pc || !pc.getStats) return;
      let report;
      try {
        report = await pc.getStats();
      } catch (_) {
        return;
      }
      const now = performance.now();
      let fps = null;
      let kbps = null;
      let jitterMs = null;
      let lostPct = null;
      let rttMs = null;
      report.forEach((s) => {
        if (s.type === "inbound-rtp" && (s.kind === "video" || s.mediaType === "video")) {
          if (typeof s.framesPerSecond === "number") fps = s.framesPerSecond;
          if (typeof s.jitter === "number") jitterMs = s.jitter * 1000;
          if (typeof s.packetsReceived === "number" && typeof s.packetsLost === "number") {
            const total = s.packetsReceived + s.packetsLost;
            if (total > 0) lostPct = (s.packetsLost / total) * 100;
          }
          if (lastStats && typeof s.bytesReceived === "number") {
            const dt = (now - lastStats.at) / 1000;
            if (dt > 0.2) kbps = ((s.bytesReceived - lastStats.bytes) * 8) / dt / 1000;
          }
          if (typeof s.bytesReceived === "number") lastStats = { at: now, bytes: s.bytesReceived };
        }
        if (s.type === "candidate-pair" && (s.selected || s.nominated || s.state === "succeeded")) {
          if (typeof s.currentRoundTripTime === "number") rttMs = s.currentRoundTripTime * 1000;
        }
      });
      state.rxStats = { fps, kbps, jitterMs, lostPct, rttMs };
      send({
        t: "rx-stats",
        fps: fps === null ? null : Math.round(fps),
        kbps: kbps === null ? null : Math.round(kbps),
        jitter: jitterMs === null ? null : Math.round(jitterMs),
        lost: lostPct === null ? null : Number(lostPct.toFixed(2)),
        rtt: rttMs === null ? null : Math.round(rttMs),
      });
      if (hudVideo) {
        const parts = [];
        const vw = video.videoWidth;
        const vh = video.videoHeight;
        parts.push("视频：" + (vw ? vw + "×" + vh : "—") + " @ " + (fps === null ? "?" : fps.toFixed(0)) + "fps");
        if (kbps !== null) parts.push(kbps.toFixed(0) + "kbps");
        if (jitterMs !== null) parts.push("抖动 " + jitterMs.toFixed(0) + "ms");
        if (lostPct !== null) parts.push("丢包 " + lostPct.toFixed(1) + "%");
        if (rttMs !== null) parts.push("RTT " + rttMs.toFixed(0) + "ms");
        hudVideo.textContent = parts.join(" · ");
      }
    }, 1000);
  }
  function stopVideoStats() {
    if (statsTimer) {
      clearInterval(statsTimer);
      statsTimer = null;
    }
  }

  async function startPeer() {
    stopPeer();
    const pc = new RTCPeerConnection({ iceServers: [], bundlePolicy: "max-bundle" });
    state.pc = pc;
    const transceiver = pc.addTransceiver("video", { direction: "recvonly" });
    tuneReceiver(pc);
    // Prefer H.264: the PC encodes with NVENC.
    try {
      const caps = RTCRtpSender.getCapabilities("video");
      if (caps && caps.codecs && transceiver.setCodecPreferences) {
        const isH264 = (c) => (c.mimeType || "").toLowerCase() === "video/h264";
        transceiver.setCodecPreferences(caps.codecs.filter(isH264).concat(caps.codecs.filter((c) => !isH264(c))));
      }
    } catch (_) {}
    pc.ontrack = (event) => {
      tuneReceiver(pc);
      startVideoStats();
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
    stopVideoStats();
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
    if (adjust.active) return adjustDown(event);
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
    if (adjust.active) return adjustMove(event);
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
    if (adjust.active) return adjustUp(event);
    const contact = state.contacts.get(event.pointerId);
    if (!contact) return;
    event.preventDefault();
    emitTouch(contact, "up");
    state.contacts.delete(event.pointerId);
    hudZone.textContent = "—";
  }

  // ------------------------------------------------- ring adjustment mode
  // Drag on the picture to move the ring, pinch (two fingers) to resize it.  No touch
  // is sent to the game while adjusting, so a wrong ring never presses anything.
  const adjust = { active: false, points: new Map(), snapshot: null, startPoints: null };

  function setAdjust(on) {
    adjust.active = Boolean(on);
    adjust.points = new Map();
    adjust.snapshot = null;
    adjust.startPoints = null;
    document.body.classList.toggle("adjusting", adjust.active);
    setBanner(adjust.active ? "拖动移动圆环、双指缩放，对齐后点「保存到 PC」" : null);
  }

  function adjustDown(event) {
    event.preventDefault();
    event.stopPropagation();
    stage.setPointerCapture(event.pointerId);
    adjust.points.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (!adjust.snapshot) {
      adjust.snapshot = Object.assign({}, state.panel);
      adjust.startPoints = new Map(adjust.points);
    }
  }

  function adjustMove(event) {
    if (!adjust.points.has(event.pointerId)) return;
    event.preventDefault();
    adjust.points.set(event.pointerId, { x: event.clientX, y: event.clientY });
    const snap = adjust.snapshot;
    if (!snap) return;
    const box = contentBox();
    const start = Array.from(adjust.startPoints.values());
    const now = Array.from(adjust.points.values());
    let next;
    if (start.length >= 2 && now.length >= 2) {
      const d0 = Math.hypot(start[0].x - start[1].x, start[0].y - start[1].y);
      const d1 = Math.hypot(now[0].x - now[1].x, now[0].y - now[1].y);
      const scale = d0 > 8 ? d1 / d0 : 1;
      const m0 = { x: (start[0].x + start[1].x) / 2, y: (start[0].y + start[1].y) / 2 };
      const m1 = { x: (now[0].x + now[1].x) / 2, y: (now[0].y + now[1].y) / 2 };
      next = {
        cx: snap.cx + (m1.x - m0.x) / box.w,
        cy: snap.cy + (m1.y - m0.y) / box.h,
        rx: snap.rx * scale,
        ry: snap.ry * scale,
      };
    } else {
      next = {
        cx: snap.cx + (now[0].x - start[0].x) / box.w,
        cy: snap.cy + (now[0].y - start[0].y) / box.h,
        rx: snap.rx,
        ry: snap.ry,
      };
    }
    next.rx = Math.min(1.5, Math.max(0.02, next.rx));
    next.ry = Math.min(1.5, Math.max(0.02, next.ry));
    state.manual = true;
    applyPanelLocal(Object.assign({}, state.panel, next));
  }

  function adjustUp(event) {
    if (adjust.points.has(event.pointerId)) adjust.points.delete(event.pointerId);
    if (adjust.points.size === 0) {
      if (adjust.snapshot) store.set("panel", state.panel);
      adjust.snapshot = null;
      adjust.startPoints = null;
    }
  }

  stage.addEventListener("pointerdown", onPointerDown, { passive: false });
  stage.addEventListener("pointermove", onPointerMove, { passive: false });
  stage.addEventListener("pointerup", onPointerUp, { passive: false });
  stage.addEventListener("pointercancel", onPointerUp, { passive: false });
  stage.addEventListener("contextmenu", (e) => e.preventDefault());
  document.addEventListener("gesturestart", (e) => e.preventDefault());

  // ------------------------------------------------- detected panel circle
  const PANEL_STATE_TEXT = {
    full: "整幅画面（未检测到圆）",
    locked: "已锁定游戏内的圆",
    manual: "手动设置",
    searching: "检测中…",
    lost: "重新检测中…",
    dark: "等待游戏画面（黑屏）",
    starting: "启动中…",
  };

  function applyPanel(incoming) {
    const model = Object.assign({}, PANEL_DEFAULT, incoming || {});
    // guard against a broken message: rx/ry must be sane fractions
    if (!(model.rx > 0.01 && model.rx <= 1.5) || !(model.ry > 0.01 && model.ry <= 1.5)) {
      return;
    }
    // a local fine-tune wins over the PC's auto detection (until "跟随自动检测")
    if (state.manual) {
      state.panel = Object.assign({}, model, {
        cx: state.panel.cx,
        cy: state.panel.cy,
        rx: state.panel.rx,
        ry: state.panel.ry,
        state: "manual",
        detected: true,
      });
    } else {
      state.panel = model;
    }
    syncPanel();
  }

  function applyPanelLocal(model) {
    state.panel = Object.assign({}, model, { state: "manual", detected: true });
    syncPanel();
  }

  function panelSummary() {
    const model = state.panel;
    const stateText = PANEL_STATE_TEXT[model.state] || model.state || "";
    if (!model.detected) return "面板：" + stateText;
    const ax = (model.rx * 100).toFixed(1);
    const ay = (model.ry * 100).toFixed(1);
    const stretch = (model.rx / model.ry).toFixed(3);
    return "面板：" + stateText + " · 圆心 " + (model.cx * 100).toFixed(1) + "% / " + (model.cy * 100).toFixed(1) +
      "% · 半径 " + ax + "% / " + ay + "% · 拉伸 " + stretch;
  }

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
    const model = state.panel;
    // the panel circle in canvas pixels; rx/ry keep it on the game's circle even
    // when the picture is stretched (they come from the detected circle)
    const cx = box.x + model.cx * box.w;
    const cy = box.y + model.cy * box.h;
    const rx = model.rx * box.w;
    const ry = model.ry * box.h;

    const ringPath = (radius) => {
      ctx.beginPath();
      ctx.ellipse(cx, cy, Math.max(1, radius * rx), Math.max(1, radius * ry), 0, 0, Math.PI * 2);
    };

    if (state.showGrid) {
      ctx.strokeStyle = model.detected ? "rgba(79, 209, 255, 0.60)" : "rgba(79, 209, 255, 0.30)";
      ctx.lineWidth = model.detected ? 2 : 1;
      ringPath(model.r_max);
      ctx.stroke();
      ctx.strokeStyle = "rgba(79, 209, 255, 0.30)";
      ctx.lineWidth = 1;
      for (const radius of [model.r_centre, model.r_inner]) {
        ringPath(radius);
        ctx.stroke();
      }
      const sectors = model.sectors;
      for (let i = 0; i < sectors; i++) {
        const angle = (i * (360 / sectors) * Math.PI) / 180;
        ctx.beginPath();
        ctx.moveTo(cx + model.r_centre * rx * Math.cos(angle), cy + model.r_centre * ry * Math.sin(angle));
        ctx.lineTo(cx + model.r_max * rx * Math.cos(angle), cy + model.r_max * ry * Math.sin(angle));
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
          const innerR = (model.r_centre + model.r_inner) / 2;
          const outerR = (model.r_inner + model.r_max) / 2;
          ctx.fillText("B" + (s + 1), cx + innerR * rx * Math.cos(angle), cy + innerR * ry * Math.sin(angle));
          ctx.fillText("A" + (s + 1), cx + outerR * rx * Math.cos(angle), cy + outerR * ry * Math.sin(angle));
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
    const auto = document.getElementById("chk-auto");
    const model = state.panel;
    if (auto) auto.checked = Boolean(model.detected) && !state.manual;
    for (const [key, id] of [
      ["cx", "cx"],
      ["cy", "cy"],
      ["rx", "rx"],
      ["ry", "ry"],
    ]) {
      const input = document.getElementById("in-" + id);
      const output = document.getElementById("out-" + id);
      if (input) input.value = String(model[key]);
      if (output) output.textContent = (model[key] * 100).toFixed(1) + "%";
    }
    const hudModel = document.getElementById("hud-model");
    if (hudModel) hudModel.textContent = panelSummary();
  }

  function bindSliders() {
    let queued = false;
    const apply = (key, value) => {
      const model = Object.assign({}, state.panel, { [key]: value });
      state.manual = true;
      applyPanelLocal(model);
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => {
        queued = false;
        store.set("panel", state.panel);
      });
    };
    for (const key of ["cx", "cy", "rx", "ry"]) {
      const input = document.getElementById("in-" + key);
      if (!input) continue;
      input.addEventListener("input", () => apply(key, Number(input.value)));
      input.addEventListener("change", () => syncPanel());
    }
    document.getElementById("chk-auto").addEventListener("change", (event) => {
      state.manual = !event.target.checked;
      store.set("manual", state.manual);
      if (!state.manual) {
        // hand control back: the PC keeps pushing the detected circle
        store.set("panel", {});
        send({ t: "panel-auto" });
      }
      syncPanel();
    });
    document.getElementById("chk-adjust").addEventListener("change", (event) => {
      setAdjust(event.target.checked);
      if (adjust.active) {
        document.getElementById("panel").classList.add("hidden");
      }
    });
    document.getElementById("btn-save-panel").addEventListener("click", async () => {
      const model = state.panel;
      const body = { cx: model.cx, cy: model.cy, rx: model.rx, ry: model.ry };
      try {
        const response = await fetch("http://" + state.server.replace(/^https?:\/\//, "") + "/api/panel", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(Object.assign({ save: true }, body)),
        });
        const result = await response.json();
        setBanner(result.ok ? "已保存到 PC" : "保存失败：" + (result.error || "?"));
        if (result.ok) setTimeout(() => setBanner(null), 1200);
      } catch (err) {
        setBanner("保存失败：" + (err && err.message ? err.message : err));
      }
    });
    document.getElementById("btn-reload-panel").addEventListener("click", async () => {
      try {
        const response = await fetch("http://" + state.server.replace(/^https?:\/\//, "") + "/api/panel");
        applyPanel(await response.json());
        state.manual = false;
        store.set("panel", {});
        store.set("manual", false);
        syncPanel();
      } catch (err) {
        setBanner("读取失败：" + (err && err.message ? err.message : err));
      }
    });
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
      // back to whatever the PC detected (or the whole frame if nothing was found)
      state.manual = false;
      store.set("manual", false);
      store.set("panel", {});
      send({ t: "panel-auto" });
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
    bindSliders();
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
