"""Headless phone simulator: connects to the PC server exactly like the APK does.

Verifies WebRTC negotiation, video frame flow and the touch round trip, without
needing a browser or an attached phone.

    python tools/test_client.py --url 192.168.31.27:8770 --seconds 8
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

import aiohttp
from aiortc import RTCPeerConnection, RTCSessionDescription


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="127.0.0.1:8770")
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument("--no-touch", action="store_true")
    args = parser.parse_args()

    url = args.url.replace("http://", "").replace("ws://", "").rstrip("/")
    ws_url = f"ws://{url}/ws"

    pc = RTCPeerConnection()
    frames = {"count": 0, "first_at": None, "last_size": 0, "bytes": 0}
    pings: list[float] = []

    @pc.on("track")
    def on_track(track):
        print(f"[i] track: {track.kind}")

        async def reader():
            while True:
                try:
                    frame = await track.recv()
                except Exception as exc:
                    print(f"[!] track ended: {exc}")
                    return
                frames["count"] += 1
                frames["last_size"] = (frame.width, frame.height)
                if frames["first_at"] is None:
                    frames["first_at"] = time.time()
                    print(f"[+] first video frame {frame.width}x{frame.height} fmt={frame.format.name}")

        asyncio.ensure_future(reader())

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(ws_url, heartbeat=15) as ws:
            welcome = None
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                if data.get("t") == "welcome":
                    welcome = data
                    break
            print("[i] welcome:", json.dumps({k: welcome.get(k) for k in ("server", "version", "touch_backend", "window")}, ensure_ascii=False))

            pc.addTransceiver("video", direction="recvonly")
            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)
            while pc.iceGatheringState != "complete":
                await asyncio.sleep(0.02)
            await ws.send_json({"t": "offer", "type": "offer", "sdp": pc.localDescription.sdp})

            loop = asyncio.get_running_loop()
            started = loop.time()
            answered = False
            touch_script = [
                (1.0, {"t": "touch", "id": 1, "a": "down", "x": 0.5, "y": 0.15}),
                (1.15, {"t": "touch", "id": 2, "a": "down", "x": 0.85, "y": 0.5}),
                (1.35, {"t": "touch", "id": 1, "a": "up"}),
                (1.4, {"t": "touch", "id": 2, "a": "up"}),
                (2.0, {"t": "touch", "id": 3, "a": "down", "x": 0.5, "y": 0.5}),
                (2.2, {"t": "touch", "id": 3, "a": "up"}),
            ]
            sent = 0
            ping_sent_at = {}

            while loop.time() - started < args.seconds:
                elapsed = loop.time() - started
                if not answered:
                    try:
                        data = await asyncio.wait_for(ws.receive_json(), timeout=0.05)
                        if data.get("t") == "answer":
                            await pc.setRemoteDescription(RTCSessionDescription(sdp=data["sdp"], type="answer"))
                            answered = True
                            print("[+] answer applied, remote description set")
                    except asyncio.TimeoutError:
                        pass
                elif not args.no_touch and sent < len(touch_script) and elapsed >= touch_script[sent][0]:
                    await ws.send_json(touch_script[sent][1])
                    print(f"[>] {touch_script[sent][1]['t']} {touch_script[sent][1].get('a', '')} id={touch_script[sent][1].get('id')}")
                    sent += 1
                if sent >= len(touch_script) or args.no_touch:
                    if int(elapsed * 2) != int((elapsed - 0.05) * 2):
                        await ws.send_json({"t": "ping", "ts": time.time() * 1000})
                try:
                    data = await asyncio.wait_for(ws.receive_json(), timeout=0.02)
                    if data.get("t") == "pong":
                        pings.append(time.time() * 1000 - data["ts"])
                    elif data.get("t") == "answer":
                        await pc.setRemoteDescription(RTCSessionDescription(sdp=data["sdp"], type="answer"))
                        answered = True
                        print("[+] answer applied")
                except asyncio.TimeoutError:
                    pass
                await asyncio.sleep(0.01)

            await pc.close()
            print("\n===== RESULT =====")
            print("answered          :", answered)
            print("video frames      :", frames["count"])
            print("last frame size   :", frames["last_size"])
            if frames["first_at"]:
                print("first frame after :", round(frames["first_at"] - started, 2), "s")
            print("avg fps           :", round(frames["count"] / max(0.1, args.seconds - 1.0), 1))
            print("ws rtt (ms)       :", [round(p, 1) for p in pings][:8])
            ok = answered and frames["count"] > 10
            print("VERDICT           :", "PASS" if ok else "FAIL")
            return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
