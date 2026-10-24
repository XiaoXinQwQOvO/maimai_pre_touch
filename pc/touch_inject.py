"""Windows synthetic pointer (multi-touch) injection.

On Windows 11 26200 the legacy `InjectTouchInput` API rejects every contact with
ERROR_INVALID_PARAMETER (87), so this module uses the modern
`CreateSyntheticPointerDevice` / `InjectSyntheticPointerInput` API instead, which
accepts up to 10 simultaneous contacts.  Injected input is delivered as real
WM_POINTER events (PT_TOUCH), i.e. exactly like a physical touch digitizer - no
mouse events are synthesised.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

user32 = ctypes.WinDLL("user32", use_last_error=True)

PT_TOUCH = 2

POINTER_FLAG_INRANGE = 0x00000002
POINTER_FLAG_INCONTACT = 0x00000004
POINTER_FLAG_DOWN = 0x00010000
POINTER_FLAG_UPDATE = 0x00020000
POINTER_FLAG_UP = 0x00040000

TOUCH_MASK_CONTACTAREA = 0x00000001
TOUCH_MASK_ORIENTATION = 0x00000002
TOUCH_MASK_PRESSURE = 0x00000004

POINTER_FEEDBACK_DEFAULT = 1
POINTER_FEEDBACK_INDIRECT = 2
POINTER_FEEDBACK_NONE = 3


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


class _PointerUnion(ctypes.Union):
    _fields_ = [("touchInfo", POINTER_TOUCH_INFO)]


class POINTER_TYPE_INFO(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_uint32), ("u", _PointerUnion)]


assert ctypes.sizeof(POINTER_INFO) == 96, ctypes.sizeof(POINTER_INFO)
assert ctypes.sizeof(POINTER_TOUCH_INFO) == 144, ctypes.sizeof(POINTER_TOUCH_INFO)
assert ctypes.sizeof(POINTER_TYPE_INFO) == 152, ctypes.sizeof(POINTER_TYPE_INFO)

user32.CreateSyntheticPointerDevice.argtypes = [ctypes.c_uint32, ctypes.c_ulong, ctypes.c_uint32]
user32.CreateSyntheticPointerDevice.restype = ctypes.c_void_p
user32.InjectSyntheticPointerInput.argtypes = [ctypes.c_void_p, ctypes.POINTER(POINTER_TYPE_INFO), ctypes.c_uint32]
user32.InjectSyntheticPointerInput.restype = wt.BOOL
user32.DestroySyntheticPointerDevice.argtypes = [ctypes.c_void_p]
user32.DestroySyntheticPointerDevice.restype = None


def _touch_info(x: float, y: float, flags: int, pointer_id: int, radius: int = 4) -> POINTER_TYPE_INFO:
    info = POINTER_TYPE_INFO()
    ctypes.memset(ctypes.byref(info), 0, ctypes.sizeof(info))
    info.type = PT_TOUCH
    ti = info.touchInfo
    ti.pointerInfo.pointerType = PT_TOUCH
    ti.pointerInfo.pointerId = pointer_id
    ti.pointerInfo.ptPixelLocation.x = int(round(x))
    ti.pointerInfo.ptPixelLocation.y = int(round(y))
    ti.pointerInfo.ptPixelLocationRaw.x = int(round(x))
    ti.pointerInfo.ptPixelLocationRaw.y = int(round(y))
    ti.pointerInfo.pointerFlags = flags
    ti.touchFlags = 0
    ti.touchMask = TOUCH_MASK_CONTACTAREA | TOUCH_MASK_ORIENTATION | TOUCH_MASK_PRESSURE
    ti.rcContact.left = int(round(x)) - radius
    ti.rcContact.right = int(round(x)) + radius
    ti.rcContact.top = int(round(y)) - radius
    ti.rcContact.bottom = int(round(y)) + radius
    ti.rcContactRaw = ti.rcContact
    ti.orientation = 90
    ti.pressure = 32000
    return info


class SyntheticTouch:
    """Pool of synthetic touch contacts (max 10 on the Windows API)."""

    MAX_CONTACTS = 10

    def __init__(self, max_contacts: int = MAX_CONTACTS, feedback: int = POINTER_FEEDBACK_NONE):
        if not 1 <= max_contacts <= self.MAX_CONTACTS:
            raise ValueError("max_contacts must be 1..10")
        ctypes.set_last_error(0)
        handle = user32.CreateSyntheticPointerDevice(PT_TOUCH, max_contacts, feedback)
        if not handle:
            raise OSError(f"CreateSyntheticPointerDevice failed: {ctypes.get_last_error()}")
        self._handle = handle
        self._active: set[int] = set()
        self._last: dict[int, tuple[float, float]] = {}

    def _send(self, infos) -> None:
        arr = (POINTER_TYPE_INFO * len(infos))(*infos)
        ctypes.set_last_error(0)
        if not user32.InjectSyntheticPointerInput(self._handle, arr, len(infos)):
            raise OSError(f"InjectSyntheticPointerInput failed: {ctypes.get_last_error()}")

    def down(self, pointer_id: int, x: float, y: float) -> None:
        self._send([_touch_info(x, y, POINTER_FLAG_DOWN | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT, pointer_id)])
        self._active.add(pointer_id)
        self._last[pointer_id] = (x, y)

    def move(self, pointer_id: int, x: float, y: float) -> None:
        self._send([_touch_info(x, y, POINTER_FLAG_UPDATE | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT, pointer_id)])
        self._last[pointer_id] = (x, y)

    def up(self, pointer_id: int) -> None:
        x, y = self._last.get(pointer_id, (0.0, 0.0))
        self._send([_touch_info(x, y, POINTER_FLAG_UP, pointer_id)])
        self._active.discard(pointer_id)
        self._last.pop(pointer_id, None)

    def close(self) -> None:
        for pid in list(self._active):
            try:
                self.up(pid)
            except OSError:
                pass
        if self._handle:
            user32.DestroySyntheticPointerDevice(self._handle)
            self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
