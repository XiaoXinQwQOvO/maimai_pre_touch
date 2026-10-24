"""Try the modern synthetic-pointer injection API (CreateSyntheticPointerDevice)."""
import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_touch as P  # noqa: E402

user32 = P.user32

POINTER_INPUT_TYPE_TOUCH = 2
POINTER_FEEDBACK_NONE_MODE = 3  # POINTER_FEEDBACK_DEFAULT=1, INDIRECT=2, NONE=3


class POINTER_TYPE_INFO(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("touchInfo", P.POINTER_TOUCH_INFO)]

    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_uint32), ("u", _U)]


print("sizeof POINTER_TYPE_INFO =", ctypes.sizeof(POINTER_TYPE_INFO), "(expect 152)")

create = user32.CreateSyntheticPointerDevice
create.argtypes = [ctypes.c_uint32, ctypes.c_ulong, ctypes.c_uint32]
create.restype = ctypes.c_void_p

inject = user32.InjectSyntheticPointerInput
inject.argtypes = [ctypes.c_void_p, ctypes.POINTER(POINTER_TYPE_INFO), ctypes.c_uint32]
inject.restype = wt.BOOL

destroy = user32.DestroySyntheticPointerDevice
destroy.argtypes = [ctypes.c_void_p]
destroy.restype = None

ctypes.set_last_error(0)
dev = create(POINTER_INPUT_TYPE_TOUCH, 10, POINTER_FEEDBACK_NONE_MODE)
print("CreateSyntheticPointerDevice ->", dev, "err", ctypes.get_last_error())
if not dev:
    raise SystemExit(1)

win = P.find_game_window(5)
cx0, cy0, cw, ch = P.client_rects(win["hwnd"])
cx, cy = cx0 + cw // 2, cy0 + ch // 2
print("client:", cx0, cy0, cw, ch)
P.user32.SetForegroundWindow(win["hwnd"])


def mk(x, y, flags, pid=0):
    ti = POINTER_TYPE_INFO()
    ctypes.memset(ctypes.byref(ti), 0, ctypes.sizeof(ti))
    ti.type = POINTER_INPUT_TYPE_TOUCH
    c = ti.touchInfo
    c.pointerInfo.pointerType = POINTER_INPUT_TYPE_TOUCH
    c.pointerInfo.pointerId = pid
    c.pointerInfo.ptPixelLocation.x = x
    c.pointerInfo.ptPixelLocation.y = y
    c.pointerInfo.ptPixelLocationRaw.x = x
    c.pointerInfo.ptPixelLocationRaw.y = y
    c.pointerInfo.pointerFlags = flags
    c.touchFlags = 0
    c.touchMask = P.TOUCH_MASK_CONTACTAREA | P.TOUCH_MASK_ORIENTATION | P.TOUCH_MASK_PRESSURE
    c.rcContact.left = x - 4
    c.rcContact.right = x + 4
    c.rcContact.top = y - 4
    c.rcContact.bottom = y + 4
    c.orientation = 90
    c.pressure = 32000
    return ti


DN = P.POINTER_FLAG_DOWN
IR = P.POINTER_FLAG_INRANGE
IC = P.POINTER_FLAG_INCONTACT
UP = P.POINTER_FLAG_UP


def send(label, infos):
    arr = (POINTER_TYPE_INFO * len(infos))(*infos)
    ctypes.set_last_error(0)
    ok = inject(dev, arr, len(infos))
    print(f"{label:44s} ok={ok} err={ctypes.get_last_error()}")
    return ok


bbox = (cx0, cy0, cx0 + cw, cy0 + ch)
before = P.grab(bbox, os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_out", "s0_before.png"))

ok1 = send("1 finger DOWN up-zone", [mk(cx, cy - 320, DN | IR | IC)])
time.sleep(0.7)
mid = P.grab(bbox, os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_out", "s1_finger.png"))
print("visual diff 1 finger:", P.diff_score(before, mid))

ok2 = send("2 fingers DOWN (left + down)", [mk(cx - 320, cy, DN | IR | IC, 1), mk(cx, cy + 320, DN | IR | IC, 2)])
time.sleep(0.7)
mid2 = P.grab(bbox, os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_out", "s2_fingers2.png"))
print("visual diff 2 fingers:", P.diff_score(mid, mid2))

send("2 fingers UP", [mk(cx - 320, cy, UP, 1), mk(cx, cy + 320, UP, 2)])
send("1 finger UP", [mk(cx, cy - 320, UP, 0)])
time.sleep(0.5)
after = P.grab(bbox, os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_out", "s3_after.png"))
print("visual diff after release:", P.diff_score(mid2, after))

destroy(dev)
print("done")
