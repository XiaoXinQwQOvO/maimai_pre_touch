"""Benchmark the server-side video path: capture rate, NVENC vs libx264 encode rate."""
from __future__ import annotations

import fractions
import os
import statistics
import sys
import time

import av
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pc"))

import window as window_mod  # noqa: E402
from capture import CaptureManager  # noqa: E402


def bench_capture(seconds=3.0):
    window_mod.set_dpi_aware()
    win = window_mod.find_game_window()
    if not win:
        print("[!] no game window - using a synthetic frame source instead")
        return None
    rect = window_mod.client_rect(win)
    print(f"[i] game window client rect: {rect}")
    cfg = {"backend": "dxcam", "fps": 60}
    cap = CaptureManager(cfg, lambda: rect)
    cap.get_frame()  # warm up / start dxcam
    time.sleep(0.5)
    stamps = []
    end = time.time() + seconds
    last = None
    distinct = 0
    while time.time() < end:
        frame = cap.get_frame()
        now = time.perf_counter()
        if frame is not None and (last is None or frame.ctypes.data != last):
            stamps.append(now)
            distinct += 1
        last = frame.ctypes.data if frame is not None else last
        time.sleep(0.001)
    cap.close()
    if len(stamps) < 2:
        return {"frames": len(stamps)}
    intervals = np.diff(stamps)
    return {
        "frames": len(stamps),
        "seconds": round(seconds, 2),
        "fps": round(len(stamps) / seconds, 1),
        "median_ms": round(float(np.median(intervals)) * 1000, 2),
        "p95_ms": round(float(np.percentile(intervals, 95)) * 1000, 2),
        "distinct_buffers": distinct,
    }


def bench_encode(codec_name: str, width=960, height=960, frames=120, bitrate=9_000_000, options=None):
    codec = av.CodecContext.create(codec_name, "w")
    codec.width = width
    codec.height = height
    codec.pix_fmt = "yuv420p"
    codec.bit_rate = bitrate
    codec.framerate = fractions.Fraction(60, 1)
    codec.time_base = fractions.Fraction(1, 90)
    if options:
        codec.options = dict(options)
    rng = np.random.default_rng(0)
    base = rng.integers(0, 255, (height, width, 3), dtype=np.uint8)
    times = []
    total_bytes = 0
    for i in range(frames):
        noise = rng.integers(-6, 6, (height, width, 3), dtype=np.int16)
        arr = np.clip(base.astype(np.int16) + noise, 0, 255).astype(np.uint8)
        frame = av.VideoFrame.from_ndarray(arr, format="rgb24").reformat(format="yuv420p")
        frame.pts = i
        t0 = time.perf_counter()
        for packet in codec.encode(frame):
            total_bytes += len(bytes(packet))
        times.append(time.perf_counter() - t0)
    times = times[5:]
    return {
        "codec": codec_name,
        "frames": frames,
        "avg_ms": round(statistics.mean(times) * 1000, 2),
        "median_ms": round(statistics.median(times) * 1000, 2),
        "max_ms": round(max(times) * 1000, 2),
        "fps_capacity": round(1.0 / statistics.mean(times), 1),
        "avg_kb_per_frame": round(total_bytes / frames / 1024, 1),
    }


if __name__ == "__main__":
    print("=== capture ===")
    print(bench_capture())
    print("\n=== encode (uinteger noise pattern, worst case) ===")
    for name, opts in (
        ("h264_nvenc", {"preset": "p1", "tune": "ull", "rc": "cbr", "delay": "0"}),
        ("h264_nvenc", {"preset": "p4", "tune": "ull", "rc": "cbr", "delay": "0"}),
        ("libx264", {"preset": "ultrafast", "tune": "zerolatency"}),
    ):
        try:
            print(bench_encode(name, options=opts), "opts=", opts)
        except Exception as exc:
            print(f"{name}: FAILED {exc}")
