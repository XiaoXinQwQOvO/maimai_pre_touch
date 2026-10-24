"""Control the maitouch touch panel state of a running maimai from outside.

Examples
--------
  python tools/maitouch_ctl.py probe
  python tools/maitouch_ctl.py set C O90 I270
  python tools/maitouch_ctl.py raw 2:0x41 4:0x50
  python tools/maitouch_ctl.py clear
  python tools/maitouch_ctl.py restore
  python tools/maitouch_ctl.py daemon --port 5599        # UDP command server

UDP protocol (one line per datagram, answer is one line of text):
  PING                 -> "PONG <pid> <base>"
  SET C,O90,I270       -> "OK <packet hex> <zones>"
  RAW 2:0x41,4:0x50    -> "OK <packet hex>"
  CLEAR                -> "OK <packet hex>"
  GET                  -> "<packet hex> <zones>"
  RESTORE              -> "RESTORED"

How it works (see docs/re_maitouch.md):
  mice.dll regenerates the 14-byte status packet ~500x/s right before writing
  it to the game.  We patch the first byte of that generator with `ret` and
  then keep the packet set to whatever we want, so the game is fed a fully
  synthetic 17-sensor touch panel with arbitrary simultaneous zones.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import maitouch_state as MS  # noqa: E402


def do_probe(w):
    info = w.probe()
    pkt = bytes.fromhex(info["packet"].replace(" ", ""))
    info["zones"] = MS.describe(pkt)
    return json.dumps(info, indent=2)


def do_set(w, zones, raw=None):
    if raw:
        buckets = {}
        for tok in raw:
            tok = tok.strip()
            if not tok:
                continue
            idx, val = tok.split(":")
            buckets[int(idx, 0)] = int(val, 0)
        # sensor byte value is 0x40|bits; allow both "2:0x41" and "2:0x01"
        fixed = {k: (v & 0x3F) if (v & 0x3F) else 0 for k, v in buckets.items()}
        pkt = w.set_bytes(fixed)
    else:
        pkt = w.write_packet(MS.packet_from_zones(zones))
    return pkt


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe")
    sp = sub.add_parser("set")
    sp.add_argument("zones", nargs="+")
    sp = sub.add_parser("raw")
    sp.add_argument("bytes", nargs="+")
    sub.add_parser("clear")
    sub.add_parser("restore")
    sp = sub.add_parser("daemon")
    sp.add_argument("--port", type=int, default=5599)
    sp.add_argument("--host", default="127.0.0.1")
    sp = sub.add_parser("sweep")
    sp.add_argument("--hold", type=float, default=0.35)
    args = ap.parse_args()

    if args.cmd == "daemon":
        return daemon(args.host, args.port)

    w = MS.MaitouchWriter()
    try:
        if args.cmd == "probe":
            print(do_probe(w))
        elif args.cmd in ("set", "raw"):
            print(f"freeze: {w.freeze()}")
            pkt = do_set(w, getattr(args, "zones", None), getattr(args, "bytes", None))
            print(f"packet: {pkt.hex(' ')}  zones={MS.describe(pkt)}")
            print("(patch stays active; use 'restore' to undo)")
        elif args.cmd == "clear":
            w.freeze()
            print(f"packet: {w.clear().hex(' ')}")
        elif args.cmd == "restore":
            print(w.unfreeze())
            w.clear()
            print("idle packet written again")
        elif args.cmd == "sweep":
            w.freeze()
            for name in ["C"] + [f"{r}{a}" for r in "OI" for a in (0, 45, 90, 135, 180, 225, 270, 315)]:
                pkt = w.write_packet(MS.packet_from_zones([name]))
                print(f"{name:>5} -> {pkt.hex(' ')}")
                time.sleep(args.hold)
            w.clear()
            print("cleared")
    finally:
        w.close()
    return 0


def daemon(host, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, port))
    print(f"[i] maitouch control daemon on udp://{host}:{port}")
    w = None
    while True:
        data, addr = sock.recvfrom(4096)
        line = data.decode("utf-8", "replace").strip()
        reply = "ERR"
        try:
            if w is None:
                w = MS.MaitouchWriter()
            words = line.split(None, 1)
            verb = words[0].upper() if words else ""
            rest = words[1] if len(words) > 1 else ""
            if verb == "PING":
                reply = f"PONG {w.pid} {w.base:#x}"
            elif verb == "SET":
                w.freeze()
                pkt = w.write_packet(MS.packet_from_zones([z for z in rest.replace(",", " ").split() if z]))
                reply = f"OK {pkt.hex()} {' '.join(MS.describe(pkt))}"
            elif verb == "RAW":
                w.freeze()
                pkt = do_set(w, None, rest.replace(",", " ").split())
                reply = f"OK {pkt.hex()}"
            elif verb == "CLEAR":
                w.freeze()
                reply = f"OK {w.clear().hex()}"
            elif verb == "GET":
                pkt = w.read(w.packet_addr, MS.PACKET_LEN)
                reply = f"{pkt.hex()} {' '.join(MS.describe(pkt))}"
            elif verb == "RESTORE":
                w.unfreeze()
                w.clear()
                reply = "RESTORED"
            else:
                reply = "ERR unknown-verb"
        except Exception as e:  # noqa: BLE001
            reply = f"ERR {type(e).__name__}: {e}"
            try:
                if w:
                    w.close()
            finally:
                w = None
        print(f"{addr[0]}:{addr[1]} {line!r} -> {reply}")
        sock.sendto(reply.encode(), addr)


if __name__ == "__main__":
    sys.exit(main())
