"""Control experiment: does InjectSyntheticPointerInput actually produce
WM_POINTER messages on this machine (Windows 11 26200)?

Creates a plain top-level window, pumps messages on a worker thread, injects a
synthetic touch contact inside it and reports which messages the window got.
No game / no frida involved - this isolates the OS-level injection path from
the micetools DLL.

Run:  python tools/probe_synth_pointer_window.py
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import sys
import threading
import time
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pc"))
from touch_inject import SyntheticTouch  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_POINTERUPDATE = 0x0245
WM_POINTERDOWN = 0x0246
WM_POINTERUP = 0x0247
WM_POINTERENTER = 0x0249
WM_POINTERLEAVE = 0x024A
WM_POINTERCAPTURECHANGED = 0x024C
MSG_NAMES = {
    0x0241: "WM_NCPOINTERUPDATE", 0x0242: "WM_NCPOINTERDOWN", 0x0243: "WM_NCPOINTERUP",
    WM_POINTERUPDATE: "WM_POINTERUPDATE", WM_POINTERDOWN: "WM_POINTERDOWN",
    WM_POINTERUP: "WM_POINTERUP", 0x0248: "WM_POINTERROUTEDAWAY",
    WM_POINTERENTER: "WM_POINTERENTER", WM_POINTERLEAVE: "WM_POINTERLEAVE",
    0x024B: "WM_POINTERACTIVATE", WM_POINTERCAPTURECHANGED: "WM_POINTERCAPTURECHANGED",
    0x0002: "WM_MOVE", 0x000F: "WM_PAINT", 0x0100: "WM_KEYDOWN",
    0x0200: "WM_MOUSEMOVE", 0x0201: "WM_LBUTTONDOWN", 0x0202: "WM_LBUTTONUP",
    0x0084: "WM_NCHITTEST",
}

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, ctypes.c_size_t, ctypes.c_ssize_t)

user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
user32.DefWindowProcW.restype = ctypes.c_ssize_t

messages = []
hwnd_box = {}
ready = threading.Event()
stop = threading.Event()
hwnd_ready = threading.Event()


def wndproc(hwnd, msg, wparam, lparam):
    if msg in MSG_NAMES:
        messages.append({"msg": MSG_NAMES[msg], "code": hex(msg),
                         "wParam": int(wparam), "lParam": int(lparam),
                         "pointerId": int(wparam) & 0xFFFF,
                         "flags": (int(wparam) >> 16) & 0xFFFF,
                         "t": round(time.time(), 3)})
    if msg == 0x0010:  # WM_CLOSE
        user32.DestroyWindow(hwnd)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


def worker():
    hinst = kernel32.GetModuleHandleW(None)
    cls = WNDCLASSW()
    cls.style = 0x0020 | 0x0002  # CS_OWNDC | CS_HREDRAW
    cls.lpfnWndProc = ctypes.cast(WNDPROC(wndproc), ctypes.c_void_p)
    cls.hInstance = hinst
    cls.hbrBackground = ctypes.cast(ctypes.c_void_p(6), wt.HBRUSH)  # COLOR_WINDOW+1
    cls.lpszClassName = "DSHProbeSynthPointer"
    if not user32.RegisterClassW(ctypes.byref(cls)):
        err = ctypes.get_last_error()
        if err not in (1410,):  # class already exists
            print("[!] RegisterClassW failed", err)
    hwnd = user32.CreateWindowExW(0, "DSHProbeSynthPointer", "synth probe",
                                  0x00CF0000,  # WS_OVERLAPPEDWINDOW
                                  100, 100, 500, 500, None, None, hinst, None)
    if not hwnd:
        print("[!] CreateWindowExW failed", ctypes.get_last_error())
        return
    hwnd_box["hwnd"] = hwnd
    user32.ShowWindow(hwnd, 5)   # SW_SHOW
    user32.UpdateWindow(hwnd)
    hwnd_ready.set()
    if not user32.SetForegroundWindow(hwnd):
        print("[!] SetForegroundWindow failed (continuing)")
    msg = wt.MSG()
    while not stop.is_set():
        if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            if msg.message == 0x0012:  # WM_QUIT
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        else:
            time.sleep(0.005)
    ready.set()


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def main():
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    if not hwnd_ready.wait(5):
        print("[!] window not created")
        return 1
    hwnd = hwnd_box["hwnd"]
    time.sleep(0.4)
    out = {"hwnd": hex(hwnd), "fg_before": hex(user32.GetForegroundWindow() or 0),
           "tests": []}
    print(f"[i] probe window hwnd={hwnd:#x}") 
    pt = POINT(200, 250)   # client (100,150) roughly -> screen coords
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    under = user32.WindowFromPoint(pt)
    out["target_point"] = [pt.x, pt.y]
    out["window_from_point"] = hex(under or 0)
    out["wfp_is_probe"] = bool(under == hwnd)
    print(f"[i] point ({pt.x},{pt.y}) WindowFromPoint={under:#x} probe={hwnd:#x}")
    out["fg_after"] = hex(user32.GetForegroundWindow() or 0)

    messages.clear()
    try:
        dev = SyntheticTouch(max_contacts=10)
        dev.down(7, pt.x, pt.y)
        time.sleep(0.4)
        phase1 = list(messages)
        dev.move(7, pt.x + 10, pt.y + 10)
        time.sleep(0.3)
        phase2 = [m for m in messages if m not in phase1]
        dev.up(7)
        time.sleep(0.3)
        dev.close()
        out["phases"] = {"down": phase1, "move": phase2}
        print(f"[i] messages after down: {len(phase1)}")
        for m in phase1[:12]:
            print("   ", m)
        print(f"[i] messages after move: {len(phase2)}")
        for m in phase2[:12]:
            print("   ", m)
    except OSError as e:
        out["inject_error"] = str(e)
        print("[!] inject:", e)

    # also try the legacy API for reference
    try:
        ok = user32.InitializeTouchInjection(10, 3)
        out["InitializeTouchInjection"] = {"ok": bool(ok), "err": ctypes.get_last_error()}
    except Exception as e:
        out["InitializeTouchInjection"] = {"error": str(e)}

    # sanity check: does the message loop see ordinary mouse input at all?
    messages.clear()
    user32.SetCursorPos(pt.x, pt.y)
    time.sleep(0.25)
    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", wt.DWORD),
                    ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]
    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT)]
    class INPUT(ctypes.Structure):
        _fields_ = [("type", wt.DWORD), ("u", _U)]
    for flags in (0x0002, 0x0004):
        inp = INPUT()
        inp.type = 0
        inp.u.mi.dwFlags = flags
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        time.sleep(0.15)
    out["mouse_control"] = [m["msg"] for m in messages]
    print("[i] mouse control messages:", sorted(set(out["mouse_control"])))
    if not messages:
        print("[!] even real mouse messages did not arrive -> window/loop problem")

    stop.set()
    user32.PostMessageW(hwnd, 0x0010, 0, 0)
    time.sleep(0.2)
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_out", "synth_window.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    print("[i] ->", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
