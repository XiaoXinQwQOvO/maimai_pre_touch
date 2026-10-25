"""How long does a touch take from the phone's WebSocket message to the game's memory?

Polled with a busy loop: on Windows asyncio.sleep(0.0005) really sleeps ~15 ms, which
would show up as a fake 15 ms of touch latency.
"""
from __future__ import annotations

import asyncio
import math
import statistics
import sys
import time

sys.path.insert(0, "pc")
sys.path.insert(0, "tools")

import aiohttp  # noqa: E402

import maitouch_port as MP  # noqa: E402
import sensors  # noqa: E402


async def main() -> int:
    url = "127.0.0.1:8770"
    port = MP.MaitouchPort()
    if not port.attach():
        print(f"[!] cannot attach: {port.last_error}")
        return 1
    port.freeze()

    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://{url}/api/panel") as response:
            info = await response.json()
        panel = {"cx": info["cx"], "cy": info["cy"], "rx": info["rx"], "ry": info["ry"]}
        angle = math.radians(22.5)
        point = (
            panel["cx"] + 0.85 * math.cos(angle) * panel["rx"],
            panel["cy"] + 0.85 * math.sin(angle) * panel["ry"],
        )

        async with session.ws_connect(f"ws://{url}/ws", heartbeat=15) as ws:
            for _ in range(20):
                msg = await ws.receive_json()
                if msg.get("t") == "welcome":
                    break

            down_latency = []
            up_latency = []
            for index in range(20):
                await ws.send_json({"t": "touch", "id": index, "a": "down", "x": point[0], "y": point[1]})
                sent = time.perf_counter()
                deadline = sent + 0.5
                while time.perf_counter() < deadline:
                    if sensors.sensors_in(port.read(MP.PACKET_RVA, sensors.PACKET_LEN)):
                        down_latency.append((time.perf_counter() - sent) * 1000)
                        break
                await ws.send_json({"t": "touch", "id": index, "a": "up"})
                sent = time.perf_counter()
                deadline = sent + 0.5
                while time.perf_counter() < deadline:
                    if port.read(MP.PACKET_RVA, sensors.PACKET_LEN) == sensors.IDLE_PACKET:
                        up_latency.append((time.perf_counter() - sent) * 1000)
                        break
                await asyncio.sleep(0.05)

    port.restore()
    for label, values in (("press", down_latency), ("release", up_latency)):
        if values:
            print(
                f"{label:8s}: median {statistics.median(values):5.2f} ms, "
                f"max {max(values):5.2f} ms, n={len(values)}"
            )
        else:
            print(f"{label:8s}: no sample")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
