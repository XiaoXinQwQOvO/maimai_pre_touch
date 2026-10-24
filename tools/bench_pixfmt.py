import fractions, time, statistics
import av, numpy as np

def bench(pix_fmt, n=60):
    codec = av.CodecContext.create("h264_nvenc", "w")
    codec.width = codec.height = 960
    codec.pix_fmt = pix_fmt
    codec.bit_rate = 9_000_000
    codec.time_base = fractions.Fraction(1, 90)
    codec.options = {"preset": "p1", "tune": "ull", "rc": "cbr", "delay": "0"}
    rng = np.random.default_rng(1)
    base = rng.integers(0, 255, (960, 960, 4), dtype=np.uint8)
    times, sizes = [], []
    for i in range(n):
        arr = np.clip(base.astype(np.int16) + rng.integers(-4, 4, base.shape, dtype=np.int16), 0, 255).astype(np.uint8)
        rgb = np.ascontiguousarray(arr[:, :, [2, 1, 0, 3]])
        f = av.VideoFrame.from_ndarray(rgb, format="bgra")
        if pix_fmt != "bgra":
            f = f.reformat(format=pix_fmt)
        f.pts = i
        t0 = time.perf_counter()
        out = list(codec.encode(f))
        times.append(time.perf_counter() - t0)
        sizes.append(sum(len(bytes(p)) for p in out))
    times = times[5:]
    print(f"pix_fmt={pix_fmt:8s} avg={statistics.mean(times)*1000:.2f}ms max={max(times)*1000:.2f}ms bytes={statistics.mean(sizes):.0f}")

bench("yuv420p")
bench("bgra")
