"""Video path: capture -> H.264 (NVENC when available, libx264 otherwise) -> WebRTC.

aisortc's default encoder is libx264 which costs 8-15 ms/frame at 960x960@60 on
this machine.  The bundled PyAV build ships `h264_nvenc`, so we swap the encoder
for a hardware one configured for ultra-low latency (p1/ull, no B-frames, no
lookahead) which brings it down to ~1-3 ms/frame.
"""
from __future__ import annotations

import asyncio
import fractions
import logging
import threading
import time

import av
import numpy as np
from aiortc import VideoStreamTrack
from aiortc.codecs.h264 import MAX_FRAME_RATE, H264Encoder
from aiortc.mediastreams import VIDEO_CLOCK_RATE, VIDEO_TIME_BASE

log = logging.getLogger(__name__)

#: Frame format the capture pipeline must hand to the encoder.  NVENC converts
#: BGRA to NV12 on the GPU, which is several milliseconds per frame cheaper than
#: doing the swscale conversion in Python.
INPUT_FORMAT = "yuv420p"

#: Bitrate control.  aiortc's own H264Encoder clamps target_bitrate to 3 Mbps, which is
#: not enough for this game's 960x960@60 picture, so the target lives here instead and
#: the encoders read it.  ``remb`` only records what the receiver suggests (aiortc's
#: sender writes it) for diagnostics; the target is steered by the phone's own reports.
#: Frames per second the encoder should budget for.  aiortc's MAX_FRAME_RATE is 30, and
#: NVENC sizes each frame as bitrate/framerate - so with the wrong value here every
#: stream ran at twice the bitrate that was configured.
FRAME_RATE = {"fps": 60}


def set_frame_rate(fps: int) -> None:
    FRAME_RATE["fps"] = max(1, int(fps))


BITRATE = {
    "target": 8_000_000,
    "min": 1_500_000,
    "max": 12_000_000,
    "remb": None,
}
_LIVE_ENCODERS: list = []


def set_bitrate_limits(max_kbps: int, min_kbps: int | None = None, start_kbps: int | None = None) -> None:
    """Configure the range the controller may use (kbps)."""
    BITRATE["max"] = int(max_kbps) * 1000
    BITRATE["min"] = int(min_kbps if min_kbps else max(800, max_kbps // 6)) * 1000
    target = int(start_kbps) * 1000 if start_kbps else BITRATE["max"]
    BITRATE["target"] = max(BITRATE["min"], min(target, BITRATE["max"]))
    log.info(
        "video: bitrate range %d-%d kbps (target %d kbps)",
        BITRATE["min"] // 1000,
        BITRATE["max"] // 1000,
        BITRATE["target"] // 1000,
    )


def set_stream_bitrate(kbps: float) -> int:
    """Set the encoder target (kbps); returns the value actually applied."""
    target = int(max(BITRATE["min"], min(float(kbps) * 1000, BITRATE["max"])))
    BITRATE["target"] = target
    return target


def stream_bitrate_kbps() -> float:
    return round(BITRATE["target"] / 1000.0, 1)


class H264NvencEncoder(H264Encoder):
    """H.264 encoder backed by NVENC with ultra-low-latency settings."""

    CODEC_NAME = "h264_nvenc"
    PIX_FMT = INPUT_FORMAT

    def __init__(self) -> None:
        super().__init__()
        _LIVE_ENCODERS.append(self)

    @property
    def target_bitrate(self) -> int:
        return int(BITRATE["target"])

    @target_bitrate.setter
    def target_bitrate(self, bitrate: int) -> None:
        # aiortc's sender writes the receiver's REMB estimate here; remember it (the
        # phone's own statistics drive the target) instead of obeying a 3 Mbps clamp
        BITRATE["remb"] = int(bitrate)
    OPTIONS = {
        "preset": "p1",
        "tune": "ull",
        "rc": "cbr",
        "delay": "0",
        "zerolatency": "1",
        "rc-lookahead": "0",
        "bf": "0",
        # a longer GOP means fewer (expensive) keyframes per second on a weak link
        "g": "120",
    }

    def _encode_frame(self, frame: av.VideoFrame, force_keyframe: bool):
        if self.codec and (
            frame.width != self.codec.width
            or frame.height != self.codec.height
            or int(FRAME_RATE["fps"]) != int(self.codec.framerate or 0)
            or abs(self.target_bitrate - self.codec.bit_rate) / max(1, self.codec.bit_rate or 1) > 0.1
        ):
            self.buffer_data = b""
            self.buffer_pts = None
            self.codec = None

        if force_keyframe:
            frame.pict_type = av.video.frame.PictureType.I
        else:
            frame.pict_type = av.video.frame.PictureType.NONE

        if self.codec is None:
            codec = av.CodecContext.create(self.CODEC_NAME, "w")
            codec.width = frame.width
            codec.height = frame.height
            codec.bit_rate = self.target_bitrate
            codec.pix_fmt = self.PIX_FMT
            fps = int(FRAME_RATE["fps"])
            codec.framerate = fractions.Fraction(fps, 1)
            codec.time_base = fractions.Fraction(1, fps)
            codec.options = dict(self.OPTIONS)
            try:
                codec.profile = "baseline"
            except Exception:
                pass
            self.codec = codec

        data_to_send = b""
        for package in self.codec.encode(frame):
            data_to_send += bytes(package)
        if data_to_send:
            yield from self._split_bitstream(data_to_send)


def _nvenc_available() -> bool:
    try:
        codec = av.CodecContext.create("h264_nvenc", "w")
        codec.width = 320
        codec.height = 320
        codec.pix_fmt = "bgra"
        codec.bit_rate = 1_000_000
        codec.options = dict(H264NvencEncoder.OPTIONS)
        codec.time_base = fractions.Fraction(1, 30)
        frame = av.VideoFrame(320, 320, "bgra")
        frame.pts = 0
        list(codec.encode(frame))
        return True
    except Exception as exc:
        log.warning("NVENC unavailable: %s", exc)
        return False


def install_encoder(choice: str = "auto") -> str:
    """Patch aiortc's video encoder factory. Returns the encoder actually used."""
    global INPUT_FORMAT

    import aiortc.codecs as codecs_pkg
    import aiortc.rtcrtpsender as sender_mod

    original = sender_mod.get_encoder
    use_nvenc = choice in ("auto", "nvenc") and _nvenc_available()

    if use_nvenc:
        INPUT_FORMAT = "bgra"
        H264NvencEncoder.PIX_FMT = "bgra"
    else:
        INPUT_FORMAT = "yuv420p"
        H264NvencEncoder.PIX_FMT = "yuv420p"

    def patched(codec):
        mime = codec.mimeType.lower()
        if mime == "video/h264" and use_nvenc:
            encoder = H264NvencEncoder()
        else:
            encoder = original(codec)
        log.info("encoder created for %s: %s", codec.mimeType, type(encoder).__name__)
        return encoder

    sender_mod.get_encoder = patched
    # keep a reference so linters do not drop the import
    _ = codecs_pkg
    log.info("video encoder: %s (input %s)", "h264_nvenc" if use_nvenc else "libx264", INPUT_FORMAT)
    return "h264_nvenc" if use_nvenc else "libx264"


class GameVideoTrack(VideoStreamTrack):
    """Sends the game's picture at a rock-steady frame rate.

    Capturing and sending run on separate threads: a pump thread keeps the newest frame
    (already scaled and converted to the encoder's pixel format) and ``recv`` hands out
    whatever is current every 1/fps seconds.  Waiting for a fresh capture inside ``recv``
    instead used to make the stream skip a frame whenever one encode took slightly longer
    than the frame interval - that is what dropped this machine from 60 to ~50 fps - and
    it also added the capture jitter to every frame's latency.
    """

    kind = "video"

    def __init__(
        self, capture_manager, fps: int = 60, scale: float = 1.0, bitrate_kbps: int = 9000
    ):
        super().__init__()
        self._capture = capture_manager
        self._fps = max(1, int(fps))
        self._scale = float(scale)
        self._pending: av.VideoFrame | None = None
        self._pending_at = 0.0
        self._pending_lock = threading.Lock()
        self._pump_stop = threading.Event()
        self._frames_sent = 0
        self._started = time.time()
        self._times = {
            "grab": [],  # how long the pump waited for a new capture
            "convert": [],  # scale + pixel format conversion, in the pump
            "pace": [],  # the send loop's own sleep
            "total": [],  # the send loop's duration
            "period": [],  # the send loop's period
            "age": [],  # age of the frame being sent
        }
        self._t0 = None
        self._timestamp = 0
        self._last_period_start = None
        self._resize = None
        if abs(self._scale - 1.0) > 1e-3:
            import cv2

            self._resize = cv2
        self._pump = threading.Thread(target=self._pump_loop, name="video-pump", daemon=True)
        self._pump.start()

    # -- capture side -------------------------------------------------------
    def _pump_loop(self) -> None:
        blank_size = None
        while not self._pump_stop.is_set():
            t_grab = time.perf_counter()
            try:
                array = self._capture.get_frame()
            except Exception as exc:  # noqa: BLE001 - never kill the pump
                log.warning("video: capture failed: %s", exc)
                array = None
            self._record("grab", time.perf_counter() - t_grab)
            if array is None:
                self._pump_stop.wait(0.002)
                continue
            t_conv = time.perf_counter()
            try:
                if self._resize is not None:
                    array = self._resize.resize(
                        array,
                        None,
                        fx=self._scale,
                        fy=self._scale,
                        interpolation=self._resize.INTER_LINEAR,
                    )
                frame = av.VideoFrame.from_ndarray(array, format="bgra")
                if INPUT_FORMAT != "bgra":
                    frame = frame.reformat(format=INPUT_FORMAT)
            except Exception as exc:  # noqa: BLE001
                log.warning("video: frame conversion failed: %s", exc)
                continue
            self._record("convert", time.perf_counter() - t_conv)
            blank_size = (frame.width, frame.height)
            with self._pending_lock:
                self._pending = frame
                self._pending_at = time.time()

    def _blank_frame(self) -> av.VideoFrame:
        side = max(2, int(round(960 * self._scale)))
        empty = np.zeros((side, side, 4), dtype="uint8")
        frame = av.VideoFrame.from_ndarray(empty, format="bgra")
        if INPUT_FORMAT != "bgra":
            frame = frame.reformat(format=INPUT_FORMAT)
        return frame

    # -- send side ----------------------------------------------------------
    def _record(self, key: str, seconds: float) -> None:
        samples = self._times[key]
        samples.append(seconds * 1000.0)
        if len(samples) > 240:
            del samples[:120]

    @property
    def stats(self) -> dict:
        elapsed = max(1e-6, time.time() - self._started)
        timings = {k: (round(sum(v) / len(v), 2) if v else None) for k, v in self._times.items()}
        return {
            "bitrate_kbps": stream_bitrate_kbps(),
            "remb_kbps": None if BITRATE["remb"] is None else round(BITRATE["remb"] / 1000.0, 1),
            "frames_sent": self._frames_sent,
            "fps": round(self._frames_sent / elapsed, 1),
            "last_frame_age": round(time.time() - self._pending_at, 3) if self._pending_at else None,
            "avg_ms": timings,
        }

    def stop(self) -> None:
        self._pump_stop.set()
        super().stop()

    async def recv(self):
        # aiortc's MediaStreamTrack.next_timestamp() hardcodes 30 fps
        # (VIDEO_PTIME = 1/30), so pace the track ourselves at the configured fps.
        t_start = time.perf_counter()
        now = time.time()
        if self._t0 is None:
            self._t0 = now
            self._timestamp = 0
        else:
            self._timestamp += int(VIDEO_CLOCK_RATE / self._fps)
            due = self._t0 + self._timestamp / VIDEO_CLOCK_RATE
            if due - now > -0.5:
                if due > now:
                    await asyncio.sleep(due - now)
            else:  # fell far behind (e.g. after a stall) - resync instead of bursting
                self._t0 = now - self._timestamp / VIDEO_CLOCK_RATE
        self._record("pace", time.perf_counter() - t_start)

        with self._pending_lock:
            frame = self._pending
            age = time.time() - self._pending_at if self._pending_at else None
        if age is not None:
            self._record("age", age)
        if frame is None:
            frame = self._blank_frame()

        frame.pts = self._timestamp
        frame.time_base = VIDEO_TIME_BASE
        self._record("total", time.perf_counter() - t_start)
        if self._last_period_start:
            self._record("period", t_start - self._last_period_start)
        self._last_period_start = t_start
        self._frames_sent += 1
        return frame
