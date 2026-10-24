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
import time

import av
from aiortc import VideoStreamTrack
from aiortc.codecs.h264 import MAX_FRAME_RATE, H264Encoder
from aiortc.mediastreams import VIDEO_CLOCK_RATE, VIDEO_TIME_BASE

log = logging.getLogger(__name__)

#: Frame format the capture pipeline must hand to the encoder.  NVENC converts
#: BGRA to NV12 on the GPU, which is several milliseconds per frame cheaper than
#: doing the swscale conversion in Python.
INPUT_FORMAT = "yuv420p"


class H264NvencEncoder(H264Encoder):
    """H.264 encoder backed by NVENC with ultra-low-latency settings."""

    CODEC_NAME = "h264_nvenc"
    PIX_FMT = INPUT_FORMAT
    OPTIONS = {
        "preset": "p1",
        "tune": "ull",
        "rc": "cbr",
        "delay": "0",
    }

    def _encode_frame(self, frame: av.VideoFrame, force_keyframe: bool):
        if self.codec and (
            frame.width != self.codec.width
            or frame.height != self.codec.height
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
            codec.framerate = fractions.Fraction(MAX_FRAME_RATE, 1)
            codec.time_base = fractions.Fraction(1, MAX_FRAME_RATE)
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
    """Pulls the latest captured frame and turns it into a video track."""

    kind = "video"

    def __init__(self, capture_manager, fps: int = 60, scale: float = 1.0, bitrate_kbps: int = 9000):
        super().__init__()
        self._capture = capture_manager
        self._fps = max(1, int(fps))
        self._scale = float(scale)
        self._last_frame = None
        self._frames_sent = 0
        self._started = time.time()
        self._times = {"grab": [], "convert": [], "pace": [], "total": []}
        self._t0 = None
        self._timestamp = 0
        self._resize = None
        if abs(self._scale - 1.0) > 1e-3:
            import cv2

            self._resize = cv2

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
            "frames_sent": self._frames_sent,
            "fps": round(self._frames_sent / elapsed, 1),
            "last_frame_age": round(time.time() - self._capture.last_frame_time, 3) if self._capture.last_frame_time else None,
            "avg_ms": timings,
        }

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
            if due - now > -1.0:
                if due > now:
                    await asyncio.sleep(due - now)
            else:  # fell far behind (e.g. after a stall) - resync instead of bursting
                self._t0 = now - self._timestamp / VIDEO_CLOCK_RATE
        self._record("pace", time.perf_counter() - t_start)
        pts = self._timestamp
        time_base = VIDEO_TIME_BASE

        t_grab = time.perf_counter()
        # get_latest_frame() waits for the next DXGI frame (up to ~16 ms); keep it
        # off the event loop so touch/signalling messages are not delayed.
        array = await asyncio.to_thread(self._capture.get_frame)
        self._record("grab", time.perf_counter() - t_grab)
        if array is not None:
            self._last_frame = array
        elif self._last_frame is None:
            import numpy as np

            self._last_frame = np.zeros((960, 960, 4), dtype="uint8")
        frame_array = self._last_frame

        t_conv = time.perf_counter()
        if self._resize is not None and frame_array is not None:
            frame_array = self._resize.resize(frame_array, None, fx=self._scale, fy=self._scale, interpolation=self._resize.INTER_LINEAR)
        frame = av.VideoFrame.from_ndarray(frame_array, format="bgra")
        if INPUT_FORMAT != "bgra":
            frame = frame.reformat(format=INPUT_FORMAT)
        frame.pts = pts
        frame.time_base = time_base
        self._record("convert", time.perf_counter() - t_conv)
        self._record("total", time.perf_counter() - t_start)
        self._frames_sent += 1
        return frame
