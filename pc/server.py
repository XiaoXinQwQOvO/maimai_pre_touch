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


def panel_geometry() -> dict:
    """The sensor layout the phone should draw (see pc/sensors.py)."""
    return {
        "r_centre": sensors.R_CENTRE,
        "r_inner": sensors.R_INNER,
        "r_max": sensors.R_MAX,
        "sectors": sensors.SECTORS,
        "labels": sensors.LABELS,
        "count": len(sensors.SENSORS),
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
        self.window_title = ""
        self.window_missing_since = 0.0

        touch_cfg = dict(cfg.get("touch", {}))
        touch_cfg.setdefault("process_names", tuple(cfg.get("capture", {}).get("process_names", ())))
        self.touch = create_backend(touch_cfg, self.client_rect)
        self.capture = CaptureManager(cfg.get("capture", {}), self.region_for_capture)
        self.tracker = threading.Thread(target=self._track_loop, name="window-tracker", daemon=True)

    # --- window tracking ---------------------------------------------------

    def start(self) -> None:
        self.tracker.start()

    def stop(self) -> None:
        self._stop.set()
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
        return {
            "window": self.window_title,
            "window_present": bool(self._win),
            "client_rect": rect,
            "capture_backend": getattr(self.capture._backend, "name", None),
            "frames_captured": self.capture.frames,
            "touch": dict(self.touch.stats),
            "touch_backend": self.touch.describe(),
            "touch_details": self.touch.details(),
            "panel": panel_geometry(),
        }


class App:
    def __init__(self, cfg: dict, state: GameState):
        self.cfg = cfg
        self.state = state
        self.peers: set = set()
        self.clients = 0
        video_cfg = cfg.get("video", {})
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
        return web.json_response({"ok": True, **panel_geometry()})

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

        await send(
            {
                "t": "welcome",
                "server": "maimai_pre_touch",
                "version": 1,
                "panel": panel_geometry(),
                "window": self.state.status()["client_rect"],
                "touch_backend": self.state.touch.describe(),
                "video": {
                    "codec": "H264",
                    "fps": self.cfg.get("video", {}).get("fps", 60),
                    "bitrate_kbps": self.cfg.get("video", {}).get("bitrate_kbps", 9000),
                },
            }
        )

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
                    elif kind == "stats":
                        await send({"t": "stats", "video": self.video_track.stats, "game": self.state.status()})
                elif message.type == WSMsgType.ERROR:
                    break
        finally:
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
            log.info("phone disconnected")

        return ws

    def _handle_touch(self, data: dict, held: set) -> None:
        """Apply one touch message, tracking the contacts this connection holds."""
        action = data.get("a")
        contact_id = int(data.get("id", 1))
        nx = float(data.get("x", 0.0))
        ny = float(data.get("y", 0.0))
        snap = bool(data.get("snap", False))
        if snap:
            nx, ny = sensors.snap_to_sensor(nx, ny)
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
    if os.path.isdir(WEB_DIR):
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
    state.start()
    log.info("panel: %d sensors (centre %.4f, inner ring %.4f)", len(sensors.SENSORS), sensors.R_CENTRE, sensors.R_INNER)
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
