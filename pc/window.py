"""Locate the maimai game window and report its client area in screen pixels."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def set_dpi_aware() -> None:
    """Make this process per-monitor DPI aware so window rects match physical pixels."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def process_image_name(pid: int) -> str:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def iter_windows():
    """Yield (hwnd, pid, title, class_name, rect) for visible top-level windows."""
    found = []
    EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        found.append(
            (
                hwnd,
                pid.value,
                title.value,
                cls.value,
                (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top),
            )
        )
        return True

    user32.EnumWindows(EnumProc(callback), 0)
    return found


def find_game_window(process_names=("maimai.exe", "maimai_dump_.exe"), class_substr: str = "teaGfx"):
    """Return dict(hwnd, pid, title, class, rect, exe) or None."""
    names = {n.lower() for n in process_names}
    candidates = []
    for hwnd, pid, title, cls, rect in iter_windows():
        if rect[2] < 200 or rect[3] < 200:
            continue
        exe = process_image_name(pid)
        base = exe.rsplit("\\", 1)[-1].lower()
        class_ok = class_substr.lower() in cls.lower() if class_substr else True
        if base in names or (class_ok and class_substr):
            candidates.append({"hwnd": hwnd, "pid": pid, "title": title, "class": cls, "rect": rect, "exe": exe})
    if not candidates:
        return None
    candidates.sort(key=lambda c: c["rect"][2] * c["rect"][3], reverse=True)
    return candidates[0]


def client_rect(win) -> tuple[int, int, int, int]:
    """Client area of the game window in screen coordinates: (x, y, w, h)."""
    hwnd = win["hwnd"]
    rect = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    origin = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(origin))
    return origin.x, origin.y, rect.right - rect.left, rect.bottom - rect.top


def bring_to_front(win) -> None:
    user32.SetForegroundWindow(win["hwnd"])
    user32.SetWindowPos(win["hwnd"], -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)
