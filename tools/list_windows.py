"""List visible top-level windows (title, class, rect, state) - sanity check for capture."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.SetProcessDPIAware()

EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def window_text(hwnd) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 2)
    user32.GetWindowTextW(hwnd, buffer, length + 2)
    return buffer.value


def class_name(hwnd) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def main() -> int:
    rect = wt.RECT()

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width < 60 or height < 60:
            return True
        title = window_text(hwnd)
        cls = class_name(hwnd)
        iconic = bool(user32.IsIconic(hwnd))
        print(
            f"hwnd={hwnd:>8} rect=({rect.left:5d},{rect.top:5d},{width:4d}x{height:4d}) "
            f"iconic={iconic!s:<5} class={cls[:34]:<34} title={title[:44]!r}"
        )
        return True

    print(f"screen: {user32.GetSystemMetrics(0)}x{user32.GetSystemMetrics(1)}")
    user32.EnumWindows(EnumWindowsProc(callback), 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
