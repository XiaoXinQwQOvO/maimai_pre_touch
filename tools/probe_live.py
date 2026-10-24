"""Ad-hoc probe against an already-running game instance (no relaunch, no log reads)."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_touch as P  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_out")
os.makedirs(OUT, exist_ok=True)

win = P.find_game_window(10)
if not win:
    print("no game window")
    raise SystemExit(1)
print("window:", json.dumps({k: win[k] for k in ("pid", "title", "class", "exe")}, ensure_ascii=False))
print("rect:", win["rect"])
cx0, cy0, cw, ch = P.client_rects(win["hwnd"])
print("client:", cx0, cy0, cw, ch)

P.user32.SetForegroundWindow(win["hwnd"])
time.sleep(0.5)

cx, cy = cx0 + cw // 2, cy0 + ch // 2
ring = int(min(cw, ch) * 0.38)
pts = {
    "up": (cx, cy - ring),
    "down": (cx, cy + ring),
    "left": (cx - ring, cy),
    "right": (cx + ring, cy),
    "center": (cx, cy),
}
bbox = (cx0, cy0, cx0 + cw, cy0 + ch)

base = P.grab(bbox, os.path.join(OUT, "a0_base.png"))

# --- A: mouse hover only, no button press ---
P.user32.SetCursorPos(*pts["up"])
time.sleep(1.0)
hover = P.grab(bbox, os.path.join(OUT, "a1_hover_up.png"))
print("A hover diff:", P.diff_score(base, hover))

# move to another zone (hover right)
P.user32.SetCursorPos(*pts["right"])
time.sleep(1.0)
hover2 = P.grab(bbox, os.path.join(OUT, "a2_hover_right.png"))
print("A hover-right diff vs hover-up:", P.diff_score(hover, hover2))

# leave window
P.user32.SetCursorPos(cx0 + 2, cy0 + 2)
time.sleep(0.8)
leave = P.grab(bbox, os.path.join(OUT, "a3_leave.png"))
print("A leave diff:", P.diff_score(hover2, leave))

# --- B: two simultaneous injected touch contacts (no mouse involved) ---
ok, err = P.inject_setup()
print("B setup ok:", ok, "err:", err)
if ok:
    down = [
        P.make_contact(*pts["left"], P.POINTER_FLAG_DOWN | P.POINTER_FLAG_INRANGE | P.POINTER_FLAG_INCONTACT),
        P.make_contact(*pts["down"], P.POINTER_FLAG_DOWN | P.POINTER_FLAG_INRANGE | P.POINTER_FLAG_INCONTACT),
    ]
    d_ok, d_err = P.inject_contacts(down)
    print("B down ok:", d_ok, "err:", d_err)
    time.sleep(1.0)
    touch = P.grab(bbox, os.path.join(OUT, "b1_touch2.png"))
    print("B diff vs leave:", P.diff_score(leave, touch))
    up = [
        P.make_contact(*pts["left"], P.POINTER_FLAG_UP),
        P.make_contact(*pts["down"], P.POINTER_FLAG_UP),
    ]
    u_ok, u_err = P.inject_contacts(up)
    print("B up ok:", u_ok, "err:", u_err)
    time.sleep(1.0)
    after = P.grab(bbox, os.path.join(OUT, "b2_after.png"))
    print("B diff after release:", P.diff_score(touch, after))
print("done")
