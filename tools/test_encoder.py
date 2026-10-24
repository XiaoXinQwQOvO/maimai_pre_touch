"""Validate the NVENC encoder that the server installs into aiortc.

Encodes a run of frames through aiortc's own H264Encoder API (so packetisation
is exercised too) and decodes the result back to prove the bitstream is valid.
"""
from __future__ import annotations

import fractions
import os
import statistics
import sys
import time

import av
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pc"))

import video as video_mod  # noqa: E402


def make_frame(index: int, width=960, height=960) -> av.VideoFrame:
    """Moving pattern so the encoder cannot cheat by re-sending identical frames."""
    yy, xx = np.mgrid[0:height, 0:width]
    phase = index * 6
    r = (np.sin((xx + phase) / 40.0) * 0.5 + 0.5) * 255
    g = (np.cos((yy - phase) / 55.0) * 0.5 + 0.5) * 255
    b = (((xx // 60) + (yy // 60) + index) % 2) * 200
    array = np.stack([r, g, b], axis=-1).astype(np.uint8)
    return av.VideoFrame.from_ndarray(array, format="rgb24").reformat(format="yuv420p")


def run(encoder, frames=90, label=""):
    timings = []
    sizes = []
    nal_types = []
    for i in range(frames):
        frame = make_frame(i)
        frame.pts = i
        frame.time_base = fractions.Fraction(1, 60)
        t0 = time.perf_counter()
        packages, pts = encoder.encode(frame, force_keyframe=(i % 30 == 0))
        timings.append(time.perf_counter() - t0)
        sizes.append(sum(len(p) for p in packages))
        for package in packages:
            nal_types.append(package[0] & 0x1F)
    timings = timings[5:]
    print(f"--- {label or type(encoder).__name__} ---")
    print(f"frames            : {frames}")
    print(f"avg encode ms     : {statistics.mean(timings) * 1000:.2f}")
    print(f"p95 encode ms     : {sorted(timings)[int(len(timings) * 0.95)] * 1000:.2f}")
    print(f"max encode ms     : {max(timings) * 1000:.2f}")
    print(f"avg packet bytes  : {statistics.mean(sizes):.0f}")
    print(f"NAL types seen    : {sorted(set(nal_types))}")
    return nal_types, sizes


def decode_roundtrip(frames=30):
    """Encode with the server encoder and decode with libav to prove validity."""
    encoder = video_mod.H264NvencEncoder()
    encoder.target_bitrate = 9_000_000
    decoder = av.CodecContext.create("h264", "r")
    decoded = 0
    total_bytes = 0
    for i in range(frames):
        frame = make_frame(i)
        frame.pts = i
        frame.time_base = fractions.Fraction(1, 60)
        annexb = b"".join(b"\x00\x00\x00\x01" + nal for nal in encoder._encode_frame(frame, i == 0))
        total_bytes += len(annexb)
        packet = av.Packet(annexb)
        packet.pts = i
        packet.time_base = fractions.Fraction(1, 60)
        for out in decoder.decode(packet):
            decoded += 1
    print(f"decoded frames    : {decoded} / {frames} (bytes={total_bytes})")
    return decoded


if __name__ == "__main__":
    print("install_encoder ->", video_mod.install_encoder("auto"))
    run(video_mod.H264NvencEncoder(), label="H264NvencEncoder(p1/ull)")
    print()
    ok = decode_roundtrip()
    print("VERDICT:", "PASS" if ok >= 28 else "FAIL")
