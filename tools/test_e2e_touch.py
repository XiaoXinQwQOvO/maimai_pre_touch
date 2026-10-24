"""End-to-end touch test: phone simulator -> server -> game memory.

Connects to a running pc/server.py over the same WebSocket the APK uses, presses
sensors, and verifies from an *independent* process view (ReadProcessMemory) that
the game's maitouch status packet really carries those sensors, and that it goes
back to idle when the finger lifts.

The touch points are derived from the panel circle the server reports, so the test
follows the detected circle instead of assuming the whole picture is the panel.

    python tools/test_e2e_touch.py --url 127.0.0.1:8770
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pc"))

import aiohttp  # noqa: E402

import maitouch_port as MP  # noqa: E402
import sensors  # noqa: E402


async def fetch_panel(session, url: str) -> dict:
    async with session.get(f"http://{url}/api/panel") as response:
        return await response.json()


async def press_and_check(ws, port, label, nx, ny, expected, results):
    await ws.send_json({"t": "touch", "id": 1, "a": "down", "x": round(nx, 5), "y": round(ny, 5)})
    await asyncio.sleep(0.35)
    packet = port.read(MP.PACKET_RVA, sensors.PACKET_LEN)
    found = sensors.sensors_in(packet)
    ok = found == expected
    results.append(ok)
    print(
        f"[{'PASS' if ok else 'FAIL'}] {label:<28} at ({nx:.3f}, {ny:.3f}) -> "
        f"{packet.hex(' ')}  sensors={found} (expected {expected})"
    )
    await ws.send_json({"t": "touch", "id": 1, "a": "up"})
    await asyncio.sleep(0.3)
    idle = port.read(MP.PACKET_RVA, sensors.PACKET_LEN)
    ok = idle == sensors.IDLE_PACKET
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {'  release -> idle':<28} {idle.hex(' ')}")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="127.0.0.1:8770")
    args = parser.parse_args()
    url = args.url.replace("http://", "").replace("ws://", "").rstrip("/")

    port = MP.MaitouchPort()
    if not port.attach():
        print(f"[!] cannot attach to the game: {port.last_error}")
        return 1
    print(f"[i] game pid={port.pid} base={port.base:#x} frozen={port.probe()['frozen']}")

    results: list[bool] = []
    async with aiohttp.ClientSession() as session:
        info = await fetch_panel(session, url)
        panel = {"cx": info["cx"], "cy": info["cy"], "rx": info["rx"], "ry": info["ry"]}
        print(
            f"[i] panel from server: state={info.get('state')} detected={info.get('detected')} "
            f"cx={panel['cx']:.4f} cy={panel['cy']:.4f} rx={panel['rx']:.4f} ry={panel['ry']:.4f} "
            f"stretch={panel['rx']/panel['ry']:.3f}"
        )
        ok = info.get("state") in ("locked", "manual")
        results.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] {'panel circle detected':<28} state={info.get('state')}")

        def point(radius: float, degrees: float) -> tuple[float, float]:
            """Panel coordinates -> frame coordinates, on the *sector centre* angle.

            Using the middle of a sector (22.5 deg, 112.5 deg, ...) instead of a
            boundary keeps the expectation stable when the detected circle drifts by a
            few thousandths between the fetch and the touch.
            """
            angle = math.radians(degrees)
            return (
                panel["cx"] + radius * math.cos(angle) * panel["rx"],
                panel["cy"] + radius * math.sin(angle) * panel["ry"],
            )

        async with session.ws_connect(f"ws://{url}/ws", heartbeat=15) as ws:
            welcome = None
            for _ in range(20):
                msg = await ws.receive_json()
                if msg.get("t") == "welcome":
                    welcome = msg
                    break
            print(f"[i] server touch backend: {welcome.get('touch_backend') if welcome else '?'}")
            results.append(bool(welcome and welcome.get("touch_backend") == "maitouch"))
            if welcome and welcome.get("panel"):
                same = all(abs(welcome["panel"].get(k, 0.0) - panel[k]) < 0.02 for k in panel)
                results.append(same)
                print(f"[{'PASS' if same else 'FAIL'}] {'welcome panel == /api/panel':<28}")

            centre = point(0.0, 0.0)
            await press_and_check(ws, port, "panel centre", *centre, ["C"], results)
            await press_and_check(ws, port, "outer ring A1 (22.5 deg)", *point(0.85, 22.5), ["O0"], results)
            await press_and_check(ws, port, "inner ring B3 (112.5 deg)", *point(0.40, 112.5), ["I90"], results)
            await press_and_check(ws, port, "outside the circle", *point(1.25, 22.5), [], results)

            # two fingers at once
            await ws.send_json({"t": "touch", "id": 1, "a": "down", "x": round(centre[0], 5), "y": round(centre[1], 5)})
            outer = point(0.85, 112.5)
            await ws.send_json({"t": "touch", "id": 2, "a": "down", "x": round(outer[0], 5), "y": round(outer[1], 5)})
            await asyncio.sleep(0.4)
            packet = port.read(MP.PACKET_RVA, sensors.PACKET_LEN)
            found = sensors.sensors_in(packet)
            ok = sorted(found) == sorted([sensors.CENTRE, "O90"])
            results.append(ok)
            print(f"[{'PASS' if ok else 'FAIL'}] {'two fingers':<28} {packet.hex(' ')}  sensors={found}")
            await ws.send_json({"t": "touch", "id": 1, "a": "up"})
            await ws.send_json({"t": "touch", "id": 2, "a": "up"})
            await asyncio.sleep(0.3)
            idle = port.read(MP.PACKET_RVA, sensors.PACKET_LEN)
            ok = idle == sensors.IDLE_PACKET
            results.append(ok)
            print(f"[{'PASS' if ok else 'FAIL'}] {'both released -> idle':<28} {idle.hex(' ')}")

            # hang up while a finger is still pressed: the server must release it
            await ws.send_json(
                {"t": "touch", "id": 7, "a": "down", "x": round(centre[0], 5), "y": round(centre[1], 5)}
            )
            await asyncio.sleep(0.35)
            print(f"[i] packet while held: {port.read(MP.PACKET_RVA, sensors.PACKET_LEN).hex(' ')}")
        # websocket closed here
        await asyncio.sleep(0.6)
        packet = port.read(MP.PACKET_RVA, sensors.PACKET_LEN)
        ok = packet == sensors.IDLE_PACKET
        results.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] {'disconnect releases touch':<28} {packet.hex(' ')}")

    port.detach()
    print(f"\nRESULT: {sum(results)}/{len(results)} checks passed -> {'PASS' if all(results) else 'FAIL'}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
