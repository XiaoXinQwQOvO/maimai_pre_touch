"""Video path: capture -> H.264 (NVENC when available, libx264 otherwise) -> WebRTC.

aisortc's default encoder is libx264 which costs 8-15 ms/frame at 960x960@60 on
this machine.  The bundled PyAV build ships `h264_nvenc`, so we swap the encoder
for a hardware one configured for ultra-low latency (p1/ull, no B-frames, no
lookahead) which brings it down to ~1-3 ms/frame.
"""
from __future__ import annotations

import fractions
import logging
import time

import av
from aiortc import VideoStreamTrack
from aiortc.codecs.h264 import MAX_FRAME_RATE, H264Encoder

log = logging.getLogger(__name__)


class H264NvencEncoder(H264Encoder):
    """H.264 encoder backed by NVENC with ultra-low-latency settings."""

    CODEC_NAME = "h264_nvenc"
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
            codec.pix_fmt = "yuv420p"
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
        codec.pix_fmt = "yuv420p"
        codec.bit_rate = 1_000_000
        codec.options = dict(H264NvencEncoder.OPTIONS)
        codec.time_base = fractions.Fraction(1, 30)
        frame = av.VideoFrame(320, 320, "yuv420p")
        frame.pts = 0
        list(codec.encode(frame))
        return True
    except Exception as exc:
        log.warning("NVENC unavailable: %s", exc)
        return False


def install_encoder(choice: str = "auto") -> str:
    """Patch aiortc's video encoder factory. Returns the encoder actually used."""
    import aiortc.codecs as codecs_pkg
    import aiortc.rtcrtpsender as sender_mod

    original = sender_mod.get_encoder
    use_nvenc = choice in ("auto", "nvenc") and _nvenc_available()

    def patched(codec):
        mime = codec.mimeType.lower()
        if mime == "video/h264" and use_nvenc:
            return H264NvencEncoder()
        return original(codec)

    sender_mod.get_encoder = patched
    # keep a reference so linters do not drop the import
    _ = codecs_pkg
    log.info("video encoder: %s", "h264_nvenc" if use_nvenc else "libx264")
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
        self._resize = None
        if abs(self._scale - 1.0) > 1e-3:
            import cv2

            self._resize = cv2

    @property
    def stats(self) -> dict:
        elapsed = max(1e-6, time.time() - self._started)
        return {
            "frames_sent": self._frames_sent,
            "fps": round(self._frames_sent / elapsed, 1),
            "last_frame_age": round(time.time() - self._capture.last_frame_time, 3) if self._capture.last_frame_time else None,
        }

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        array = self._capture.get_frame()
        if array is not None:
            self._last_frame = array
        elif self._last_frame is None:
            import numpy as np

            self._last_frame = np.zeros((960, 960, 4), dtype="uint8")
        frame_array = self._last_frame
        if self._resize is not None and frame_array is not None:
            frame_array = self._resize.resize(frame_array, None, fx=self._scale, fy=self._scale, interpolation=self._resize.INTER_LINEAR)
        frame = av.VideoFrame.from_ndarray(frame_array, format="bgra")
        frame = frame.reformat(format="yuv420p")
        frame.pts = pts
        frame.time_base = time_base
        self._frames_sent += 1
        return frame
