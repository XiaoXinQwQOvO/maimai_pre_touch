"""Diagnose InjectTouchInput parameter errors (error 87)."""
import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_touch as P  # noqa: E402

user32 = P.user32

print("sizeof(POINTER_INFO) =", ctypes.sizeof(P.POINTER_INFO), "(expect 96)")
print("sizeof(POINTER_TOUCH_INFO) =", ctypes.sizeof(P.POINTER_TOUCH_INFO), "(expect 144)")

win = P.find_game_window(5)
cx0, cy0, cw, ch = P.client_rects(win["hwnd"])
print("client:", cx0, cy0, cw, ch)
cx, cy = cx0 + cw // 2, cy0 + ch // 2

inject = user32.InjectTouchInput
inject.argtypes = [ctypes.c_uint32, ctypes.POINTER(P.POINTER_TOUCH_INFO)]
inject.restype = wt.BOOL

init = user32.InitializeTouchInjection
init.argtypes = [ctypes.c_uint32, wt.DWORD]
init.restype = wt.BOOL


def try_inject(label, contacts):
    arr = (P.POINTER_TOUCH_INFO * len(contacts))(*contacts)
    ctypes.set_last_error(0)
    ok = inject(len(contacts), arr)
    err = ctypes.get_last_error()
    print(f"{label:52s} ok={ok} err={err}")
    return ok


def contact(x, y, flags, pointer_id=0, touch_mask=None, touch_flags=0):
    c = P.POINTER_TOUCH_INFO()
    ctypes.memset(ctypes.byref(c), 0, ctypes.sizeof(c))
    c.pointerInfo.pointerType = 2
    c.pointerInfo.pointerId = pointer_id
    c.pointerInfo.ptPixelLocation.x = x
    c.pointerInfo.ptPixelLocation.y = y
    c.pointerInfo.ptPixelLocationRaw.x = x
    c.pointerInfo.ptPixelLocationRaw.y = y
    c.pointerInfo.pointerFlags = flags
    c.touchFlags = touch_flags
    c.touchMask = P.TOUCH_MASK_CONTACTAREA | P.TOUCH_MASK_ORIENTATION | P.TOUCH_MASK_PRESSURE if touch_mask is None else touch_mask
    c.rcContact.left = x - 5
    c.rcContact.right = x + 5
    c.rcContact.top = y - 5
    c.rcContact.bottom = y + 5
    c.orientation = 90
    c.pressure = 32000
    return c


print("\n-- InitializeTouchInjection variants --")
for mc in (2, 10, 256):
    ctypes.set_last_error(0)
    ok = init(mc, P.TOUCH_FEEDBACK_NONE)
    print(f"maxCount={mc:<4d} ok={ok} err={ctypes.get_last_error()}")

DN = P.POINTER_FLAG_DOWN
IR = P.POINTER_FLAG_INRANGE
IC = P.POINTER_FLAG_INCONTACT
UP = P.POINTER_FLAG_UP

print("\n-- single contact variants --")
variants = [
    ("DOWN only", DN),
    ("DOWN|INRANGE", DN | IR),
    ("DOWN|INCONTACT", DN | IC),
    ("DOWN|INRANGE|INCONTACT", DN | IR | IC),
    ("INRANGE|INCONTACT|UPDATE", IR | IC | P.POINTER_FLAG_UPDATE),
]
for label, flags in variants:
    for pid in (0, 1):
        for tmask in (None, 0):
            lbl = f"{label} pid={pid} mask={'default' if tmask is None else 'none'}"
            try_inject(lbl, [contact(cx, cy - 300, flags, pid, tmask)])
            time.sleep(0.15)

print("\n-- two contacts --")
try_inject(
    "two DOWN pid0 both",
    [contact(cx - 300, cy, DN | IR | IC, 0), contact(cx, cy + 300, DN | IR | IC, 0)],
)
time.sleep(0.2)
try_inject(
    "two DOWN pid1/pid2",
    [contact(cx - 300, cy, DN | IR | IC, 1), contact(cx, cy + 300, DN | IR | IC, 2)],
)
time.sleep(0.3)
try_inject("release pid1/pid2", [contact(cx - 300, cy, UP, 1), contact(cx, cy + 300, UP, 2)])
try_inject("release pid0", [contact(cx, cy - 300, UP, 0), contact(cx, cy + 300, UP, 0)])

# Is input injection allowed at all for this process? try SetCursorPos-equivalent via mouse_event
print("\n-- integrity / desktop check --")
h = user32.GetProcessWindowStation()
print("window station handle:", h)
