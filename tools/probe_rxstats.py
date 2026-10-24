"""Send one rx-stats message and show whether the controller reacts."""
import asyncio
import json
import sys
import urllib.request

import aiohttp


async def main() -> int:
    url = "127.0.0.1:8770"
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"ws://{url}/ws", heartbeat=15) as ws:
            for _ in range(20):
                msg = await ws.receive_json()
                if msg.get("t") == "welcome":
                    break
            await ws.send_json({"t": "rx-stats", "fps": 60, "kbps": 9000, "jitter": 12, "lost": 0, "rtt": 3})
            await asyncio.sleep(0.4)
            await ws.send_json({"t": "rx-stats", "fps": 60, "kbps": 9000, "jitter": 12, "lost": 0, "rtt": 3})
            await asyncio.sleep(2.5)
            await ws.send_json({"t": "rx-stats", "fps": 60, "kbps": 9000, "jitter": 12, "lost": 0, "rtt": 3})
            await asyncio.sleep(0.5)
    with urllib.request.urlopen(f"http://{url}/api/status", timeout=5) as response:
        status = json.load(response)
    print("phone:", status.get("phone"))
    print("bitrate_kbps:", status["video"].get("bitrate_kbps"), "remb:", status["video"].get("remb_kbps"))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
