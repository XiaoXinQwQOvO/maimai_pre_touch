"""Two fingers at once: both must reach the game's packet while both slide.

The phone used a single global timestamp to throttle move messages, so with two fingers
in the air whichever pointer came second in each pair was dropped and that finger never
moved.  This test drives the same WebSocket the APK uses and reads the game's memory
directly, so it verifies the server + backend path independently of the browser.

    python tools/test_two_finger.py --url 127.0.0.1:8770
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


def point(panel, radius, degrees):
    angle = math.radians(degrees)
    return (
        panel["cx"] + radius * math.cos(angle) * panel["rx"],
        panel["cy"] + radius * math.sin(angle) * panel["ry"],
    )


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="127.0.0.1:8770")
    args = parser.parse_args()
    url = args.url.replace("http://", "").replace("ws://", "").rstrip("/")

    port = MP.MaitouchPort()
    if not port.attach():
        print(f"[!] cannot attach to the game: {port.last_error}")
        return 1
    if not port.freeze():
        print("[!] cannot freeze build_status")
        return 1

    results: list[bool] = []

    def check(ok: bool, label: str, detail: str = "") -> None:
        results.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] {label:<44} {detail}")

    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://{url}/api/panel") as response:
            info = await response.json()
        panel = {"cx": info["cx"], "cy": info["cy"], "rx": info["rx"], "ry": info["ry"]}
        print(
            f"[i] panel: state={info.get('state')} cx={panel['cx']:.4f} cy={panel['cy']:.4f} "
            f"rx={panel['rx']:.4f} ry={panel['ry']:.4f}"
        )

        async with session.ws_connect(f"ws://{url}/ws", heartbeat=15) as ws:
            for _ in range(20):
                msg = await ws.receive_json()
                if msg.get("t") == "welcome":
                    break

            async def down(contact_id: int, nx: float, ny: float) -> None:
                await ws.send_json(
                    {"t": "touch", "id": contact_id, "a": "down", "x": round(nx, 5), "y": round(ny, 5)}
                )

            async def move(contact_id: int, nx: float, ny: float) -> None:
                await ws.send_json(
                    {"t": "touch", "id": contact_id, "a": "move", "x": round(nx, 5), "y": round(ny, 5)}
                )

            async def up(contact_id: int) -> None:
                await ws.send_json({"t": "touch", "id": contact_id, "a": "up"})

            def sensors_now() -> list[str]:
                packet = port.read(MP.PACKET_RVA, sensors.PACKET_LEN)
                return sensors.sensors_in(packet) or []

            # 1. two fingers pressed at once, opposite sides
            a = point(panel, 0.85, 22.5)
            b = point(panel, 0.85, 202.5)
            await down(1, *a)
            await down(2, *b)
            await asyncio.sleep(0.25)
            both = sensors_now()
            want = sorted({sensors.sensor_at(*a, panel), sensors.sensor_at(*b, panel)})
            check(len(both) == 2, "two fingers pressed -> two sensors", f"{both}")
            check(
                set(both) == set(want),
                "  the two sensors are the ones asked for",
                f"got {sorted(both)}, wanted {want}",
            )

            # 2. both slide, one step per 45 degrees, ten steps (as a real swipe)
            lost = 0
            for step in range(1, 11):
                aa = point(panel, 0.85, 22.5 + 45 * step)
                bb = point(panel, 0.85, 202.5 + 45 * step)
                await move(1, *aa)
                await move(2, *bb)
                await asyncio.sleep(0.04)
                now = sensors_now()
                if len(now) < 2:
                    lost += 1
                    print(f"      step {step}: only {now}")
            check(lost == 0, "both sensors kept while both slide", f"{lost}/10 steps lost one")

            # 3. lift one finger, the other must stay
            await up(1)
            await asyncio.sleep(0.2)
            left = sensors_now()
            check(len(left) == 1, "one finger lifted -> the other stays", f"{left}")

            # 4. lift the second
            await up(2)
            await asyncio.sleep(0.3)
            idle = port.read(MP.PACKET_RVA, sensors.PACKET_LEN)
            check(idle == sensors.IDLE_PACKET, "both lifted -> idle packet", idle.hex(" ") if idle else "?")

            # 5. inner + outer at the same time (two hands, different rings)
            a = point(panel, 0.40, 112.5)
            b = point(panel, 0.85, 292.5)
            await down(3, *a)
            await down(4, *b)
            await asyncio.sleep(0.25)
            now = sensors_now()
            check(len(now) == 2, "inner + outer pressed together", f"{now}")
            await up(3)
            await up(4)
            await asyncio.sleep(0.25)

    port.restore()
    passed = sum(1 for r in results if r)
    print(f"\nRESULT: {passed}/{len(results)} checks passed -> {'PASS' if passed == len(results) else 'FAIL'}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
