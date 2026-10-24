"""maimai_pre_touch PC server: capture the game window, stream it over WebRTC and
apply the touch events that the phone sends back.

Run:  python pc/server.py [--port 8770] [--touch maitouch|log|synth] [--region L,T,W,H]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import sys
import threading
import time

from aiohttp import WSMsgType, web

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WEB_DIR = os.path.join(ROOT, "web")
sys.path.insert(0, HERE)

import config as config_mod  # noqa: E402
import window as window_mod  # noqa: E402
import sensors  # noqa: E402
from capture import CaptureManager  # noqa: E402
from touch_backends import create_backend  # noqa: E402
import video as video_mod  # noqa: E402
from video import GameVideoTrack, install_encoder  # noqa: E402

log = logging.getLogger("server")


def lan_ip() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception:
        return "127.0.0.1"


def panel_geometry(panel=None, detected: bool = False, state: str = "full") -> dict:
    """The sensor layout + panel circle the phone should draw (see pc/sensors.py)."""
    geometry = sensors.panel_geometry(panel)
    geometry.update({"detected": detected, "state": state})
    return geometry


class PanelWatcher(threading.Thread):
    """Watches the captured picture for the panel circle the game draws.

    The circle appears on the waiting screen that follows the boot black screen, so
    detection waits for the picture to get bright, then locks the circle.  While it is
    locked that circle - not the whole frame - is the touch geometry.
    """

    def __init__(self, capture, cfg: dict):
        super().__init__(name="panel-watcher", daemon=True)
        self.capture = capture
        self.cfg = dict(cfg or {})
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._manual = self.cfg.get("manual")
        self._tracker = None
        self._panel: dict | None = None
        self.misses = 0

    # --- lifecycle ---------------------------------------------------------

    def run(self) -> None:
        import panel_detect

        interval = float(self.cfg.get("detect_interval", 0.5))
        self._tracker = panel_detect.PanelTracker(
            dark_threshold=float(self.cfg.get("dark_threshold", 24.0)),
            lock_frames=int(self.cfg.get("lock_frames", 3)),
            tolerance=float(self.cfg.get("tolerance", 0.04)),
            verify_interval=float(self.cfg.get("verify_interval", 2.0)),
            roi=tuple(self.cfg.get("roi") or (0.0, 0.30, 0.55, 1.0)),
            log=log,
        )
        if self.cfg.get("auto", True) is False:
            log.info("panel: auto detection disabled, using %s", self._manual or "the whole frame")
            return
        while not self._stop.is_set():
            frame = None
            try:
                frame = self.capture.get_frame()
            except Exception as exc:  # noqa: BLE001 - never kill the watcher
                log.debug("panel: capture failed: %s", exc)
            if frame is None:
                self._stop.wait(interval)
                continue
            if frame.ndim == 3 and frame.shape[2] == 4:
                frame = frame[:, :, :3]
            try:
                status = self._tracker.update(frame)
            except Exception as exc:  # noqa: BLE001
                log.warning("panel: detection failed: %s", exc)
                self._stop.wait(interval)
                continue
            with self._lock:
                self._panel = self._tracker.panel()
            self._stop.wait(interval)

    def stop(self) -> None:
        self._stop.set()

    # --- manual override ---------------------------------------------------

    def set_manual(self, panel: dict | None) -> None:
        """Pin the panel geometry (or drop the pin and go back to auto detection)."""
        with self._lock:
            self._manual = dict(panel) if panel else None
        if panel:
            log.info(
                "panel: manual geometry cx=%.4f cy=%.4f rx=%.4f ry=%.4f",
                panel["cx"],
                panel["cy"],
                panel["rx"],
                panel["ry"],
            )
        else:
            log.info("panel: manual geometry cleared, back to auto detection")

    # --- queries -----------------------------------------------------------

    def effective(self) -> tuple[dict | None, str]:
        """(panel or None, state) - manual override wins, then the locked circle."""
        if self._manual:
            return dict(self._manual), "manual"
        with self._lock:
            panel = dict(self._panel) if self._panel else None
        if panel is not None:
            return panel, "locked"
        state = getattr(self._tracker, "state", "starting")
        return None, state

    def status(self) -> dict:
        panel, state = self.effective()
        tracker_state = getattr(self._tracker, "state", "starting") if self._tracker else "starting"
        return {
            "auto": bool(self.cfg.get("auto", True)),
            "state": state,
            "tracker_state": tracker_state,
            "panel": panel,
        }


class GameState:
    """Tracks the game window (rect) and owns capture + touch backends."""

    def __init__(self, cfg: dict, region_override=None, follow_window: bool = True):
        self.cfg = cfg
        self.follow_window = follow_window
        self._region_override = tuple(region_override) if region_override else None
        self._client_rect: tuple[int, int, int, int] | None = self._region_override
        self._win = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.rx_stats: dict = {}
        self.window_title = ""
        self.window_missing_since = 0.0

        touch_cfg = dict(cfg.get("touch", {}))
        touch_cfg.setdefault("process_names", tuple(cfg.get("capture", {}).get("process_names", ())))
        self.capture = CaptureManager(cfg.get("capture", {}), self.region_for_capture)
        self.panel_watcher = PanelWatcher(self.capture, cfg.get("panel", {}))
        self.touch = create_backend(touch_cfg, self.client_rect, panel_provider=self.panel)
        self.tracker = threading.Thread(target=self._track_loop, name="window-tracker", daemon=True)

    # --- window tracking ---------------------------------------------------

    def start(self) -> None:
        self.tracker.start()
        self.panel_watcher.start()

    def stop(self) -> None:
        self._stop.set()
        self.panel_watcher.stop()
        self.touch.close()
        self.capture.close()

    def _track_loop(self) -> None:
        capture_cfg = self.cfg.get("capture", {})
        names = tuple(capture_cfg.get("process_names", ("maimai.exe",)))
        cls = capture_cfg.get("window_class_substr", "teaGfx")
        while not self._stop.is_set():
            if self.follow_window:
                win = window_mod.find_game_window(names, cls)
                with self._lock:
                    self._win = win
                    if win:
                        self.window_title = f"{win['title']} ({os.path.basename(win['exe'])})"
                        self._client_rect = window_mod.client_rect(win)
                        self.window_missing_since = 0.0
                    elif self._region_override:
                        self._client_rect = self._region_override
                    else:
                        self._client_rect = None
                        if not self.window_missing_since:
                            self.window_missing_since = time.time()
            self._stop.wait(0.5)

    # --- providers ---------------------------------------------------------

    def client_rect(self):
        with self._lock:
            return self._client_rect

    def region_for_capture(self):
        rect = self.client_rect()
        if rect is None:
            return None
        x, y, w, h = rect
        if w <= 1 or h <= 1:
            return None
        return (int(x), int(y), int(w), int(h))

    def status(self) -> dict:
        rect = self.client_rect()
        panel, panel_state = self.panel_watcher.effective()
        return {
            "window": self.window_title,
            "window_present": bool(self._win),
            "client_rect": rect,
            "capture_backend": getattr(self.capture._backend, "name", None),
            "frames_captured": self.capture.frames,
            "touch": dict(self.touch.stats),
            "phone": self.rx_stats,
            "touch_backend": self.touch.describe(),
            "touch_details": self.touch.details(),
            "panel": panel_geometry(panel, detected=bool(panel) and panel_state in ("locked", "manual"), state=panel_state),
            "panel_detect": self.panel_watcher.status(),
        }

    def set_rx_stats(self, data: dict) -> None:
        """Remember what the phone reports about its own video reception."""

        def number(key):
            try:
                return round(float(data.get(key)), 2)
            except (TypeError, ValueError):
                return None

        self.rx_stats = {
            "fps": number("fps"),
            "kbps": number("kbps"),
            "jitter_ms": number("jitter"),
            "loss_pct": number("lost"),
            "rtt_ms": number("rtt"),
        }

    def _adapt_bitrate(self) -> None:
        """Follow the phone's own measurements, like Sunshine's adaptive bitrate.

        The encoder target starts at the configured maximum; a clean link is probed
        upwards slowly and a dirty one is backed off quickly, so the picture keeps
        its resolution and the latency stays bounded.
        """
        stats = self.state.rx_stats or {}
        fps = stats.get("fps") or 0
        loss = stats.get("loss_pct")
        jitter = stats.get("jitter_ms") or 0
        rtt = stats.get("rtt_ms") or 0
        now = time.time()
        if now - self._bitrate_change_at < 2.0:
            return
        current = video_mod.stream_bitrate_kbps()
        clean = (loss is None or loss < 0.5) and jitter < 30 and rtt < 80 and fps >= 50
        bad = (loss or 0) > 1.0 or jitter > 80 or rtt > 200 or fps < 40
        if clean:
            target = min(current * 1.2 + 250, self.bitrate_max)
        elif bad:
            target = max(current * 0.7, self.bitrate_min)
        else:
            return
        if abs(target - current) <= 0.05 * max(1.0, current):
            return
        applied = video_mod.set_stream_bitrate(target)
        self._bitrate_change_at = now
        log.info(
            "video: bitrate %d -> %d kbps (phone: %s fps, loss %s%%, jitter %sms, rtt %sms)",
            int(current),
            applied // 1000,
            fps,
            loss,
            jitter,
            rtt,
        )

    def set_phone_count(self, count: int) -> None:
        """Tell the touch backend whether somebody is playing through the phone."""
        setter = getattr(self.touch, "set_connected", None)
        if setter is not None:
            try:
                setter(count > 0)
            except Exception as exc:  # noqa: BLE001
                log.warning("touch backend set_connected failed: %s", exc)

    def panel(self) -> tuple[dict | None, str]:
        return self.panel_watcher.effective()


class App:
    def __init__(self, cfg: dict, state: GameState):
        self.cfg = cfg
        self.state = state
        self.peers: set = set()
        self.clients = 0
        video_cfg = cfg.get("video", {})
        video_mod.set_bitrate_limits(
            max_kbps=int(video_cfg.get("bitrate_kbps", 12000)),
            min_kbps=video_cfg.get("min_bitrate_kbps"),
        )
        self.bitrate_max = int(video_cfg.get("bitrate_kbps", 12000))
        self.bitrate_min = int(
            video_cfg.get("min_bitrate_kbps") or max(1500, self.bitrate_max // 4)
        )
        self._bitrate_change_at = 0.0
        self.video_track = GameVideoTrack(
            state.capture,
            fps=int(video_cfg.get("fps", 60)),
            scale=float(video_cfg.get("scale", 1.0)),
            bitrate_kbps=int(video_cfg.get("bitrate_kbps", 9000)),
        )
        self._started = time.time()

    # --- HTTP routes -------------------------------------------------------

    async def index(self, request: web.Request):
        return web.FileResponse(os.path.join(WEB_DIR, "index.html"))

    async def status(self, request: web.Request):
        payload = {
            "uptime": round(time.time() - self._started, 1),
            "clients": self.clients,
            "video": self.video_track.stats,
            "game": self.state.status(),
        }
        return web.json_response(payload)

    async def healthz(self, request: web.Request):
        return web.json_response({"ok": True})

    async def panel(self, request: web.Request):
        panel, state = self.state.panel()
        return web.json_response(
            {"ok": True, **panel_geometry(panel, detected=bool(panel) and state in ("locked", "manual"), state=state)}
        )

    async def panel_set(self, request: web.Request):
        """Pin (POST geometry) or release ({"auto": true}) the panel circle."""
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"ok": False, "error": "expected JSON"}, status=400)

        if data.get("auto"):
            self.state.panel_watcher.set_manual(None)
            if data.get("save"):
                self._persist_panel(None)
            panel, state = self.state.panel()
            return web.json_response(
                {"ok": True, **panel_geometry(panel, detected=bool(panel) and state in ("locked", "manual"), state=state)}
            )

        try:
            panel = {key: float(data[key]) for key in ("cx", "cy", "rx", "ry")}
        except (KeyError, TypeError, ValueError):
            return web.json_response({"ok": False, "error": "cx, cy, rx, ry required"}, status=400)
        if not (-0.5 <= panel["cx"] <= 1.5 and -0.5 <= panel["cy"] <= 1.5):
            return web.json_response({"ok": False, "error": "centre out of range"}, status=400)
        if not (0.01 <= panel["rx"] <= 1.5 and 0.01 <= panel["ry"] <= 1.5):
            return web.json_response({"ok": False, "error": "radius out of range"}, status=400)

        self.state.panel_watcher.set_manual(panel)
        if data.get("save"):
            self._persist_panel(panel)
        current, state = self.state.panel()
        return web.json_response(
            {"ok": True, **panel_geometry(current, detected=True, state=state)}
        )

    def _persist_panel(self, panel: dict | None) -> None:
        path = getattr(self.state, "cfg_path", config_mod.CONFIG_PATH)
        try:
            cfg = config_mod.load(path)
            cfg.setdefault("panel", {})["manual"] = panel
            cfg["panel"]["auto"] = panel is None
            config_mod.save(cfg, path)
            log.info("panel: %s saved to %s", "manual geometry" if panel else "auto detection", path)
        except Exception as exc:  # noqa: BLE001 - never fail the request because of disk
            log.warning("panel: could not save to %s: %s", path, exc)

    # --- WebSocket ---------------------------------------------------------

    async def ws(self, request: web.Request):
        from aiortc import RTCPeerConnection, RTCSessionDescription
        from aiortc.codecs import get_capabilities

        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)
        self.clients += 1
        peer = None
        log.info("phone connected from %s", request.remote)

        async def send(payload: dict):
            if not ws.closed:
                await ws.send_str(json.dumps(payload))

        panel, panel_state = self.state.panel()
        await send(
            {
                "t": "welcome",
                "server": "maimai_pre_touch",
                "version": 1,
                "panel": panel_geometry(panel, detected=bool(panel) and panel_state in ("locked", "manual"), state=panel_state),
                "window": self.state.status()["client_rect"],
                "touch_backend": self.state.touch.describe(),
                "video": {
                    "codec": "H264",
                    "fps": self.cfg.get("video", {}).get("fps", 60),
                    "bitrate_kbps": self.cfg.get("video", {}).get("bitrate_kbps", 9000),
                },
            }
        )

        async def push_panel():
            """Tell the phone as soon as the panel circle is (re)locked or lost."""
            last = panel
            while True:
                current, current_state = self.state.panel()
                if current != last:
                    last = current
                    await send(
                        {
                            "t": "panel",
                            **panel_geometry(
                                current,
                                detected=bool(current) and current_state in ("locked", "manual"),
                                state=current_state,
                            ),
                        }
                    )
                await asyncio.sleep(0.5)

        panel_task = asyncio.ensure_future(push_panel())

        active: set[int] = set()
        try:
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(message.data)
                    except ValueError:
                        continue
                    kind = data.get("t")
                    if kind == "ping":
                        await send({"t": "pong", "ts": data.get("ts"), "server_ts": time.time() * 1000})
                    elif kind == "offer":
                        if peer is not None:
                            await peer.close()
                        peer = RTCPeerConnection()
                        self.peers.add(peer)
                        self.state.set_phone_count(len(self.peers))
                        peer.addTrack(self.video_track)
                        try:
                            capabilities = get_capabilities("video")
                            caps = getattr(capabilities, "codecs", None) or capabilities["codecs"]
                            h264 = [c for c in caps if c.name.upper() == "H264"]
                            vp8 = [c for c in caps if c.name.upper() == "VP8"]
                            if h264:
                                peer.getTransceivers()[0].setCodecPreferences(h264 + vp8)
                                log.info("codec preference: H264 (%d profiles) then VP8", len(h264))
                        except Exception as exc:
                            log.warning("codec preference failed: %s", exc)

                        @peer.on("connectionstatechange")
                        async def on_state():
                            log.info("peer state: %s", peer.connectionState)

                        await peer.setRemoteDescription(RTCSessionDescription(sdp=data["sdp"], type=data["type"]))
                        answer = await peer.createAnswer()
                        await peer.setLocalDescription(answer)
                        for _ in range(40):
                            if peer.iceGatheringState == "complete":
                                break
                            await asyncio.sleep(0.05)
                        sdp = peer.localDescription.sdp
                        mline = next((line for line in sdp.splitlines() if line.startswith("m=video")), "")
                        log.info("answer video line: %s", mline)
                        first_pt = None
                        if mline:
                            fields = mline.split()
                            if len(fields) >= 4:
                                first_pt = fields[3]
                        for line in sdp.splitlines():
                            if first_pt and line.startswith(f"a=rtpmap:{first_pt} "):
                                log.info("negotiated video codec: %s (pt %s)", line.split(" ", 1)[1], first_pt)
                                break
                        await send({"t": "answer", "sdp": sdp, "type": peer.localDescription.type})
                    elif kind == "touch":
                        self._handle_touch(data, active)
                    elif kind == "panel-auto":
                        self.state.panel_watcher.set_manual(None)
                    elif kind == "stats":
                        await send({"t": "stats", "video": self.video_track.stats, "game": self.state.status()})
                    elif kind == "rx-stats":
                        # what the phone says it receives/decodes - the only place the
                        # link quality is visible from this side
                        self.state.set_rx_stats(data)
                        self._adapt_bitrate()
                        log.info(
                            "phone: %s fps, %s kbps, jitter %s ms, loss %s %%, rtt %s ms",
                            data.get("fps"),
                            data.get("kbps"),
                            data.get("jitter"),
                            data.get("lost"),
                            data.get("rtt"),
                        )
                elif message.type == WSMsgType.ERROR:
                    break
        finally:
            panel_task.cancel()
            if active:
                # never leave a finger pressed when the phone goes away
                for contact_id in list(active):
                    try:
                        self.state.touch.release(contact_id)
                    except Exception:  # noqa: BLE001
                        pass
                log.info("released %d touch contact(s) held by the disconnected phone", len(active))
            self.clients -= 1
            if peer is not None:
                await peer.close()
                self.peers.discard(peer)
            self.state.set_phone_count(len(self.peers))
            log.info("phone disconnected")

        return ws

    def _handle_touch(self, data: dict, held: set) -> None:
        """Apply one touch message, tracking the contacts this connection holds."""
        action = data.get("a")
        contact_id = int(data.get("id", 1))
        nx = float(data.get("x", 0.0))
        ny = float(data.get("y", 0.0))
        snap = bool(data.get("snap", False))
        panel, _ = self.state.panel()
        if snap:
            nx, ny = sensors.snap_to_sensor(nx, ny, panel)
        try:
            if action == "down":
                self.state.touch.press(contact_id, nx, ny)
                held.add(contact_id)
            elif action == "move":
                self.state.touch.move(contact_id, nx, ny)
                held.add(contact_id)
            elif action == "up":
                self.state.touch.release(contact_id)
                held.discard(contact_id)
        except Exception as exc:  # never let a bad touch kill the connection
            log.warning("touch handling failed: %s", exc)


def make_app(cfg: dict, state: GameState) -> web.Application:
    app_impl = App(cfg, state)
    app = web.Application()
    app["impl"] = app_impl
    app.router.add_get("/", app_impl.index)
    app.router.add_get("/ws", app_impl.ws)
    app.router.add_get("/api/status", app_impl.status)
    app.router.add_get("/healthz", app_impl.healthz)
    app.router.add_get("/api/panel", app_impl.panel)
    app.router.add_post("/api/panel", app_impl.panel_set)
    if os.path.isdir(WEB_DIR):
        # the phone app opens ``http://<pc>:8770/app/``: register the directory itself
        # before the static handler, which otherwise answers 403 (no directory listing)
        app.router.add_get("/app", app_impl.index)
        app.router.add_get("/app/", app_impl.index)
        app.router.add_static("/app/", WEB_DIR, show_index=False)
    return app


async def run(args) -> None:
    cfg = config_mod.load(args.config)
    if args.port:
        cfg["http"]["port"] = args.port
    if args.touch:
        cfg["touch"]["backend"] = args.touch
    if args.bitrate:
        cfg["video"]["bitrate_kbps"] = args.bitrate
    if args.fps:
        cfg["video"]["fps"] = args.fps
        cfg["capture"]["fps"] = args.fps

    window_mod.set_dpi_aware()
    install_encoder(cfg.get("video", {}).get("encoder", "auto"))

    follow = not args.no_follow_window
    state = GameState(cfg, region_override=args.region, follow_window=follow)
    state.cfg_path = args.config or config_mod.CONFIG_PATH
    state.start()
    log.info("panel: %d sensors (centre %.4f, inner ring %.4f)", len(sensors.SENSORS), sensors.R_CENTRE, sensors.R_INNER)
    panel_cfg = cfg.get("panel", {})
    if panel_cfg.get("manual"):
        log.info("panel: manual geometry %s", panel_cfg["manual"])
    elif panel_cfg.get("auto", True):
        log.info("panel: waiting for the game's panel circle (bottom-left of the picture)")
    else:
        log.info("panel: auto detection off, the whole picture is the panel")
    log.info("touch backend: %s", state.touch.describe())

    app = make_app(cfg, state)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, cfg["http"]["host"], int(cfg["http"]["port"]))
    await site.start()
    host = lan_ip()
    port = cfg["http"]["port"]
    log.info("=" * 62)
    log.info(" 打开手机浏览器 / APK 指向:  http://%s:%s/", host, port)
    log.info(" 状态接口:                   http://%s:%s/api/status", host, port)
    log.info("=" * 62)

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()
        state.stop()


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="maimai_pre_touch PC server")
    parser.add_argument("--config", default=None, help="path to config.json")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--touch", choices=["synth", "log", "maitouch"], default=None)
    parser.add_argument("--bitrate", type=int, default=None, help="video bitrate in kbps")
    parser.add_argument("--fps", type=int, default=None)
    parser.add_argument("--region", default=None, help="capture region L,T,W,H (debug, no window follow)")
    parser.add_argument("--no-follow-window", action="store_true")
    parser.add_argument("--log-level", default=None)
    args = parser.parse_args(argv)

    cfg = config_mod.load(args.config)
    level = (args.log_level or cfg.get("debug", {}).get("log_level", "INFO")).upper()
    logging.basicConfig(level=getattr(logging, level, logging.INFO), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    for noisy in ("aioice", "aiortc", "dxcam"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.region:
        left, top, width, height = (int(v) for v in args.region.split(","))
        args.region = (left, top, width, height)

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
