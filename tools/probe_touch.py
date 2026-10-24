"""Probe how micetools (mice.dll) receives touch panel input for maimai DX.

Steps
1. optionally launch G:\\Original\\maimai\\mice.exe (log_level bumped to 6)
2. wait for the maimai.exe window
3. test A: mouse hover only (no click) over a touch zone position
4. test B: two simultaneous contacts via Windows touch injection (InjectTouchInput)
5. report: mice log lines mentioning touch/zone, screenshots saved for visual diff

Run:  python tools/probe_touch.py [--no-launch] [--wait 120]
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import shutil
import subprocess
import sys
import time

GAME_DIR = r"G:\Original\maimai"
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_out")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class POINTER_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerType", ctypes.c_uint32),
        ("pointerId", ctypes.c_uint32),
        ("frameId", ctypes.c_uint32),
        ("pointerFlags", ctypes.c_uint32),
        ("sourceDevice", ctypes.c_void_p),
        ("hwndTarget", wt.HWND),
        ("ptPixelLocation", wt.POINT),
        ("ptHimetricLocation", wt.POINT),
        ("ptPixelLocationRaw", wt.POINT),
        ("ptHimetricLocationRaw", wt.POINT),
        ("dwTime", wt.DWORD),
        ("historyCount", ctypes.c_uint32),
        ("InputState", ctypes.c_int32),
        ("dwKeyStates", wt.DWORD),
        ("PerformanceCount", ctypes.c_uint64),
        ("ButtonChangeType", ctypes.c_int32),
    ]


class POINTER_TOUCH_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerInfo", POINTER_INFO),
        ("touchFlags", ctypes.c_uint32),
        ("touchMask", ctypes.c_uint32),
        ("rcContact", wt.RECT),
        ("rcContactRaw", wt.RECT),
        ("orientation", ctypes.c_uint32),
        ("pressure", ctypes.c_uint32),
    ]


POINTER_FLAG_INRANGE = 0x00000002
POINTER_FLAG_INCONTACT = 0x00000004
POINTER_FLAG_DOWN = 0x00010000
POINTER_FLAG_UPDATE = 0x00020000
POINTER_FLAG_UP = 0x00040000
TOUCH_MASK_CONTACTAREA = 0x00000001
TOUCH_MASK_ORIENTATION = 0x00000002
TOUCH_MASK_PRESSURE = 0x00000004
TOUCH_FEEDBACK_NONE = 0x3


def wintitle_windows():
    """Yield (hwnd, pid, title, class, rect) for visible top-level windows."""
    result = []
    EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        result.append(
            (
                hwnd,
                pid.value,
                buf.value,
                cls.value,
                (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top),
            )
        )
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return result


def process_name(pid):
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(h)


def find_game_window(timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for hwnd, pid, title, cls, rect in wintitle_windows():
            name = process_name(pid)
            if name.lower().endswith("maimai.exe") or name.lower().endswith("maimai_dump_.exe"):
                if rect[2] > 200 and rect[3] > 200:
                    return {
                        "hwnd": hwnd,
                        "pid": pid,
                        "title": title,
                        "class": cls,
                        "rect": rect,
                        "exe": name,
                    }
        time.sleep(1.0)
    return None


def client_rects(hwnd):
    cr = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(cr))
    origin = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(origin))
    return origin.x, origin.y, cr.right - cr.left, cr.bottom - cr.top


def grab(bbox, path):
    from PIL import ImageGrab

    img = ImageGrab.grab(bbox=bbox)
    img.save(path)
    return img


def diff_score(a, b):
    import numpy as np

    x = np.asarray(a.convert("L"), dtype=np.int16)
    y = np.asarray(b.convert("L"), dtype=np.int16)
    d = np.abs(x - y)
    return {"mean": float(d.mean()), "max": int(d.max()), "changed_px": int((d > 24).sum())}


def inject_setup(max_contacts=10):
    fn = user32.InitializeTouchInjection
    fn.argtypes = [ctypes.c_uint32, wt.DWORD]
    fn.restype = wt.BOOL
    ok = fn(max_contacts, TOUCH_FEEDBACK_NONE)
    err = ctypes.get_last_error()
    return ok, err


def inject_contacts(contacts):
    fn = user32.InjectTouchInput
    fn.argtypes = [ctypes.c_uint32, ctypes.POINTER(POINTER_TOUCH_INFO)]
    fn.restype = wt.BOOL
    arr = (POINTER_TOUCH_INFO * len(contacts))(*contacts)
    fn.argtypes = [ctypes.c_uint32, ctypes.POINTER(POINTER_TOUCH_INFO)]
    ok = fn(len(contacts), arr)
    return ok, ctypes.get_last_error()


def make_contact(x, y, flags):
    c = POINTER_TOUCH_INFO()
    ctypes.memset(ctypes.byref(c), 0, ctypes.sizeof(c))
    c.pointerInfo.pointerType = 2  # PT_TOUCH
    c.pointerInfo.pointerId = 0
    c.pointerInfo.ptPixelLocation.x = int(x)
    c.pointerInfo.ptPixelLocation.y = int(y)
    c.pointerInfo.ptPixelLocationRaw.x = int(x)
    c.pointerInfo.ptPixelLocationRaw.y = int(y)
    c.pointerInfo.pointerFlags = flags
    c.touchFlags = 0
    c.touchMask = TOUCH_MASK_CONTACTAREA | TOUCH_MASK_ORIENTATION | TOUCH_MASK_PRESSURE
    c.rcContact.left = int(x) - 5
    c.rcContact.right = int(x) + 5
    c.rcContact.top = int(y) - 5
    c.rcContact.bottom = int(y) + 5
    c.orientation = 90
    c.pressure = 32000
    return c


def read_log(log_path, from_offset):
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(from_offset)
            return f.read(), f.tell()
    except FileNotFoundError:
        return "", from_offset


def bump_log_level(value):
    cfg = os.path.join(GAME_DIR, "mice", "config.ini")
    bak = cfg + ".probe_bak"
    if not os.path.exists(bak):
        shutil.copy2(cfg, bak)
    with open(cfg, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    text = re.sub(r"(?m)^log_level = \d+", f"log_level = {value}", text, count=1)
    with open(cfg, "w", encoding="utf-8", errors="replace") as f:
        f.write(text)
    return bak


def restore_config(bak):
    if os.path.exists(bak):
        cfg = os.path.join(GAME_DIR, "mice", "config.ini")
        shutil.copy2(bak, cfg)
        os.remove(bak)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-launch", action="store_true")
    ap.add_argument("--wait", type=int, default=150)
    ap.add_argument("--boot-wait", type=int, default=45)
    ap.add_argument("--keep-running", action="store_true")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    log_path = os.path.join(GAME_DIR, "log.txt")
    report = {"log_path": log_path, "tests": []}

    bak = bump_log_level(6)
    report["config_backup"] = bak
    if os.path.exists(log_path):
        open(log_path, "w").close()

    proc = None
    win = find_game_window(1)
    if win is None and not args.no_launch:
        proc = subprocess.Popen(
            [os.path.join(GAME_DIR, "mice.exe")],
            cwd=GAME_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=0x00000008,  # DETACHED_PROCESS
        )
        report["launched_pid"] = proc.pid

    win = find_game_window(args.wait)
    if win is None:
        report["error"] = "game window not found"
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 1

    report["window"] = {k: win[k] for k in ("pid", "title", "class", "exe")}
    report["window"]["rect"] = win["rect"]
    cx0, cy0, cw, ch = client_rects(win["hwnd"])
    report["client"] = {"x": cx0, "y": cy0, "w": cw, "h": ch}

    user32.SetForegroundWindow(win["hwnd"])
    user32.SetWindowPos(win["hwnd"], -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)

    print(f"[i] waiting {args.boot_wait}s for game boot ...")
    time.sleep(args.boot_wait)

    cx = cx0 + cw // 2
    cy = cy0 + ch // 2
    ring = int(min(cw, ch) * 0.38)
    pts = {
        "up": (cx, cy - ring),
        "right": (cx + ring, cy),
        "down": (cx, cy + ring),
        "left": (cx - ring, cy),
        "center": (cx, cy),
    }
    report["points"] = pts

    bbox = (cx0, cy0, cx0 + cw, cy0 + ch)

    # ---- baseline ----
    off = 0
    _, off = read_log(log_path, off)
    base = grab(bbox, os.path.join(OUT_DIR, "00_base.png"))
    time.sleep(0.5)

    # ---- test A: mouse hover only (no click) ----
    user32.SetCursorPos(pts["up"][0], pts["up"][1])
    time.sleep(0.8)
    hover = grab(bbox, os.path.join(OUT_DIR, "01_hover.png"))
    a_diff = diff_score(base, hover)
    user32.SetCursorPos(cx0 + 3, cy0 + 3)
    time.sleep(0.4)
    after = grab(bbox, os.path.join(OUT_DIR, "02_hover_leave.png"))
    a_diff_leave = diff_score(hover, after)
    logtxt, off = read_log(log_path, off)
    report["tests"].append(
        {
            "name": "A_mouse_hover",
            "point": pts["up"],
            "diff_vs_base": a_diff,
            "diff_after_leave": a_diff_leave,
            "log": logtxt[-4000:],
        }
    )

    # ---- test B: two simultaneous injected touch contacts ----
    ok, err = inject_setup()
    report["tests"].append({"name": "B_setup", "ok": bool(ok), "err": err})
    if ok:
        contacts = [
            make_contact(pts["left"][0], pts["left"][1], POINTER_FLAG_DOWN | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT),
            make_contact(pts["down"][0], pts["down"][1], POINTER_FLAG_DOWN | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT),
        ]
        down_ok, down_err = inject_contacts(contacts)
        time.sleep(0.8)
        touch = grab(bbox, os.path.join(OUT_DIR, "03_touch2.png"))
        b_diff = diff_score(after, touch)
        logtxt2, off = read_log(log_path, off)
        up = [
            make_contact(pts["left"][0], pts["left"][1], POINTER_FLAG_UP),
            make_contact(pts["down"][0], pts["down"][1], POINTER_FLAG_UP),
        ]
        up_ok, up_err = inject_contacts(up)
        report["tests"].append(
            {
                "name": "B_touch_inject_two",
                "down_ok": bool(down_ok),
                "down_err": down_err,
                "up_ok": bool(up_ok),
                "up_err": up_err,
                "diff_vs_prev": b_diff,
                "log": logtxt2[-6000:],
            }
        )

    # ---- final log tail ----
    logtail, off = read_log(log_path, off)
    report["log_tail"] = logtail[-8000:]

    interesting = []
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if re.search(r"touch|Touch|zone|Zone|panel|Panel|pointer|Pointer", line):
                    interesting.append(line.rstrip())
    except FileNotFoundError:
        pass
    report["log_touch_lines"] = interesting[-80:]

    with open(os.path.join(OUT_DIR, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(json.dumps({k: v for k, v in report.items() if k not in ("log_tail", "log_touch_lines")}, indent=2, ensure_ascii=False))
    print("=== touch-related log lines ===")
    for line in report["log_touch_lines"]:
        print(line)

    if not args.keep_running:
        subprocess.run(["taskkill", "/F", "/IM", "maimai_dump_.exe"], capture_output=True)
        subprocess.run(["taskkill", "/F", "/IM", "maimai.exe"], capture_output=True)
        subprocess.run(["taskkill", "/F", "/IM", "mice.exe"], capture_output=True)
    restore_config(bak)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BaseException:
        cfg_bak = os.path.join(GAME_DIR, "mice", "config.ini.probe_bak")
        if os.path.exists(cfg_bak):
            shutil.copy2(cfg_bak, os.path.join(GAME_DIR, "mice", "config.ini"))
            os.remove(cfg_bak)
            print("[i] config.ini restored after failure")
        raise
