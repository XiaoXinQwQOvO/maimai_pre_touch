"""Does the encoder really follow BITRATE['target']?  Encode 90 frames at two targets."""
import sys
import time

sys.path.insert(0, "pc")
import av  # noqa: E402
import numpy as np  # noqa: E402

import imgio  # noqa: E402
import video  # noqa: E402

frame_bgr = imgio.imread("tools/probe_out/median.png")
frame_bgr = np.ascontiguousarray(frame_bgr)
height, width = frame_bgr.shape[:2]
video.install_encoder("auto")


def encode_at(kbps, frames=90):
    encoder = video.H264NvencEncoder()
    video.BITRATE["target"] = kbps * 1000
    total = 0
    t0 = time.perf_counter()
    for index in range(frames):
        frame = av.VideoFrame.from_ndarray(frame_bgr, format="bgr24")
        frame = frame.reformat(format=video.INPUT_FORMAT)
        frame.pts = index
        frame.time_base = video.VIDEO_TIME_BASE
        data = b"".join(bytes(p) for p in encoder._encode_frame(frame, index == 0))
        total += len(data)
    elapsed = time.perf_counter() - t0
    fps = frames / elapsed
    print(
        f"target {kbps:5d} kbps -> {total * 8 / elapsed / 1000:8.0f} kbps actual, "
        f"{total / frames:7.0f} bytes/frame, {fps:5.1f} fps encode"
    )
    return total * 8 / elapsed / 1000


for kbps in (3000, 6000, 8000, 12000):
    encode_at(kbps)
