"""Live check of the maitouch port write path against a running game.

Prerequisites: the game is already running (mice.exe / maimai_dump_.exe) with
mice.dll loaded.  The script only touches process memory - no frida, no mouse.

    python tools/test_port_backend.py                 # full check, then restore
    python tools/test_port_backend.py --keep-frozen    # leave the patch in place
    python tools/test_port_backend.py --mouse-check    # also verify that the
                                                       # game's own mouse->touch
                                                       # path writes the same buffer

Exit code 0 = PASS.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pc"))

import maitouch_port as MP  # noqa: E402
import sensors  # noqa: E402


def step(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{(': ' + detail) if detail else ''}")
    return ok


def mouse_check(port: MP.MaitouchPort, window_rect) -> bool:
    """Unfrozen sanity check: does a real mouse press show up in the packet?

    This is a diagnostic only - it proves our write target is the same buffer the
    game's own input path uses.  The product never injects mouse input.
    """
    import ctypes

    user32 = ctypes.WinDLL("user32")
    left, top, width, height = window_rect
    cx, cy = left + width // 2, top + height // 2
    port.unfreeze()
    time.sleep(0.3)
    user32.SetCursorPos(int(cx), int(cy))
    time.sleep(0.2)
    user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
    time.sleep(0.35)
    info = port.probe()
    user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP
    time.sleep(0.2)
    got = info.get("sensors")
    ok = got == [sensors.CENTRE]
    return step("mouse press lands on the centre sensor in the same packet", ok, f"sensors={got}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-frozen", action="store_true")
    parser.add_argument("--mouse-check", action="store_true")
    parser.add_argument("--window-rect", default="800,240,960,960")
    args = parser.parse_args()

    port = MP.MaitouchPort(log=_Logger())
    results: list[bool] = []

    if not port.attach():
        print(f"[FAIL] cannot attach to the game: {port.last_error}")
        return 1
    info = port.probe()
    print(f"[i] attached: pid={info['pid']} exe={info['exe']} base={info['base']}")
    print(f"[i] build_status prologue: {info['build_status']}")
    print(f"[i] packet now            : {info['packet']}  ({sensors.describe(bytes.fromhex(info['packet'])) if info['packet'] else '-'})")
    print(f"[i] touch_active flag     : {info['touch_active']}")

    results.append(step("mice.dll found and process readable", info["base"] is not None))
    results.append(step("build_status prologue recognisable", info["build_status"] == MP.BUILD_STATUS_PROLOGUE.hex(" ") or info["frozen"]))

    if args.mouse_check:
        left, top, width, height = (int(v) for v in args.window_rect.split(","))
        results.append(mouse_check(port, (left, top, width, height)))

    results.append(step("freeze build_status", port.freeze()))
    info = port.probe()
    results.append(step("ret patch visible in memory", info["build_status"].startswith("c3"), info["build_status"]))

    for names in (["C"], ["O0"], ["C", "I90", "O180", "I270", "O45"], sensors.SENSOR_ORDER):
        packet = sensors.packet_from_sensors(names)
        ok = port.write_packet(packet)
        back = port.read(MP.PACKET_RVA, sensors.PACKET_LEN)
        results.append(
            step(
                f"write {names if len(names) <= 6 else f'all {len(names)} sensors'}",
                ok and back == packet,
                packet.hex(" "),
            )
        )
        time.sleep(0.05)

    results.append(step("clear -> idle packet", port.clear() and port.read(MP.PACKET_RVA, sensors.PACKET_LEN) == sensors.IDLE_PACKET))

    if args.keep_frozen:
        print("[i] leaving build_status frozen (use `python pc/maitouch_port.py restore` to undo)")
    else:
        results.append(step("restore (unfreeze + idle packet)", port.restore()))
        info = port.probe()
        results.append(step("build_status back to 0x55", (info["build_status"] or "").startswith("55"), info["build_status"]))
    port.detach()

    print()
    print(f"RESULT: {sum(results)}/{len(results)} checks passed -> {'PASS' if all(results) else 'FAIL'}")
    return 0 if all(results) else 1


class _Logger:
    def info(self, msg, *a):
        print("[i] " + (msg % a if a else msg))

    def warning(self, msg, *a):
        print("[!] " + (msg % a if a else msg))

    def debug(self, msg, *a):
        pass


if __name__ == "__main__":
    raise SystemExit(main())
